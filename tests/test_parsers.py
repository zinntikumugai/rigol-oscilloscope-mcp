"""driver/parsers.py のテスト(全て純粋関数)。"""

import pytest

from rigol_oscilloscope_mcp.driver.parsers import (
    format_number,
    from_scpi_impedance,
    from_scpi_slope,
    from_scpi_sweep,
    parse_bool,
    parse_coupling,
    parse_nr3,
    to_scpi_impedance,
    to_scpi_slope,
    to_scpi_sweep,
)
from rigol_oscilloscope_mcp.errors import ErrorCode, ScopeError


def _assert_scpi_error(fn, raw) -> None:
    with pytest.raises(ScopeError) as exc:
        fn(raw)
    assert exc.value.code == ErrorCode.SCPI_ERROR
    assert exc.value.detail == {"raw": raw}


def _assert_param_error(fn, value) -> None:
    with pytest.raises(ScopeError) as exc:
        fn(value)
    assert exc.value.code == ErrorCode.INVALID_PARAMETER


# --- parse_nr3 ------------------------------------------------------------


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        # phase0実測の応答形式
        ("2.000000E+00", 2.0),
        ("1.000000E+1", 10.0),  # 非標準の指数部1桁(MHO98実測)
        ("1.0000E+04", 10000.0),
        ("6.8267E-02", 0.068267),
        ("-1.000000E-3", -0.001),
        ("0.000000", 0.0),
        ("128", 128.0),
        ("0", 0.0),
        ("3.268", 3.268),
        ("-0.5", -0.5),
        ("1e-9", 1e-9),
        ("+2.5E+2", 250.0),
    ],
)
def test_parse_nr3_values(raw: str, expected: float) -> None:
    assert parse_nr3(raw) == pytest.approx(expected)


@pytest.mark.parametrize("raw", ["2.000000E+00\n", " 2.000000E+00 ", "2.000000E+00\r\n"])
def test_parse_nr3_strips_whitespace_and_newlines(raw: str) -> None:
    assert parse_nr3(raw) == 2.0


def test_parse_nr3_returns_float_type() -> None:
    assert isinstance(parse_nr3("128"), float)


@pytest.mark.parametrize("raw", ["", "   ", "DC", "1.0V", "nan-ish", "1,000", "--1"])
def test_parse_nr3_rejects_non_numeric(raw: str) -> None:
    _assert_scpi_error(parse_nr3, raw)


# --- parse_bool -----------------------------------------------------------


@pytest.mark.parametrize("raw", ["ON", "on", "On", "1", " ON\n", "1\r\n"])
def test_parse_bool_true(raw: str) -> None:
    assert parse_bool(raw) is True


@pytest.mark.parametrize("raw", ["OFF", "off", "Off", "0", " OFF\n", "0\r\n"])
def test_parse_bool_false(raw: str) -> None:
    assert parse_bool(raw) is False


@pytest.mark.parametrize("raw", ["", "TRUE", "2", "YES", "O N"])
def test_parse_bool_rejects_unknown(raw: str) -> None:
    _assert_scpi_error(parse_bool, raw)


# --- parse_coupling -------------------------------------------------------


@pytest.mark.parametrize(
    ("raw", "expected"),
    [("DC", "DC"), ("dc", "DC"), (" DC\n", "DC"), ("AC", "AC"), ("ac", "AC"), ("GND", "GND"), ("gnd", "GND")],
)
def test_parse_coupling(raw: str, expected: str) -> None:
    assert parse_coupling(raw) == expected


@pytest.mark.parametrize("raw", ["", "DCC", "GROUND", "OMEG"])
def test_parse_coupling_rejects_unknown(raw: str) -> None:
    _assert_scpi_error(parse_coupling, raw)


# --- impedance ------------------------------------------------------------


@pytest.mark.parametrize(
    ("raw", "expected"),
    [("OMEG", "1M"), ("omeg", "1M"), (" OMEG\n", "1M"), ("FIFT", "50"), ("fift", "50"), ("FIFTY", "50")],
)
def test_from_scpi_impedance(raw: str, expected: str) -> None:
    assert from_scpi_impedance(raw) == expected


@pytest.mark.parametrize("raw", ["", "1M", "50", "OHM", "FIF"])
def test_from_scpi_impedance_rejects_unknown(raw: str) -> None:
    _assert_scpi_error(from_scpi_impedance, raw)


@pytest.mark.parametrize(("value", "expected"), [("1M", "OMEG"), ("1m", "OMEG"), ("50", "FIFT"), (" 50 ", "FIFT")])
def test_to_scpi_impedance(value: str, expected: str) -> None:
    assert to_scpi_impedance(value) == expected


@pytest.mark.parametrize("value", ["", "OMEG", "1MOhm", "500", "50Ohm"])
def test_to_scpi_impedance_rejects_unknown(value: str) -> None:
    _assert_param_error(to_scpi_impedance, value)


def test_impedance_roundtrip() -> None:
    for value in ("1M", "50"):
        assert from_scpi_impedance(to_scpi_impedance(value)) == value


# --- slope ----------------------------------------------------------------


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("POS", "rising"),  # phase0実測: MHO98は短形式を返す
        ("POSitive", "rising"),
        ("positive", "rising"),
        (" POS\n", "rising"),
        ("NEG", "falling"),
        ("NEGative", "falling"),
        ("RFAL", "either"),
        ("RFALl", "either"),
        ("rfall", "either"),
    ],
)
def test_from_scpi_slope(raw: str, expected: str) -> None:
    assert from_scpi_slope(raw) == expected


@pytest.mark.parametrize("raw", ["", "PO", "RISING", "BOTH", "POSITIVEX"])
def test_from_scpi_slope_rejects_unknown(raw: str) -> None:
    _assert_scpi_error(from_scpi_slope, raw)


@pytest.mark.parametrize(
    ("value", "expected"),
    [("rising", "POSitive"), ("falling", "NEGative"), ("either", "RFALl"), ("Rising", "POSitive"), (" either ", "RFALl")],
)
def test_to_scpi_slope(value: str, expected: str) -> None:
    assert to_scpi_slope(value) == expected


@pytest.mark.parametrize("value", ["", "POS", "up", "both"])
def test_to_scpi_slope_rejects_unknown(value: str) -> None:
    _assert_param_error(to_scpi_slope, value)


def test_slope_roundtrip() -> None:
    for value in ("rising", "falling", "either"):
        assert from_scpi_slope(to_scpi_slope(value)) == value


# --- sweep ----------------------------------------------------------------


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("AUTO", "auto"),  # phase0実測
        ("auto", "auto"),
        (" AUTO\n", "auto"),
        ("NORM", "normal"),
        ("NORMal", "normal"),
        ("normal", "normal"),
        ("SING", "single"),
        ("SINGle", "single"),
        ("single", "single"),
    ],
)
def test_from_scpi_sweep(raw: str, expected: str) -> None:
    assert from_scpi_sweep(raw) == expected


@pytest.mark.parametrize("raw", ["", "AUT", "NORMALLY", "ONCE"])
def test_from_scpi_sweep_rejects_unknown(raw: str) -> None:
    _assert_scpi_error(from_scpi_sweep, raw)


@pytest.mark.parametrize(
    ("value", "expected"),
    [("auto", "AUTO"), ("normal", "NORMal"), ("single", "SINGle"), ("Auto", "AUTO"), (" single ", "SINGle")],
)
def test_to_scpi_sweep(value: str, expected: str) -> None:
    assert to_scpi_sweep(value) == expected


@pytest.mark.parametrize("value", ["", "AUTO_", "norm", "sing", "once"])
def test_to_scpi_sweep_rejects_unknown(value: str) -> None:
    _assert_param_error(to_scpi_sweep, value)


def test_sweep_roundtrip() -> None:
    for value in ("auto", "normal", "single"):
        assert from_scpi_sweep(to_scpi_sweep(value)) == value


# --- format_number --------------------------------------------------------


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        (0.0002, "0.0002"),
        (3e-4, "0.0003"),
        (3.0, "3.0"),
        (2.0, "2.0"),
        (-0.001, "-0.001"),
        (0.0, "0.0"),
        (1e-9, "1e-09"),
        (5e6, "5000000.0"),
    ],
)
def test_format_number(value: float, expected: str) -> None:
    assert format_number(value) == expected


def test_format_number_accepts_int() -> None:
    assert format_number(3) == "3.0"


@pytest.mark.parametrize("value", [0.0002, 3e-4, 1e-9, 1 / 3, 6.8267e-02, -2.5e-7, 1.7976931348623157e308])
def test_format_number_is_lossless(value: float) -> None:
    """情報落ちしない(round-trip)。"""
    assert float(format_number(value)) == value


@pytest.mark.parametrize("value", [float("nan"), float("inf"), float("-inf")])
def test_format_number_rejects_non_finite(value: float) -> None:
    _assert_param_error(format_number, value)
