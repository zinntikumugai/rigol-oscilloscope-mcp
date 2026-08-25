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
