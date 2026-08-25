"""ホスト側の波形解析。

規範: tools.md 5章 `analyze_waveform` / roadmap.md 2.4。

統計・FFTの計算自体はホスト側の純関数(`waveform_stats` / `waveform_fft`)で、
機器へは何も送らない。Toolの入口 `analyze_waveform` だけは `read_samples` で
波形取得(capture_waveformと同じ読み取り)を行ってから解析する。

- 統計とFFTはサーバー側で計算し、LLMには要約(数値)だけを返す。生の配列は
  `capture_waveform` の責務であり、ここでは決して返さない(トークン浪費の防止)
- FFTの分解能は 1/(点数 × サンプル間隔) が上限。画面データは間引きされている
  ことがあるため、周波数の確度は `frequency_resolution_hz` を添えて判断させる
"""

from __future__ import annotations

import cmath
import math
import statistics

from ..config import Config
from ..driver.scope import ScopeDriver, normalize_channel
from ..errors import ErrorCode, ScopeError
from .waveform import NOTE, read_samples

#: `analyses` に指定できる解析名(返却キーもこの名前)
VALID_ANALYSES = ("stats", "fft")

#: FFTピークの既定本数
FFT_TOP_N = 5

WINDOW = "hann"


def waveform_stats(samples: list[float]) -> dict:
    """振幅方向の基本統計(V)。std_v は母標準偏差。"""
    if not samples:
        raise ScopeError(
            ErrorCode.INVALID_PARAMETER, "samples must not be empty", {"points": 0}
        )
    mean = statistics.fmean(samples)
    lowest, highest = min(samples), max(samples)
    return {
        "min_v": lowest,
        "max_v": highest,
        "mean_v": mean,
        "rms_v": math.sqrt(math.fsum(value * value for value in samples) / len(samples)),
        "std_v": statistics.pstdev(samples, mu=mean),
        "vpp_v": highest - lowest,
    }


def _fft(values: list[complex]) -> list[complex]:
    """反復radix-2 Cooley-Tukey(入力長は2の冪であること)。"""
    # ponytail: 純Python FFT(131k点≈0.15s実測)。解析が重くなったらnumpy optional extras化
    size = len(values)
    data = list(values)
    # ビット反転置換
    target = 0
    for index in range(1, size):
        bit = size >> 1
        while target & bit:
            target ^= bit
            bit >>= 1
        target |= bit
        if index < target:
            data[index], data[target] = data[target], data[index]
    length = 2
    while length <= size:
        step = cmath.exp(-2j * cmath.pi / length)
        half = length // 2
        for start in range(0, size, length):
            twiddle = 1 + 0j
            for k in range(start, start + half):
                upper = data[k]
                lower = data[k + half] * twiddle
                data[k] = upper + lower
                data[k + half] = upper - lower
                twiddle *= step
        length *= 2
    return data


def waveform_fft(
    samples: list[float], sample_interval_s: float, top_n: int = FFT_TOP_N
) -> dict:
    """Hann窓つき単側振幅スペクトルのピークを返す。

    2の冪へゼロパディングしてビン間隔を細かくするが、真の分解能は元の
    レコード長で決まる `frequency_resolution_hz` のまま(見かけの精度に騙されない
    ための注意値としてそのまま返す)。
    直流はピーク探索から除く。平均を引いてから窓を掛けるのは、1レコード=数周期
    しかない波形で直流の窓漏れが信号ビンを覆い隠すのを防ぐため。
    """
    if len(samples) < 2:
        raise ScopeError(
            ErrorCode.INVALID_PARAMETER,
            "fft needs 2 or more samples",
            {"points": len(samples)},
        )
    if sample_interval_s <= 0:
        raise ScopeError(
            ErrorCode.INVALID_PARAMETER,
            f"sample_interval_s must be positive: {sample_interval_s}",
            {"sample_interval_s": sample_interval_s},
        )

    points = len(samples)
    mean = statistics.fmean(samples)
    size = 1 << (points - 1).bit_length()
    windowed = [
        complex(
            (0.5 - 0.5 * math.cos(2.0 * math.pi * i / points)) * (samples[i] - mean), 0.0
        )
        for i in range(points)
    ]
    spectrum = _fft(windowed + [0j] * (size - points))

    # Hannのコヒーレントゲイン0.5補正(1/(N*0.5))と単側スペクトルの×2
    scale = 4.0 / points
    half = size // 2
    amplitudes = [abs(spectrum[k]) * scale for k in range(half + 1)]
    bin_hz = 1.0 / (size * sample_interval_s)

    peaks = sorted(
        (
            {"frequency_hz": k * bin_hz, "amplitude_v": amplitudes[k]}
            for k in range(1, half)
            if amplitudes[k] > amplitudes[k - 1] and amplitudes[k] >= amplitudes[k + 1]
        ),
        key=lambda peak: peak["amplitude_v"],
        reverse=True,
    )[:top_n]

    return {
        "dominant_frequency_hz": peaks[0]["frequency_hz"] if peaks else None,
        "frequency_resolution_hz": 1.0 / (points * sample_interval_s),
        "window": WINDOW,
        "peaks": peaks,
    }


def _select(analyses: list[str] | None) -> list[str]:
    """解析名を検証する(機器へ触る前に落とすため、呼び出しの先頭で使う)。"""
    if analyses is None:
        return list(VALID_ANALYSES)
    unknown = [name for name in analyses if name not in VALID_ANALYSES]
    if unknown or not analyses:
        raise ScopeError(
            ErrorCode.INVALID_PARAMETER,
            f"Unknown analyses: {', '.join(unknown)}"
            if unknown
            else "analyses must not be empty",
            {"unknown": unknown, "valid": list(VALID_ANALYSES)},
        )
    return analyses


def analyze_waveform(
    driver: ScopeDriver,
    config: Config,
    channel: str,
    analyses: list[str] | None = None,
    max_points: int | None = None,
) -> dict:
    """波形を取得してホスト側で解析し、要約だけを返す(サンプル配列は返さない)。

    `analyses` が None なら全解析。`max_points` の扱いは capture_waveform と同じ。
    """
    selected = _select(analyses)
    samples, preamble, clamped = read_samples(driver, config, channel, max_points)

    result = {
        "channel": normalize_channel(channel),
        "points": len(samples),
        "sample_interval_s": preamble.xincrement,
        "effective_sample_rate_sa_per_s": 1.0 / preamble.xincrement,
        "note": NOTE,
    }
    if clamped:
        result["max_points_clamped"] = True
    if "stats" in selected:
        result["stats"] = waveform_stats(samples)
    if "fft" in selected:
        result["fft"] = waveform_fft(samples, preamble.xincrement)
    return result
