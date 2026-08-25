"""波形取得(生BYTE → 物理量V の変換と、大規模データの受け渡し)。

規範: tools.md 5章 `capture_waveform` / docs/verification/mho98-phase0.md。

- 電圧変換はプリアンブルの yorigin / yreference / yincrement に従いサーバー側で
  実施し、LLMには物理量(V)のみを返す
- 小規模はレスポンスへ直接、大規模は一時ファイル(CSV)へ退避してパスを返す
  (巨大配列でMCPレスポンスを膨らませない)
- 画面表示データは間引きされている(実測: 表示500 kSa/s vs 実5 MSa/s)ため、
  実効サンプルレートを `xincrement` の逆数としてメタデータに含める
"""

from __future__ import annotations

import os
import tempfile

from ..config import Config
from ..driver.scope import _CHANNEL_RE, ScopeDriver
from ..errors import ErrorCode, ScopeError

# これ以下の点数はレスポンスにサンプル配列を直接含める
INLINE_POINTS_LIMIT = 10000

NOTE = (
    "画面表示データは間引きされている場合があります"
    "(実効レートは sample_interval_s の逆数)。"
)

FILE_PREFIX = "rigol_waveform_"
FILE_SUFFIX = ".csv"
CSV_HEADER = "time_s,volts"


def _channel_label(channel: str) -> str:
    """`CHANnel1` / `1` → `CH1`。解釈できない値の検証はドライバに委ねる。"""
    match = _CHANNEL_RE.match(channel.strip()) if isinstance(channel, str) else None
    return f"CH{int(match.group(1))}" if match else channel


def _write_csv(times: list[float], volts: list[float]) -> str:
    """一時ファイルへCSVを書き出し、絶対パスを返す(削除は呼び出し側の責務)。"""
    fd, path = tempfile.mkstemp(prefix=FILE_PREFIX, suffix=FILE_SUFFIX)
    with os.fdopen(fd, "w", encoding="utf-8") as fp:
        fp.write(CSV_HEADER + "\n")
        for time_s, value in zip(times, volts, strict=True):
            fp.write(f"{time_s},{value}\n")
    return path


def capture_waveform(
    driver: ScopeDriver,
    config: Config,
    channel: str,
    max_points: int | None = None,
) -> dict:
    """波形を取得して物理量(V)へ変換する。

    `max_points` が None なら設定値を使う。点数が INLINE_POINTS_LIMIT 以下なら
    `samples_v` を直接返し、超える場合は一時ファイル(CSV)のパスを返す。
    一時ファイルの削除は呼び出し側の責務(サーバーは消さない)。
    """
    limit = config.waveform_max_points if max_points is None else max_points
    if limit <= 0:
        raise ScopeError(
            ErrorCode.INVALID_PARAMETER,
            f"max_points は1以上である必要があります: {limit}",
            {"max_points": limit},
        )

    raw = driver.read_waveform(channel, limit)
    preamble = raw.preamble
    samples = [
        (value - preamble.yorigin - preamble.yreference) * preamble.yincrement
        for value in raw.data
    ]

    result = {
        "channel": _channel_label(channel),
        "points": len(samples),
        "sample_interval_s": preamble.xincrement,
        "time_origin_s": preamble.xorigin,
        "effective_sample_rate_sa_per_s": 1.0 / preamble.xincrement,
        "note": NOTE,
    }

    if len(samples) <= INLINE_POINTS_LIMIT:
        result["samples_v"] = samples
    else:
        times = [preamble.xorigin + i * preamble.xincrement for i in range(len(samples))]
        result["data_file"] = _write_csv(times, samples)
        result["data_format"] = "csv"
    return result
