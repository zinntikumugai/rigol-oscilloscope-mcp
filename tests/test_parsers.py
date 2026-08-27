"""driver/parsers.py のテスト(全て純粋関数)。"""

import pytest

from rigol_oscilloscope_mcp.driver.decode import profile_enum
from rigol_oscilloscope_mcp.driver.parsers import (
    format_number,
    from_scpi_impedance,
    from_scpi_slope,
    from_scpi_sweep,
    parse_bool,
    parse_coupling,
    parse_eng_number,
    parse_fft_peaks,
    parse_histogram_result,
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


# --------------------------------------------------------------------------
# parse_eng_number(イベントテーブルの時刻列。`-2.47us` のような接尾辞付き)
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("-2.47us", -2.47e-6),
        ("-2.444us", -2.444e-6),
        ("3ms", 3e-3),
        ("100ns", 100e-9),
        ("1.5ps", 1.5e-12),
        ("2s", 2.0),
        ("-0.5", -0.5),
        ("0", 0.0),
        ("  +1.25 ms  ", 1.25e-3),
        ("1.0E-3s", 1.0e-3),
        # マイクロは2種類のグリフが流通する(MICRO SIGN / GREEK SMALL LETTER MU)
        ("-2.47µs", -2.47e-6),
        ("-2.47μs", -2.47e-6),
    ],
)
def test_parse_eng_number(text: str, expected: float) -> None:
    assert parse_eng_number(text) == pytest.approx(expected)


@pytest.mark.parametrize("raw", ["", "abc", "us", "1.2.3us", "5km", "1 2us", None])
def test_parse_eng_number_rejects_garbage(raw) -> None:
    _assert_scpi_error(parse_eng_number, raw)


# --- parse_fft_peaks ------------------------------------------------------


def test_parse_fft_peaks_guide_example() -> None:
    """ガイド3.16.30の返却例をそのまま解釈する。"""
    peaks, warnings = parse_fft_peaks(
        "1,2.50000MHz,-24.98dBV\n"
        "2,3.50000MHz,-27.84dBV\n"
        "5,6.50125MHz,-32.34dBV"
    )

    assert warnings == []
    assert peaks[0] == {
        "index": 1,
        "frequency_hz": 2.5e6,
        "amplitude": -24.98,
        "amplitude_unit": "dBV",
    }
    assert peaks[-1]["frequency_hz"] == pytest.approx(6.50125e6)
    assert peaks[-1]["amplitude"] == pytest.approx(-32.34)


@pytest.mark.parametrize(
    ("text", "expected_hz"),
    [
        ("1,500Hz,1.0Vrms", 500.0),
        ("1,1.5kHz,1.0Vrms", 1500.0),
        ("1,2.50000MHz,1.0Vrms", 2.5e6),
        ("1,1.25GHz,1.0Vrms", 1.25e9),
    ],
)
def test_parse_fft_peaks_frequency_suffixes(text: str, expected_hz: float) -> None:
    peaks, warnings = parse_fft_peaks(text)

    assert warnings == []
    assert peaks[0]["frequency_hz"] == pytest.approx(expected_hz)


def test_parse_fft_peaks_keeps_the_amplitude_unit_verbatim() -> None:
    """接頭辞の無い単位はそのまま保持する。"""
    peaks, _ = parse_fft_peaks("1,1.0kHz,0.25Vrms")

    assert peaks[0]["amplitude"] == pytest.approx(0.25)
    assert peaks[0]["amplitude_unit"] == "Vrms"


@pytest.mark.parametrize(
    ("text", "expected_value", "expected_unit"),
    [
        # 実機実測(MHO98 fw 00.01.00、`:MATH1:FFT:UNIT VRMS`)
        ("1,9.09294kHz,851.6mVrms", 0.8516, "Vrms"),
        ("5,129.047Hz,14.51mVrms", 0.01451, "Vrms"),
        ("1,1.0kHz,12.0uVrms", 12.0e-6, "Vrms"),
        ("1,1.0kHz,12.0µVrms", 12.0e-6, "Vrms"),
        ("1,1.0kHz,1.5kVrms", 1500.0, "Vrms"),
        # dB系の先頭 d は deci接頭辞ではない(値も単位もそのまま)
        ("1,9.09061kHz,-1.373dBV", -1.373, "dBV"),
        ("4,63.0150kHz,-35.15dBV", -35.15, "dBV"),
        ("1,1.0kHz,-10.0dBm", -10.0, "dBm"),
    ],
)
def test_parse_fft_peaks_amplitude_si_prefix(
    text: str, expected_value: float, expected_unit: str
) -> None:
    """振幅もSI接頭辞を換算する(`851.6mVrms` は 0.8516 Vrms)。"""
    peaks, warnings = parse_fft_peaks(text)

    assert warnings == []
    assert peaks[0]["amplitude"] == pytest.approx(expected_value)
    assert peaks[0]["amplitude_unit"] == expected_unit


def test_parse_fft_peaks_falls_open_on_unparsable_lines() -> None:
    """解釈できない行は raw で残し、警告を添える(例外にしない)。"""
    peaks, warnings = parse_fft_peaks("1,2.50000MHz,-24.98dBV\nnonsense\n\n")

    assert peaks[0]["index"] == 1
    assert peaks[1] == {"raw": "nonsense"}
    assert len(peaks) == 2
    assert len(warnings) == 1
    assert "nonsense" in warnings[0]


def test_parse_fft_peaks_empty_response() -> None:
    assert parse_fft_peaks("") == ([], [])


def test_parse_fft_peaks_non_string() -> None:
    peaks, warnings = parse_fft_peaks(None)

    assert peaks == []
    assert len(warnings) == 1


# --- parse_histogram_result -----------------------------------------------

#: 実機実測(MHO98 fw 00.01.00、`:HISTogram:ENABle ON` / source CH2 / VERTical)。
#: 終端は改行1本のみ(**終端の空行は無い**)。
REAL_HISTOGRAM_RESULT = (
    "[Sum:30.37khits, Peaks:234hits, Max:1.562V, Min:-999.9mV, Pk_Pk:2.562V, "
    "Mean:265.1mV, Median:281.2mV, Mode:1.421V, Bin width:15.62mV, "
    "Sigma:6.159mV, meanPlusSigma:0.581421, meanPlus2Sigma:1.000000, "
    "meanPlus3Sigma:1.000000]\n"
)


def test_parse_histogram_result_real_device_capture() -> None:
    """実機の `Label:Value` 列を正規化キーの統計辞書へ。"""
    stats, warnings = parse_histogram_result(REAL_HISTOGRAM_RESULT)

    assert warnings == []
    assert stats["sum"] == pytest.approx(30370.0)
    assert stats["sum_unit"] == "hits"
    assert stats["peaks"] == pytest.approx(234.0)
    assert stats["peaks_unit"] == "hits"
    assert stats["max"] == pytest.approx(1.562)
    assert stats["max_unit"] == "V"
    assert stats["min"] == pytest.approx(-0.9999)
    assert stats["min_unit"] == "V"
    assert stats["pk_pk"] == pytest.approx(2.562)
    assert stats["mean"] == pytest.approx(0.2651)
    assert stats["median"] == pytest.approx(0.2812)
    assert stats["mode"] == pytest.approx(1.421)
    assert stats["bin_width"] == pytest.approx(0.01562)
    assert stats["bin_width_unit"] == "V"
    assert stats["sigma"] == pytest.approx(0.006159)


def test_parse_histogram_result_unitless_values_stay_plain_numbers() -> None:
    """`meanPlusSigma` 系は単位なし。`*_unit` キー自体を付けない。"""
    stats, warnings = parse_histogram_result(REAL_HISTOGRAM_RESULT)

    assert warnings == []
    assert stats["mean_plus_sigma"] == pytest.approx(0.581421)
    assert stats["mean_plus2_sigma"] == pytest.approx(1.0)
    assert stats["mean_plus3_sigma"] == pytest.approx(1.0)
    assert "mean_plus_sigma_unit" not in stats


def test_parse_histogram_result_disabled_empty_list() -> None:
    """無効時の `[]`(driverは送らないが、パーサは黙って空を返す)。"""
    assert parse_histogram_result("[]\n") == ({}, [])


def test_parse_histogram_result_falls_open_on_a_bad_entry() -> None:
    """1項目が壊れても他は返す(例外にしない)。"""
    stats, warnings = parse_histogram_result("[Max:1.5V, nonsense, Min:-1.0V]")

    assert stats["max"] == pytest.approx(1.5)
    assert stats["min"] == pytest.approx(-1.0)
    assert len(warnings) == 1
    assert "nonsense" in warnings[0]


def test_parse_histogram_result_falls_open_on_an_unbracketed_response() -> None:
    stats, warnings = parse_histogram_result("something else")

    assert stats == {}
    assert len(warnings) == 1


def test_parse_histogram_result_non_string() -> None:
    stats, warnings = parse_histogram_result(None)

    assert stats == {}
    assert len(warnings) == 1


# ---------------------------------------------------------------------------
# driver/decode.py の列挙マッチャ(`profile_enum`)
# ---------------------------------------------------------------------------
#
# SCPI規格上、機器は「短形式以上・長形式以下」の任意の略形で応答してよい。
# 実測(MHO98 firmware 00.01.00): ガイド3.20.7 のReturn Formatは `GRE` と
# 書いてあるが、実機は `GREE` を返す。短形/長形の2形だけを見る実装では
# 取りこぼすため、前置一致で受理する。


def _colors() -> object:
    """実機と同じ綴りの色テーブル(プロファイル `reference_colors` と同一)。"""
    _, from_scpi = profile_enum(
        (
            ("gray", "GRAY"),
            ("green", "GREen"),
            ("blue", "BLUE"),
            ("red", "RED"),
            ("orange", "ORANge"),
        )
    )
    return from_scpi


@pytest.mark.parametrize("raw", ["GRE", "GREE", "GREEN", " greE "])
def test_profile_enum_accepts_any_abbreviation_between_the_two_forms(raw: str) -> None:
    """実測: 実機は `GREE` を返す(ガイド記載の `GRE` ではない)。"""
    assert _colors()(raw) == "green"


@pytest.mark.parametrize("raw", ["GR", "GREENS", "PURPLE", ""])
def test_profile_enum_rejects_tokens_outside_the_two_forms(raw: str) -> None:
    with pytest.raises(ScopeError) as error:
        _colors()(raw)
    assert error.value.code is ErrorCode.SCPI_ERROR


def test_profile_enum_prefers_an_exact_form_over_an_ambiguous_prefix() -> None:
    """曖昧な前置一致より、短形/長形の完全一致を優先する。"""
    _, from_scpi = profile_enum((("time", "TIMe"), ("timeout", "TIMeout")))

    # `TIME` は `timeout` の前置でもあるが、`time` の長形式なので一意に読む
    assert from_scpi("TIME") == "time"
    assert from_scpi("TIMEO") == "timeout"  # `timeout` だけに一致する前置

    # 短形式が両者で `TIM` に衝突する表。完全一致でも一意でなければ受理しない
    with pytest.raises(ScopeError) as error:
        from_scpi("TIM")
    assert error.value.code is ErrorCode.SCPI_ERROR


def test_profile_enum_rejects_an_ambiguous_prefix() -> None:
    """複数の値に一致しうる略形は、黙って片方を選ばずSCPIエラーにする。"""
    _, from_scpi = profile_enum((("timer", "TIMer"), ("timeout", "TIMeout")))

    with pytest.raises(ScopeError) as error:
        from_scpi("TIME")
    assert error.value.code is ErrorCode.SCPI_ERROR
