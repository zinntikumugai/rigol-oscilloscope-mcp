"""ホスト側の波形解析。

規範: tools.md 5章 `analyze_waveform`。pulse / noise / thd / jitter は issue #73 で追加。

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
from ..driver.scope import ScopeDriver
from ..errors import ErrorCode, ScopeError
from .waveform import NOTE, math_operator, read_samples, source_name

#: `analyses` に指定できる解析名(返却キーもこの名前)。pulse / noise / thd / jitter
#: は issue #73 で追加した
VALID_ANALYSES = ("stats", "fft", "pulse", "noise", "thd", "jitter")
#: `analyses` を省略したときの解析(従来どおり。追加分は明示したときだけ計算する)
DEFAULT_ANALYSES = ("stats", "fft")
#: パルス解析でトップ/ベースを求めるヒストグラムのビン数
_PULSE_BINS = 256
#: THD で数える高調波の次数の上限
THD_MAX_ORDER = 10
#: THD を計算する記録内の周期数の下限(これ未満はトーンが重なる)
THD_MIN_FUNDAMENTAL_BIN = 12

#: FFTピークの既定本数
FFT_TOP_N = 5

WINDOW = "hann"


def _check_samples(samples: list[float]) -> None:
    if not samples:
        raise ScopeError(
            ErrorCode.INVALID_PARAMETER, "samples must not be empty", {"points": 0}
        )


def waveform_stats(samples: list[float]) -> dict:
    """振幅方向の基本統計(V)。std_v は母標準偏差。"""
    _check_samples(samples)
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


def _histogram(samples: list[float]) -> tuple[list[int], float, float]:
    """`_PULSE_BINS` 本のヒストグラムと、最小値・ビン幅。"""
    lowest = min(samples)
    width = (max(samples) - lowest) / _PULSE_BINS
    counts = [0] * _PULSE_BINS
    for value in samples:
        counts[min(int((value - lowest) / width), _PULSE_BINS - 1)] += 1
    return counts, lowest, width


def _otsu_split(counts: list[int]) -> int:
    """2群に分けたときの群間分散が最大になる境界のビン番号(大津の方法)。

    パーセンタイルの中点と違い、デューティの小さいパルスでも上下を分けられ、
    まばらなスパイクは群の重みが小さいので境界を動かさない。
    """
    total = sum(counts)
    weighted = math.fsum(k * c for k, c in enumerate(counts))
    best, best_k = -1.0, 1
    below = 0
    below_weighted = 0.0
    for k in range(1, len(counts)):
        below += counts[k - 1]
        below_weighted += (k - 1) * counts[k - 1]
        above = total - below
        if below == 0 or above == 0:
            continue
        diff = below_weighted / below - (weighted - below_weighted) / above
        score = below * above * diff * diff
        if score > best:
            best, best_k = score, k
    return best_k


def waveform_pulse(samples: list[float]) -> dict:
    """矩形波・パルスのオーバーシュート/アンダーシュート(%)。

    トップとベースはヒストグラムの最頻値(大津の方法で分けた上/下で別々に)。
    オーバーシュート = (最大 − トップ) / 振幅、アンダーシュート = (ベース − 最小) /
    振幅(記録のどこで起きたかは問わない)。振幅がほぼ無い(平坦な)信号では計算しない。
    """
    _check_samples(samples)
    lowest, highest = min(samples), max(samples)
    span = highest - lowest
    empty = {"top_v": None, "base_v": None, "amplitude_v": None,
             "overshoot_percent": None, "undershoot_percent": None}
    if span <= 1e-12:
        return {**empty, "warning": "the signal is flat; no pulse to analyze"}
    counts, lowest, width = _histogram(samples)
    middle = _otsu_split(counts)
    upper = max(range(middle, _PULSE_BINS), key=lambda k: counts[k])
    lower = max(range(middle), key=lambda k: counts[k])
    top = lowest + (upper + 0.5) * width
    base = lowest + (lower + 0.5) * width
    amplitude = top - base
    if amplitude <= span * 0.05:
        return {**empty, "warning": "no clear top and base levels (not a pulse-like signal)"}
    return {
        "top_v": top,
        "base_v": base,
        "amplitude_v": amplitude,
        "overshoot_percent": max(0.0, (highest - top) / amplitude * 100.0),
        "undershoot_percent": max(0.0, (base - lowest) / amplitude * 100.0),
    }


def waveform_noise(samples: list[float]) -> dict:
    """白色雑音の実効値の推定(V)。隣接サンプルの差の**中央絶対偏差**から求める。

    隣接差には信号そのものの変化がほとんど残らない(サンプル間隔に比べて遅い
    変化の場合)ので、波形の形に左右されずに雑音を取り出せる。矩形波のエッジの
    ような**まばらな**大きい段差は、標準偏差だと推定を大きく狂わせる(実機で
    踏んだ)が、中央絶対偏差なら無視される。σ_diff = 1.4826 × MAD、雑音 =
    σ_diff / √2。エッジが記録の大半を占めるほど速い信号では過大に出る。
    """
    if len(samples) < 3:
        raise ScopeError(
            ErrorCode.INVALID_PARAMETER, "noise needs 3 or more samples", {"points": len(samples)}
        )
    diffs = [b - a for a, b in zip(samples, samples[1:])]
    center = statistics.median(diffs)
    mad = statistics.median(abs(d - center) for d in diffs)
    return {
        "noise_rms_v": 1.4826 * mad / math.sqrt(2.0),
        "method": "first_difference_mad",
    }


def _hann_amplitudes(samples: list[float], sample_interval_s: float) -> tuple[list[float], float]:
    """Hann窓つき単側振幅スペクトル(V)とビン間隔(Hz)。2のべき乗へ0詰めする。"""
    points = len(samples)
    mean = statistics.fmean(samples)
    size = 1 << (points - 1).bit_length()
    windowed = [
        complex((0.5 - 0.5 * math.cos(2.0 * math.pi * i / points)) * (samples[i] - mean), 0.0)
        for i in range(points)
    ]
    spectrum = _fft(windowed + [0j] * (size - points))
    scale = 4.0 / points
    amplitudes = [abs(spectrum[k]) * scale for k in range(size // 2 + 1)]
    return amplitudes, 1.0 / (size * sample_interval_s)


def _tone_amplitude(amplitudes: list[float], center: int, spread: int, padding: float) -> float:
    """center±spread のビンの電力を足して振幅へ。

    Hann の等価雑音帯域 1.5 と、0詰めの倍率 `padding`(= 変換長 / 記録長。0詰めで
    ビンが細かくなるぶん、1つのトーンの電力が多くのビンに分かれる)で割る。
    ピークがビンの中央に無いときの振幅の目減り(スカラッピング)に左右されない。
    """
    window = amplitudes[max(1, center - spread): center + spread + 1]
    return math.sqrt(math.fsum(a * a for a in window) / (1.5 * padding))


def _tone_frequency(amplitudes: list[float], center: int, spread: int, bin_hz: float) -> float:
    """center±spread のビンの電力の重心(Hz)。ビン間隔より細かく周波数を求める。"""
    window = range(max(1, center - spread), min(len(amplitudes), center + spread + 1))
    power = [amplitudes[k] ** 2 for k in window]
    total = math.fsum(power)
    if total <= 0:
        return center * bin_hz
    return math.fsum(k * p for k, p in zip(window, power)) / total * bin_hz


def _check_interval(sample_interval_s: float) -> None:
    """サンプル間隔は正の数(`waveform_fft` と同じ検証)。"""
    if sample_interval_s <= 0:
        raise ScopeError(
            ErrorCode.INVALID_PARAMETER,
            f"sample_interval_s must be positive: {sample_interval_s}",
            {"sample_interval_s": sample_interval_s},
        )


def waveform_thd(samples: list[float], sample_interval_s: float) -> dict:
    """全高調波歪み(%)= √(Σ 高調波²) / 基本波。基本波は最大のピーク。

    各トーンの振幅は周囲のビンの電力の和から求める(ピークがビン中央に無くても
    比が崩れない)。ナイキスト周波数を超える次数は数えない。
    """
    if len(samples) < 16:
        raise ScopeError(
            ErrorCode.INVALID_PARAMETER, "thd needs 16 or more samples", {"points": len(samples)}
        )
    _check_interval(sample_interval_s)
    amplitudes, bin_hz = _hann_amplitudes(samples, sample_interval_s)
    padding = 2 * (len(amplitudes) - 1) / len(samples)
    peak = max(range(1, len(amplitudes) - 1), key=lambda k: amplitudes[k])
    # 窓の電力が数ビンに広がるので、ピークの両側3ビン(0詰めで細かくなったぶん
    # 広げる)を1つのトーンとみなす
    spread = math.ceil(3 * padding)
    cycles = peak / padding
    # 基本波のビンが低すぎる(記録に入っている周期が少ない)と、高調波の探索範囲が
    # 基本波と重なり、基本波を高調波として数えてしまう(実機で踏んだ: 2周期で
    # THD 183%)。トーン同士が重ならない程度に周期が入っているときだけ計算する
    if cycles < THD_MIN_FUNDAMENTAL_BIN:
        return {
            "fundamental_hz": _tone_frequency(amplitudes, peak, spread, bin_hz),
            "fundamental_v": None,
            "thd_percent": None,
            "harmonics": [],
            "warning": (
                "too few cycles in the record for THD; capture at least about "
                f"{THD_MIN_FUNDAMENTAL_BIN} cycles (use a slower timebase)"
            ),
        }
    fundamental = _tone_amplitude(amplitudes, peak, spread, padding)
    harmonics = []
    for order in range(2, THD_MAX_ORDER + 1):
        center = round(peak * order)
        if center + spread >= len(amplitudes):
            break
        # 基本波の推定誤差が次数倍に広がるので、予想位置の近くで最大のビンを使う。
        # 探索幅は隣の高調波に届かない範囲(基本波のビン数の1/4)に抑える
        reach = min(math.ceil(order * padding), max(1, peak // 4))
        search = range(max(1, center - reach), min(len(amplitudes) - 1, center + reach + 1))
        best = max(search, key=lambda k: amplitudes[k])
        harmonics.append(
            {
                "order": order,
                "frequency_hz": _tone_frequency(amplitudes, best, spread, bin_hz),
                "amplitude_v": _tone_amplitude(amplitudes, best, spread, padding),
            }
        )
    distortion = math.sqrt(math.fsum(h["amplitude_v"] ** 2 for h in harmonics))
    return {
        "fundamental_hz": _tone_frequency(amplitudes, peak, spread, bin_hz),
        "fundamental_v": fundamental,
        "thd_percent": distortion / fundamental * 100.0 if fundamental > 0 else None,
        "harmonics": harmonics,
    }


def waveform_jitter(samples: list[float], sample_interval_s: float) -> dict:
    """周期ジッタ(立ち上がりの間隔のばらつき)。

    しきい値は大津の方法で分けた上下の平均の中点(スパイクに動かされない)。
    振幅の10%のヒステリシスで立ち上がりを1回ずつ数え、交差時刻を線形補間で求め、
    周期の平均・標準偏差(周期ジッタ)・最大−最小と、隣り合う周期の差の実効値
    (サイクル間ジッタ)を返す。立ち上がりが3つ未満なら計算しない。
    """
    _check_samples(samples)
    _check_interval(sample_interval_s)
    if max(samples) > min(samples):
        counts, lowest, width = _histogram(samples)
        boundary = lowest + _otsu_split(counts) * width
    else:
        boundary = samples[0]
    below = [v for v in samples if v < boundary] or samples
    above = [v for v in samples if v >= boundary] or samples
    low_level, high_level = statistics.fmean(below), statistics.fmean(above)
    threshold = (low_level + high_level) / 2.0
    # しきい値付近の雑音で余計に数えないよう、振幅の10%のヒステリシスを持たせる:
    # しきい値−h を下回ってから、最初にしきい値を上へ横切った時刻を1つだけ数える
    hysteresis = 0.1 * (high_level - low_level)
    crossings = []
    armed = False
    for index in range(1, len(samples)):
        if samples[index - 1] < threshold - hysteresis:
            armed = True
        if armed and samples[index - 1] < threshold <= samples[index]:
            fraction = (threshold - samples[index - 1]) / (samples[index] - samples[index - 1])
            crossings.append((index - 1 + fraction) * sample_interval_s)
            armed = False
    periods = [b - a for a, b in zip(crossings, crossings[1:])]
    if len(periods) < 2:
        return {
            "cycles": len(periods),
            "threshold_v": threshold,
            "period_mean_s": None,
            "period_jitter_rms_s": None,
            "period_jitter_pk_pk_s": None,
            "cycle_to_cycle_jitter_rms_s": None,
            "warning": "fewer than 3 rising edges in the record; capture more cycles",
        }
    steps = [b - a for a, b in zip(periods, periods[1:])]
    return {
        "cycles": len(periods),
        "threshold_v": threshold,
        "period_mean_s": statistics.fmean(periods),
        "period_jitter_rms_s": statistics.pstdev(periods),
        "period_jitter_pk_pk_s": max(periods) - min(periods),
        "cycle_to_cycle_jitter_rms_s": math.sqrt(math.fsum(d * d for d in steps) / len(steps)),
    }


def _select(analyses: list[str] | None) -> list[str]:
    """解析名を検証する(機器へ触る前に落とすため、呼び出しの先頭で使う)。"""
    if analyses is None:
        return list(DEFAULT_ANALYSES)
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

    `analyses` が None なら `DEFAULT_ANALYSES`(stats / fft)だけ。`max_points` の扱いは capture_waveform と同じ。

    MATHソースのうちFFT演算のトレースは横軸が周波数のため、時間軸前提の統計も
    ホスト側FFTも意味を持たない。取得前に `INVALID_PARAMETER` で拒否する。
    """
    selected = _select(analyses)
    operator = math_operator(driver, channel)
    if operator == "fft":
        raise ScopeError(
            ErrorCode.INVALID_PARAMETER,
            f"{channel} is an FFT trace, whose x axis is frequency: host-side "
            "statistics and FFT do not apply. Use get_math_state for the "
            "instrument's peak table, or capture_waveform to fetch the spectrum "
            "points.",
            {"channel": channel, "operator": operator},
        )
    samples, preamble, clamped = read_samples(driver, config, channel, max_points)

    result = {
        "channel": source_name(channel, operator),
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
    if "pulse" in selected:
        result["pulse"] = waveform_pulse(samples)
    if "noise" in selected:
        result["noise"] = waveform_noise(samples)
    if "thd" in selected:
        result["thd"] = waveform_thd(samples, preamble.xincrement)
    if "jitter" in selected:
        result["jitter"] = waveform_jitter(samples, preamble.xincrement)
    return result
