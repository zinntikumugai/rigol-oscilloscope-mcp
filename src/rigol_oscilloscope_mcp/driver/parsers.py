"""SCPI応答値 ⇔ 内部表現の変換(全て純粋関数)。

機器応答は短形式(`POS`)/長形式(`POSitive`)のどちらも返り得るため、
`from_scpi_*` は両方を受理する(phase0実測のMHO98は短形式を返す)。
"""

from __future__ import annotations

import math
import re

from ..errors import ErrorCode, ScopeError

# (短形式, 長形式, 内部表現)
_IMPEDANCE: tuple[tuple[str, str, str], ...] = (
    ("OMEG", "OMEG", "1M"),
    ("FIFT", "FIFTY", "50"),
)
_SLOPE: tuple[tuple[str, str, str], ...] = (
    ("POS", "POSITIVE", "rising"),
    ("NEG", "NEGATIVE", "falling"),
    ("RFAL", "RFALL", "either"),
)
_SWEEP: tuple[tuple[str, str, str], ...] = (
    ("AUTO", "AUTO", "auto"),
    ("NORM", "NORMAL", "normal"),
    ("SING", "SINGLE", "single"),
)

# 送信時に用いる表記(SCPI標準の大文字=短形式+小文字=省略可能部)
_TO_SCPI_IMPEDANCE = {"1M": "OMEG", "50": "FIFT"}
_TO_SCPI_SLOPE = {"rising": "POSitive", "falling": "NEGative", "either": "RFALl"}
_TO_SCPI_SWEEP = {"auto": "AUTO", "normal": "NORMal", "single": "SINGle"}


def _scpi_error(raw: object, message: str) -> ScopeError:
    return ScopeError(ErrorCode.SCPI_ERROR, message, {"raw": raw})


def _invalid_parameter(value: object, message: str) -> ScopeError:
    return ScopeError(ErrorCode.INVALID_PARAMETER, message, {"value": value})


def _from_scpi(
    text: str, table: tuple[tuple[str, str, str], ...], what: str
) -> str:
    """短形式/長形式のどちらでも受理して内部表現へ変換する。"""
    if not isinstance(text, str):
        raise _scpi_error(text, f"{what} response is not a string")
    token = text.strip().upper()
    for short, long, value in table:
        if token in (short, long):
            return value
    raise _scpi_error(text, f"cannot interpret {what} response: {text!r}")


def _to_scpi(value: str, table: dict[str, str], what: str, *, upper: bool) -> str:
    if not isinstance(value, str):
        raise _invalid_parameter(value, f"{what} is not a string")
    key = value.strip()
    key = key.upper() if upper else key.lower()
    try:
        return table[key]
    except KeyError:
        raise _invalid_parameter(
            value, f"invalid {what} value: {value!r} (allowed: {sorted(table)})"
        ) from None


def parse_nr3(text: str) -> float:
    """NR3数値応答をfloatへ変換する(前後空白・改行を除去)。

    MHO98は指数部1桁の非標準形式(`1.000000E+1`)も返すが、`float()` がそのまま
    受理するため特別扱いは不要。
    """
    if not isinstance(text, str):
        raise _scpi_error(text, "numeric response is not a string")
    try:
        return float(text.strip())
    except ValueError:
        raise _scpi_error(text, f"cannot interpret numeric response: {text!r}") from None


#: 工学接尾辞 → 秒(イベントテーブルの時刻列はNR3ではなく `-2.47us` の形)。
#: マイクロは MICRO SIGN(U+00B5)と GREEK SMALL LETTER MU(U+03BC)の両方が流通する。
_ENG_SUFFIXES = {
    "": 1.0,
    "s": 1.0,
    "ms": 1e-3,
    "us": 1e-6,
    "µs": 1e-6,
    "μs": 1e-6,
    "ns": 1e-9,
    "ps": 1e-12,
}

_ENG_RE = re.compile(r"^([+-]?(?:\d+\.?\d*|\.\d+)(?:[eE][+-]?\d+)?)\s*(\D*)$")


def parse_eng_number(text: str) -> float:
    """`-2.47us` → `-2.47e-06`(接尾辞なしはそのままの数値)。

    `:BUS<n>:DATA?` のイベントテーブルはNR3ではなく単位付きの表記を返す
    (MHO900/DHO800/900プログラミングガイドの例: `-2.47us` / `-2.444us`)。
    """
    match = _ENG_RE.match(text.strip()) if isinstance(text, str) else None
    scale = _ENG_SUFFIXES.get(match.group(2).strip().lower()) if match else None
    if scale is None:
        raise _scpi_error(text, f"cannot interpret engineering number: {text!r}")
    return float(match.group(1)) * scale


#: FFTピーク表の周波数サフィックス(ガイド3.16.30の返却例は MHz)
_FREQ_SUFFIXES = {"hz": 1.0, "khz": 1e3, "mhz": 1e6, "ghz": 1e9}

#: 振幅列のSI接頭辞(実機実測: `:FFT:UNIT VRMS` は `851.6mVrms` と接頭辞付きで返る)。
#: **大文字小文字を区別する**(`m`=ミリ / `M`=メガ)。`d`(デシ)は意図的に持たない
#: — `dBV` / `dBm` の先頭 `d` を接頭辞と誤読しないため。
_AMPLITUDE_PREFIXES = {
    "p": 1e-12, "n": 1e-9, "u": 1e-6, "µ": 1e-6, "μ": 1e-6,
    "m": 1e-3, "k": 1e3, "K": 1e3, "M": 1e6, "G": 1e9,
}

#: `5,6.50125MHz,-32.34dBV` の1行。振幅の単位は接頭辞を外して保持する
#: (`:MATH<n>:FFT:UNIT` 依存で全集合が未検証のため、単位本体は列挙しない)。
_FFT_PEAK_RE = re.compile(
    r"^(\d+)\s*,\s*([+-]?[\d.]+(?:[eE][+-]?\d+)?)\s*([A-Za-z]*)\s*,\s*"
    r"([+-]?[\d.]+(?:[eE][+-]?\d+)?)\s*([A-Za-zµμ]*)$"
)


def _amplitude(value: float, unit: str) -> tuple[float, str]:
    """`(851.6, "mVrms")` → `(0.8516, "Vrms")`。dB系と接頭辞なしはそのまま。"""
    if unit[:2].lower() == "db":  # dBV / dBm: 先頭の d はデシ接頭辞ではない
        return value, unit
    scale = _AMPLITUDE_PREFIXES.get(unit[:1]) if len(unit) > 1 else None
    return (value, unit) if scale is None else (value * scale, unit[1:])


def parse_fft_peaks(text: object) -> tuple[list[dict], list[str]]:
    """FFTピーク探索結果表を `(行, 警告)` へ(ガイド3.16.30)。

    行は `{"index": 5, "frequency_hz": 6501250.0, "amplitude": -32.34,
    "amplitude_unit": "dBV"}`。`parse_event_table` と同じく **戻り値はタプル**で、
    解釈できない行は `{"raw": "<元の行>"}` として残し、対応する説明を警告リストへ
    積む(**例外は投げない** — ピーク表は付加情報であり、1行の想定外で
    `get_math_state` 全体を落とさない)。

    実機MHO98(fw 00.01.00)の区切りは**改行**で、末尾に終端の空行が1本つく
    (読み出しは `Transport.query_lines`)。念のため `;` 区切りも受理する。

    周波数・振幅の両列ともSI接頭辞を換算する(`851.6mVrms` → 0.8516 `Vrms`)。
    ただし `dBV` / `dBm` の先頭 `d` は接頭辞ではないため換算しない。
    """
    if not isinstance(text, str):
        return [], [f"peak search response is not a string: {text!r}"]

    peaks: list[dict] = []
    warnings: list[str] = []
    for line in re.split(r"[\r\n;]+", text):
        line = line.strip()
        if not line:
            continue
        match = _FFT_PEAK_RE.match(line)
        scale = (
            _FREQ_SUFFIXES.get(match.group(3).lower() or "hz") if match else None
        )
        if match is None or scale is None:
            peaks.append({"raw": line})
            warnings.append(f"cannot interpret peak search line: {line!r}")
            continue
        amplitude, unit = _amplitude(float(match.group(4)), match.group(5))
        peaks.append(
            {
                "index": int(match.group(1)),
                "frequency_hz": float(match.group(2)) * scale,
                "amplitude": amplitude,
                "amplitude_unit": unit,
            }
        )
    return peaks, warnings


def parse_histogram_result(text: object) -> tuple[list[list[str]], list[str]]:
    """ヒストグラム統計表を `(行, 警告)` へ(ガイド3.11.9)。

    **ガイド本文はページ欠落で書式が不明**。同種の
    `:MEASure:HISTogram:STATistics:RESult?`(3.17.32)の実例は
    `[["92","1","0","Vpp",...]]` という引用符付き文字列の入れ子リストで、
    単位とSI接頭辞が文字列に埋まっている。**列の意味が確定できないため列名は
    付けず**、引用符で括られたセルを行ごとに切り出すだけに留める。

    `parse_fft_peaks` と同じく **例外は投げない**(fail-open)。解釈できなければ
    空リストと警告を返し、生応答の保持は呼び出し側の責務。**要実機検証**。
    """
    if not isinstance(text, str):
        return [], [f"histogram statistics response is not a string: {text!r}"]
    rows = [
        cells
        for group in re.findall(r"\[([^\[\]]*)\]", text)
        if (cells := re.findall(r'"([^"]*)"', group))
    ]
    if not rows:
        return [], [f"cannot interpret histogram statistics response: {text!r}"]
    return rows, []


def parse_bool(text: str) -> bool:
    """`ON`/`1` → True、`OFF`/`0` → False(大文字小文字不問)。"""
    if not isinstance(text, str):
        raise _scpi_error(text, "boolean response is not a string")
    token = text.strip().upper()
    if token in ("ON", "1"):
        return True
    if token in ("OFF", "0"):
        return False
    raise _scpi_error(text, f"cannot interpret boolean response: {text!r}")


def parse_coupling(text: str) -> str:
    """`DC`/`AC`/`GND` を正規化(大文字化)して返す。"""
    if not isinstance(text, str):
        raise _scpi_error(text, "coupling response is not a string")
    token = text.strip().upper()
    if token in ("DC", "AC", "GND"):
        return token
    raise _scpi_error(text, f"cannot interpret coupling response: {text!r}")


def from_scpi_impedance(text: str) -> str:
    """`OMEG` → `1M`、`FIFT` → `50`。"""
    return _from_scpi(text, _IMPEDANCE, "input impedance")


def to_scpi_impedance(value: str) -> str:
    """`1M` → `OMEG`、`50` → `FIFT`。"""
    return _to_scpi(value, _TO_SCPI_IMPEDANCE, "input impedance", upper=True)


def from_scpi_slope(text: str) -> str:
    """`POS` → `rising`、`NEG` → `falling`、`RFAL` → `either`。"""
    return _from_scpi(text, _SLOPE, "trigger slope")


def to_scpi_slope(value: str) -> str:
    """`rising` → `POSitive`、`falling` → `NEGative`、`either` → `RFALl`。"""
    return _to_scpi(value, _TO_SCPI_SLOPE, "trigger slope", upper=False)


def from_scpi_sweep(text: str) -> str:
    """`AUTO` → `auto`、`NORM` → `normal`、`SING` → `single`。"""
    return _from_scpi(text, _SWEEP, "trigger sweep mode")


def to_scpi_sweep(value: str) -> str:
    """`auto` → `AUTO`、`normal` → `NORMal`、`single` → `SINGle`。"""
    return _to_scpi(value, _TO_SCPI_SWEEP, "trigger sweep mode", upper=False)


def format_number(value: float) -> str:
    """SCPI送信用の数値文字列(round-tripで情報落ちしない表記)。"""
    try:
        number = float(value)
    except (TypeError, ValueError):
        raise _invalid_parameter(value, f"cannot convert to a number: {value!r}") from None
    if not math.isfinite(number):
        raise _invalid_parameter(value, f"not a finite number: {value!r}")
    return repr(number)
