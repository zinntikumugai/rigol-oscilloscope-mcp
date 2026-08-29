"""testing/fake_scope.py と testing/fake_transport.py のテスト。

phase0実測(docs/verification/mho98-phase0.md)の挙動を、フェイク機器が
忠実に再現していることを検証する。
"""

import pytest

from rigol_oscilloscope_mcp.errors import ErrorCode, ScopeError
from rigol_oscilloscope_mcp.testing import FakeScope, FakeTransport, SilentTimeout
from rigol_oscilloscope_mcp.transport import Transport

NO_ERROR = '0,"No error"'
COMMAND_ERROR = '-100,"Command err"'
OUT_OF_RANGE = '-222,"Data out of range"'
EXECUTE_ERROR = '-200,"Command execute failed"'


@pytest.fixture
def scope() -> FakeScope:
    return FakeScope()


@pytest.fixture
def transport(scope: FakeScope) -> FakeTransport:
    t = FakeTransport(scope)
    t.open()
    return t


# --------------------------------------------------------------------------
# 識別・システム
# --------------------------------------------------------------------------


def test_idn(scope: FakeScope) -> None:
    assert scope.handle("*IDN?") == b"RIGOL TECHNOLOGIES,MHO98,FAKE0000000001,00.01.00"


def test_idn_is_case_insensitive(scope: FakeScope) -> None:
    assert scope.handle("*idn?") == scope.handle("*IDN?")


def test_error_queue_empty_by_default(scope: FakeScope) -> None:
    assert scope.handle(":SYSTem:ERRor?") == NO_ERROR.encode()
    assert scope.handle(":SYST:ERR?") == NO_ERROR.encode()


def test_stale_error_queue_is_drained_once() -> None:
    scope = FakeScope(stale_error_queue=True)
    assert scope.handle(":SYSTem:ERRor?") == OUT_OF_RANGE.encode()
    assert scope.handle(":SYSTem:ERRor?") == NO_ERROR.encode()


# --------------------------------------------------------------------------
# 未知・不正ニモニック(実機は無応答+エラーキュー汚染)
# --------------------------------------------------------------------------


def test_unknown_query_is_silent_and_queues_command_error(scope: FakeScope) -> None:
    with pytest.raises(SilentTimeout):
        scope.handle(":FOO:BAR?")
    assert scope.handle(":SYSTem:ERRor?") == COMMAND_ERROR.encode()
    assert scope.handle(":SYSTem:ERRor?") == NO_ERROR.encode()


def test_unknown_channel_number_is_silent(scope: FakeScope) -> None:
    with pytest.raises(SilentTimeout):
        scope.handle(":CHANnel5:SCALe?")
    assert scope.handle(":SYSTem:ERRor?") == COMMAND_ERROR.encode()


def test_unknown_command_still_logged(scope: FakeScope) -> None:
    with pytest.raises(SilentTimeout):
        scope.handle(":FOO:BAR?")
    assert scope.command_log == [":FOO:BAR?"]


# --------------------------------------------------------------------------
# オプション照会(docs/verification/mho98-unlicensed.md 1章)
# --------------------------------------------------------------------------


def test_option_status_defaults_to_installed(scope: FakeScope) -> None:
    assert scope.handle(":SYSTem:OPTion:STATus? BND") == b"1"
    assert scope.handle(":SYSTem:OPTion:STATus? CAN-FD") == b"1"


def test_option_status_reflects_configured_options() -> None:
    """明示したトークンだけが導入済み(未ライセンス実機は AFG50 / RLU-05 が1)。"""
    scope = FakeScope(options={"AFG50": True, "RLU-05": True})

    assert scope.handle(":SYSTem:OPTion:STATus? AFG50") == b"1"
    assert scope.handle(":SYSTem:OPTion:STATus? RLU-05") == b"1"
    assert scope.handle(":SYSTem:OPTion:STATus? BND") == b"0"


def test_option_valid_is_answered_like_status(scope: FakeScope) -> None:
    """`:VALid?`(後方互換形)も `:STATus?` と同一応答(実機実測)。"""
    assert scope.handle(":SYSTem:OPTion:VALid? BND") == scope.handle(
        ":SYSTem:OPTion:STATus? BND"
    )


def test_unknown_option_type_is_silent(scope: FakeScope) -> None:
    """リスト外トークンでもSCPIサーバーが沈黙する(実機実測: AUTOA)。"""
    with pytest.raises(SilentTimeout):
        scope.handle(":SYSTem:OPTion:STATus? AUTOA")


def test_opt_query_is_silent(scope: FakeScope) -> None:
    """`*OPT?` はRigol全シリーズで未定義ヘッダ。送れば沈黙する。"""
    with pytest.raises(SilentTimeout):
        scope.handle("*OPT?")


# --------------------------------------------------------------------------
# チャンネル
# --------------------------------------------------------------------------


def test_channel_query_formats(scope: FakeScope) -> None:
    assert scope.handle(":CHANnel1:DISPlay?") == b"1"
    assert scope.handle(":CHANnel2:DISPlay?") == b"0"
    assert scope.handle(":CHANnel1:OFFSet?") == b"0.000000E+00"
    assert scope.handle(":CHANnel1:COUPling?") == b"DC"
    assert scope.handle(":CHANnel1:PROBe?") == b"1.000000E+01"
    assert scope.handle(":CHANnel1:BWLimit?") == b"OFF"
    assert scope.handle(":CHANnel1:IMPedance?") == b"OMEG"


def test_channel_scale_uses_single_digit_exponent(scope: FakeScope) -> None:
    """phase0実測の `1.000000E+1`(指数1桁)を再現する。"""
    assert scope.channels[1]["probe"] == 10.0
    assert scope.handle(":CHANnel1:SCALe?") == b"1.000000E+1"


@pytest.mark.parametrize(
    "command",
    [
        ":CHANnel1:DISPlay?",
        ":CHAN1:DISP?",
        ":CHANNEL1:DISPLAY?",
        ":chan1:disp?",
        "chan1:disp?",
    ],
)
def test_short_long_and_lowercase_forms_accepted(
    scope: FakeScope, command: str
) -> None:
    assert scope.handle(command) == b"1"


def test_channel_display_write(scope: FakeScope) -> None:
    assert scope.handle(":CHANnel1:DISPlay OFF") is None
    assert scope.channels[1]["display"] is False
    assert scope.handle(":CHANnel1:DISPlay?") == b"0"
    scope.handle(":CHAN1:DISP 1")
    assert scope.handle(":CHANnel1:DISPlay?") == b"1"
    scope.handle(":CHAN1:DISP 0")
    assert scope.handle(":CHANnel1:DISPlay?") == b"0"
    scope.handle(":CHAN1:DISP on")
    assert scope.handle(":CHANnel1:DISPlay?") == b"1"


def test_channel_scale_write_is_verbatim(scope: FakeScope) -> None:
    """MHO98は1-2-5にスナップしない(phase0実測: 3 V/div がそのまま適用)。"""
    assert scope.handle(":CHANnel1:SCALe 3.0") is None
    assert scope.channels[1]["scale"] == 3.0
    assert scope.handle(":CHANnel1:SCALe?") == b"3.000000E+0"
    assert scope.handle(":SYSTem:ERRor?") == NO_ERROR.encode()


def test_channel_scale_snaps_when_enabled() -> None:
    scope = FakeScope(snap_to_125=True)
    scope.handle(":CHANnel1:SCALe 3.0")
    assert scope.channels[1]["scale"] == 2.0


def test_channel_other_writes(scope: FakeScope) -> None:
    scope.handle(":CHANnel2:OFFSet -1.5")
    assert scope.channels[2]["offset"] == -1.5
    assert scope.handle(":CHANnel2:OFFSet?") == b"-1.500000E+00"

    scope.handle(":CHANnel2:COUPling AC")
    assert scope.handle(":CHAN2:COUP?") == b"AC"

    scope.handle(":CHANnel2:PROBe 1")
    assert scope.handle(":CHAN2:PROB?") == b"1.000000E+00"

    scope.handle(":CHANnel2:BWLimit 20M")
    assert scope.handle(":CHAN2:BWL?") == b"20M"

    scope.handle(":CHANnel2:IMPedance FIFT")
    assert scope.handle(":CHAN2:IMP?") == b"FIFT"
    assert scope.handle(":SYSTem:ERRor?") == NO_ERROR.encode()


def test_invalid_enum_value_is_silent_and_queues_out_of_range(
    scope: FakeScope,
) -> None:
    with pytest.raises(SilentTimeout):
        scope.handle(":CHANnel1:COUPling XYZ")
    assert scope.handle(":SYSTem:ERRor?") == OUT_OF_RANGE.encode()
    assert scope.channels[1]["coupling"] == "DC"


def test_non_numeric_scale_is_silent(scope: FakeScope) -> None:
    with pytest.raises(SilentTimeout):
        scope.handle(":CHANnel1:SCALe abc")
    assert scope.handle(":SYSTem:ERRor?") == OUT_OF_RANGE.encode()


# --------------------------------------------------------------------------
# 水平軸
# --------------------------------------------------------------------------


def test_timebase(scope: FakeScope) -> None:
    assert scope.handle(":TIMebase:MAIN:SCALe?") == b"2.000000E-04"
    assert scope.handle(":TIM:MAIN:SCAL 3e-4") is None
    assert scope.timebase["scale"] == 3e-4
    assert scope.handle(":TIMebase:MAIN:SCALe?") == b"3.000000E-04"

    assert scope.handle(":TIMebase:MAIN:OFFSet?") == b"0.000000E+00"
    scope.handle(":TIMebase:MAIN:OFFSet 1e-3")
    assert scope.handle(":TIM:MAIN:OFFS?") == b"1.000000E-03"


def test_acquire_fixed_queries(scope: FakeScope) -> None:
    assert scope.handle(":ACQuire:SRATe?") == b"5.0000E+06"
    assert scope.handle(":ACQ:SRAT?") == b"5.0000E+06"
    assert scope.handle(":ACQuire:MDEPth?") == b"1.0000E+04"


# --------------------------------------------------------------------------
# トリガ
# --------------------------------------------------------------------------


def test_trigger_queries(scope: FakeScope) -> None:
    """トリガのNR3は**指数1桁**(`E+0`)。MHO98実測の quirk
    (`nr3_single_digit_exponent`)で、他のサブシステムと同じ宣言表経由になった。
    """
    assert scope.handle(":TRIGger:MODE?") == b"EDGE"
    assert scope.handle(":TRIGger:EDGE:SOURce?") == b"CHAN1"
    assert scope.handle(":TRIGger:EDGE:LEVel?") == b"0.000000E+0"
    assert scope.handle(":TRIGger:EDGE:SLOPe?") == b"POS"
    assert scope.handle(":TRIGger:SWEep?") == b"AUTO"
    assert scope.handle(":TRIGger:STATus?") == b"TD"


def test_trigger_writes(scope: FakeScope) -> None:
    assert scope.handle(":TRIGger:MODE EDGE") is None
    assert scope.handle(":TRIGger:EDGE:SOURce CHANnel2") is None
    assert scope.handle(":TRIG:EDGE:SOUR?") == b"CHAN2"
    scope.handle(":TRIGger:EDGE:LEVel 2.0")
    assert scope.handle(":TRIGger:EDGE:LEVel?") == b"2.000000E+0"
    scope.handle(":TRIGger:EDGE:SLOPe NEGative")
    assert scope.handle(":TRIG:EDGE:SLOP?") == b"NEG"
    scope.handle(":TRIGger:SWEep NORMal")
    assert scope.handle(":TRIG:SWE?") == b"NORM"
    assert scope.handle(":SYSTem:ERRor?") == NO_ERROR.encode()


def test_trigger_subtrees_are_independent(scope: FakeScope) -> None:
    """種別ごとにサブツリーの状態を分けて持つ(実機と同じく切り替えても残る)。"""
    scope.handle(":TRIGger:MODE PULSe")
    scope.handle(":TRIGger:PULSe:POLarity NEGative")
    scope.handle(":TRIGger:MODE EDGE")

    assert scope.handle(":TRIGger:PULSe:POLarity?") == b"NEG"
    assert scope.handle(":TRIGger:EDGE:SLOPe?") == b"POS"
    assert scope.handle(":SYSTem:ERRor?") == NO_ERROR.encode()


def test_window_and_setup_hold_subtree_names_differ_from_mode(scope: FakeScope) -> None:
    """**罠**: MODEは `WINDow` / `SETup` だがサブツリーは `:WINDows` / `:SHOLd`。"""
    assert scope.handle(":TRIGger:MODE WINDow") is None
    assert scope.handle(":TRIGger:MODE?") == b"WIND"
    assert scope.handle(":TRIGger:WINDows:POSition?") == b"EXIT"

    assert scope.handle(":TRIGger:MODE SETup") is None
    assert scope.handle(":TRIGger:SHOLd:PATTern?") == b"H"
    assert scope.handle(":SYSTem:ERRor?") == NO_ERROR.encode()


# --------------------------------------------------------------------------
# Acquisition
# --------------------------------------------------------------------------


def test_run_stop_single_autoset(scope: FakeScope) -> None:
    assert scope.handle(":STOP") is None
    assert scope.handle(":TRIGger:STATus?") == b"STOP"
    assert scope.handle(":RUN") is None
    assert scope.handle(":TRIGger:STATus?") == b"TD"
    assert scope.handle(":SINGle") is None
    assert scope.handle(":TRIGger:STATus?") == b"WAIT"
    assert scope.handle(":AUToset") is None
    assert scope.handle(":TRIGger:STATus?") == b"TD"
    assert scope.handle(":SYSTem:ERRor?") == NO_ERROR.encode()


# --------------------------------------------------------------------------
# 測定
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("item", "expected"),
    [
        ("FREQuency", b"1.0001E+03"),
        ("FREQ", b"1.0001E+03"),
        ("PERiod", b"9.999E-04"),
        ("VPP", b"3.268E+00"),
        ("VMAX", b"3.140E+00"),
        ("VMIN", b"-6.8267E-02"),
        ("VAVG", b"1.634E+00"),
        ("VRMS", b"1.836E+00"),
        ("PDUTy", b"5.002E-01"),
        ("RTIMe", b"1.0E-06"),
        ("FTIMe", b"1.0E-06"),
    ],
)
def test_measure_items(scope: FakeScope, item: str, expected: bytes) -> None:
    assert scope.handle(f":MEASure:ITEM? {item},CHANnel1") == expected


@pytest.mark.parametrize(
    "command",
    [
        ":MEASure:ITEM? VPP,CHANnel1",
        ":MEAS:ITEM? vpp,chan1",
        ":MEASure:ITEM? VPP,CHAN1",
        ":meas:item? VPP,CHANNEL1",
    ],
)
def test_measure_accepts_short_long_and_lowercase(
    scope: FakeScope, command: str
) -> None:
    assert scope.handle(command) == b"3.268E+00"


def test_autoscale_is_silent(scope: FakeScope) -> None:
    """旧ハードコードの :AUToscale は実機に存在しない=沈黙(誤送信をテストで検出)。"""
    with pytest.raises(SilentTimeout):
        scope.handle(":AUToscale")
    scope.handle(":SYSTem:ERRor?")  # エラーキューを掃除


def test_measure_item_query_adds_to_result_view(scope: FakeScope) -> None:
    """実機仕様: クエリ形でもResultビューへ項目が追加される(issue #16)。"""
    scope.handle(":MEASure:ITEM? VPP,CHANnel1")
    scope.handle(":MEASure:ITEM? FREQuency,CHANnel1")

    assert scope.measurement_items == ["VPP", "FREQUENCY"]


def test_measure_delete_clears_result_view(scope: FakeScope) -> None:
    scope.handle(":MEASure:ITEM? VPP,CHANnel1")

    assert scope.handle(":MEASure:DELete") is None
    assert scope.measurement_items == []


def test_measure_delete_short_form(scope: FakeScope) -> None:
    scope.handle(":MEASure:ITEM? VPP,CHANnel1")

    assert scope.handle(":MEAS:DEL") is None
    assert scope.measurement_items == []


def test_rejected_measure_item_is_not_added_to_result_view(scope: FakeScope) -> None:
    with pytest.raises(SilentTimeout):
        scope.handle(":MEASure:ITEM? VAVerage,CHANnel1")

    assert scope.measurement_items == []
    scope.handle(":SYSTem:ERRor?")  # -222 を掃除


def test_vaverage_is_rejected_then_vavg_recovers(scope: FakeScope) -> None:
    """phase0実測: VAVerage は無応答+`-222`。VAVG で回復する。"""
    with pytest.raises(SilentTimeout):
        scope.handle(":MEASure:ITEM? VAVerage,CHANnel1")
    assert scope.handle(":SYSTem:ERRor?") == OUT_OF_RANGE.encode()

    assert scope.handle(":MEASure:ITEM? VAVG,CHANnel1") == b"1.634E+00"
    assert scope.handle(":SYSTem:ERRor?") == NO_ERROR.encode()


# --------------------------------------------------------------------------
# 波形
# --------------------------------------------------------------------------


def test_waveform_setup_queries(scope: FakeScope) -> None:
    assert scope.handle(":WAVeform:SOURce?") == b"CHAN1"
    assert scope.handle(":WAVeform:MODE?") == b"NORM"
    assert scope.handle(":WAVeform:FORMat?") == b"BYTE"
    assert scope.handle(":WAVeform:STARt?") == b"1"
    assert scope.handle(":WAVeform:STOP?") == b"1000"


def test_waveform_setup_writes(scope: FakeScope) -> None:
    assert scope.handle(":WAVeform:SOURce CHANnel2") is None
    assert scope.handle(":WAV:SOUR?") == b"CHAN2"
    scope.handle(":WAVeform:MODE RAW")
    assert scope.handle(":WAV:MODE?") == b"RAW"
    scope.handle(":WAVeform:FORMat WORD")
    assert scope.handle(":WAV:FORM?") == b"WORD"
    scope.handle(":WAVeform:STARt 101")
    scope.handle(":WAVeform:STOP 200")
    assert scope.handle(":WAV:STAR?") == b"101"
    assert scope.handle(":WAV:STOP?") == b"200"
    assert scope.handle(":SYSTem:ERRor?") == NO_ERROR.encode()


def test_waveform_stop_write_does_not_stop_acquisition(scope: FakeScope) -> None:
    scope.handle(":WAVeform:STOP 200")
    assert scope.handle(":TRIGger:STATus?") == b"TD"


def test_waveform_preamble(scope: FakeScope) -> None:
    assert scope.handle(":WAVeform:PREamble?") == (
        b"0,0,1000,1,2.000000E-6,-1.000000E-3,0.000000,6.8267E-02,0,128"
    )


def test_waveform_data_framing(scope: FakeScope) -> None:
    raw = scope.handle(":WAVeform:DATA?")
    assert raw is not None
    assert raw.startswith(b"#9000001000")
    assert raw.endswith(b"\n")
    payload = raw[len(b"#9000001000") : -1]
    assert len(payload) == 1000
    assert min(payload) == 127
    assert max(payload) == 174


def test_waveform_data_is_deterministic(scope: FakeScope) -> None:
    assert scope.handle(":WAV:DATA?") == scope.handle(":WAVeform:DATA?")


# --------------------------------------------------------------------------
# スクリーンショット
# --------------------------------------------------------------------------


def test_display_data_is_a_png_block(scope: FakeScope) -> None:
    raw = scope.handle(":DISPlay:DATA?")
    assert raw is not None
    assert raw.startswith(b"#9")
    assert raw.endswith(b"\n")
    length = int(raw[2:11])
    payload = raw[11 : 11 + length]
    assert len(payload) == length
    assert payload.startswith(b"\x89PNG\r\n\x1a\n")


# --------------------------------------------------------------------------
# command_log
# --------------------------------------------------------------------------


def test_command_log_records_every_command(scope: FakeScope) -> None:
    scope.handle("*IDN?")
    scope.handle(":CHANnel1:SCALe 3.0")
    scope.handle(":CHAN1:SCAL?")
    assert scope.command_log == ["*IDN?", ":CHANnel1:SCALe 3.0", ":CHAN1:SCAL?"]


# --------------------------------------------------------------------------
# FakeTransport
# --------------------------------------------------------------------------


def test_transport_satisfies_protocol(scope: FakeScope) -> None:
    assert isinstance(FakeTransport(scope), Transport)


def test_open_close_are_idempotent(scope: FakeScope) -> None:
    t = FakeTransport(scope)
    assert t.is_open is False
    t.open()
    t.open()
    assert t.is_open is True
    t.close()
    t.close()
    assert t.is_open is False


def test_transport_query(transport: FakeTransport) -> None:
    assert transport.query("*IDN?") == (
        "RIGOL TECHNOLOGIES,MHO98,FAKE0000000001,00.01.00"
    )


def test_transport_write(transport: FakeTransport, scope: FakeScope) -> None:
    assert transport.write(":CHANnel1:SCALe 3.0") is None
    assert scope.channels[1]["scale"] == 3.0


def test_transport_silent_timeout_becomes_scope_error(
    transport: FakeTransport,
) -> None:
    with pytest.raises(ScopeError) as excinfo:
        transport.query(":FOO:BAR?")
    assert excinfo.value.code == ErrorCode.TIMEOUT
    assert transport.query(":SYSTem:ERRor?") == COMMAND_ERROR


def test_transport_query_of_write_command_times_out(
    transport: FakeTransport,
) -> None:
    with pytest.raises(ScopeError) as excinfo:
        transport.query(":RUN")
    assert excinfo.value.code == ErrorCode.TIMEOUT


def test_transport_write_of_unknown_command_only_queues_error(
    transport: FakeTransport,
) -> None:
    """実機の書き込みは応答を待たないためタイムアウトせず、エラーだけが積まれる。"""
    transport.write(":FOO:BAR 1")
    assert transport.query(":SYSTem:ERRor?") == COMMAND_ERROR


def test_transport_requires_open(scope: FakeScope) -> None:
    t = FakeTransport(scope)
    for call in (
        lambda: t.write("*IDN?"),
        lambda: t.query("*IDN?"),
        lambda: t.query_binary(":WAVeform:DATA?"),
    ):
        with pytest.raises(ScopeError) as excinfo:
            call()
        assert excinfo.value.code == ErrorCode.DEVICE_DISCONNECTED


def test_transport_rejects_use_after_close(transport: FakeTransport) -> None:
    transport.close()
    with pytest.raises(ScopeError) as excinfo:
        transport.query("*IDN?")
    assert excinfo.value.code == ErrorCode.DEVICE_DISCONNECTED


def test_query_binary_returns_screenshot_payload(transport: FakeTransport) -> None:
    payload = transport.query_binary(":DISPlay:DATA?")
    assert payload.startswith(b"\x89PNG\r\n\x1a\n")


def test_query_binary_returns_waveform_payload(transport: FakeTransport) -> None:
    payload = transport.query_binary(":WAVeform:DATA?")
    assert len(payload) == 1000
    assert isinstance(payload, bytes)


def test_query_binary_payload_matches_preamble(transport: FakeTransport) -> None:
    """preambleの変換式 volts=(raw-yorigin-yreference)*yincrement で整合すること。"""
    fields = transport.query(":WAVeform:PREamble?").split(",")
    points = int(fields[2])
    yincrement = float(fields[7])
    yorigin = float(fields[8])
    yreference = float(fields[9])

    payload = transport.query_binary(":WAVeform:DATA?")
    assert len(payload) == points

    volts = [(raw - yorigin - yreference) * yincrement for raw in payload]
    assert min(volts) == pytest.approx(-0.068, abs=1e-3)
    assert max(volts) == pytest.approx(3.140, abs=1e-3)


def test_query_binary_consumes_trailing_newline(transport: FakeTransport) -> None:
    """ブロック末尾の改行が残らず、続く問い合わせが汚染されないこと。"""
    transport.query_binary(":WAVeform:DATA?")
    assert transport.query("*IDN?").startswith("RIGOL")


def test_transport_query_binary_timeout(transport: FakeTransport) -> None:
    with pytest.raises(ScopeError) as excinfo:
        transport.query_binary(":FOO:BAR?")
    assert excinfo.value.code == ErrorCode.TIMEOUT


# --------------------------------------------------------------------------
# シリアルデコード(:BUS<n> / docs/verification/mho98-unlicensed.md 3章)
# --------------------------------------------------------------------------


def test_bus_defaults_match_the_guide(scope: FakeScope) -> None:
    """ガイド既定: MODE=PARallel、表示OFF、FORMat=HEX、EVENt=OFF。"""
    assert scope.handle(":BUS1:MODE?") == b"PAR"
    assert scope.handle(":BUS1:DISPlay?") == b"0"
    assert scope.handle(":BUS1:FORMat?") == b"HEX"
    assert scope.handle(":BUS1:EVENt?") == b"0"


def test_bus_mode_round_trip(scope: FakeScope) -> None:
    scope.handle(":BUS1:MODE RS232")
    assert scope.handle(":BUS1:MODE?") == b"RS232"


def test_bus_mode_keeps_per_protocol_settings(scope: FakeScope) -> None:
    """モード切替は各プロトコルの設定を消さない(実機の観測どおり)。"""
    scope.handle(":BUS1:RS232:BAUD 115200")
    scope.handle(":BUS1:MODE SPI")
    scope.handle(":BUS1:MODE RS232")

    assert scope.handle(":BUS1:RS232:BAUD?") == b"115200"


def test_bus_rs232_baud_default(scope: FakeScope) -> None:
    """実機プローブの実測値(:BUS1:RS232:BAUD? → 9600)。"""
    assert scope.handle(":BUS1:RS232:BAUD?") == b"9600"


def test_bus_protocol_defaults_match_the_probe(scope: FakeScope) -> None:
    assert scope.handle(":BUS1:IIC:ADDBits?") == b"7"
    assert scope.handle(":BUS1:SPI:MODE?") == b"CS"
    assert scope.handle(":BUS1:CAN:BAUD?") == b"1000000"
    assert scope.handle(":BUS1:LIN:BAUD?") == b"9600"


def test_bus_threshold_round_trip(scope: FakeScope) -> None:
    """実機実測: `:BUS1:THReshold? TX` → `0.000000`(小数6桁)。"""
    assert scope.handle(":BUS1:THReshold? TX") == b"0.000000"

    scope.handle(":BUS1:THReshold 1.65,TX")

    assert scope.handle(":BUS1:THReshold? TX") == b"1.650000"
    assert scope.handle(":BUS1:THReshold? RX") == b"0.000000"


def test_bus_unknown_threshold_type_is_silent(scope: FakeScope) -> None:
    with pytest.raises(SilentTimeout):
        scope.handle(":BUS1:THReshold? IIS")


def test_bus_source_is_normalized(scope: FakeScope) -> None:
    scope.handle(":BUS1:RS232:TX CHANnel2")
    assert scope.handle(":BUS1:RS232:TX?") == b"CHAN2"

    scope.handle(":BUS1:RS232:TX D15")
    assert scope.handle(":BUS1:RS232:TX?") == b"D15"

    scope.handle(":BUS1:RS232:TX OFF")
    assert scope.handle(":BUS1:RS232:TX?") == b"OFF"


def test_bus_event_requires_display(scope: FakeScope) -> None:
    """ガイド: `:EVENt ON` の前にバスを表示ONにしておく必要がある。"""
    with pytest.raises(SilentTimeout):
        scope.handle(":BUS1:EVENt ON")
    assert scope.handle(":SYSTem:ERRor?") == OUT_OF_RANGE.encode()

    scope.handle(":BUS1:DISPlay ON")
    scope.handle(":BUS1:EVENt ON")

    assert scope.handle(":BUS1:EVENt?") == b"1"


def test_bus5_is_silent(scope: FakeScope) -> None:
    """デコードバスは4本(実機実測: :BUS4:MODE? まで応答)。"""
    with pytest.raises(SilentTimeout):
        scope.handle(":BUS5:MODE?")


@pytest.mark.parametrize(
    "command",
    [
        ":BUS1:IIS:SCLK:SOURce?",
        ":BUS1:FLEXray:BAUD?",
        ":BUS1:M1553:SOURce?",
        ":BUS1:CAN:FDBaud?",
    ],
)
def test_option_gated_decode_subtrees_are_not_modeled(
    scope: FakeScope, command: str
) -> None:
    """オプション必須プロトコルは実装しない(送れば沈黙する)。"""
    with pytest.raises(SilentTimeout):
        scope.handle(command)


def _enable_event_table(scope: FakeScope, bus: int = 1) -> None:
    scope.handle(f":BUS{bus}:DISPlay ON")
    scope.handle(f":BUS{bus}:EVENt ON")


def test_bus_data_returns_a_tmc_block(scope: FakeScope) -> None:
    """`:BUS<n>:DATA?` は definite-length block(`#9...`)で返る。"""
    _enable_event_table(scope)

    response = scope.handle(":BUS1:DATA?")

    assert response.startswith(b"#9")
    payload = response[11:-1]  # `#9` + 9桁長 ... 末尾改行
    assert len(payload) == int(response[2:11])
    # 既定モードは PAR(実機プローブの :BUS4:MODE? と同じ短形式)
    assert payload.decode("ascii").splitlines()[0] == "PARALLEL"


def test_bus_data_header_matches_the_device(scope: FakeScope) -> None:
    """実機実測(MHO98, MODE=RS232): 先頭2行は `RS232` と `Time,Tx/Rx,Data,Error,`。"""
    _enable_event_table(scope)
    scope.handle(":BUS1:MODE RS232")

    payload = scope.handle(":BUS1:DATA?")[11:-1].decode("ascii")

    assert payload.splitlines()[:2] == ["RS232", "Time,Tx/Rx,Data,Error,"]


def test_bus_data_tracks_the_mode(scope: FakeScope) -> None:
    _enable_event_table(scope)

    lines = scope.handle(":BUS1:DATA?")[11:-1].decode("ascii").splitlines()

    assert lines[0] == "PARALLEL"
    assert lines[1] == "Time,Data,"
    # プログラミングガイドの例そのまま
    assert lines[2:4] == ["-2.47us,0,", "-2.444us,1,"]

    scope.handle(":BUS1:MODE CAN")
    assert scope.handle(":BUS1:DATA?")[11:-1].decode("ascii").splitlines()[0] == "CAN"


def test_bus_data_is_empty_while_the_event_table_is_off(scope: FakeScope) -> None:
    """イベントテーブル無効時は空ペイロード(要実機検証)。"""
    scope.handle(":BUS1:DISPlay ON")

    assert scope.handle(":BUS1:EVENt?") == b"0"
    assert scope.handle(":BUS1:DATA?") == b"#9000000000\n"


# --------------------------------------------------------------------------
# 信号発生(:SOURce<n> / docs/verification/mho98-afg.md)
# --------------------------------------------------------------------------


def test_afg_defaults_match_the_probe(scope: FakeScope) -> None:
    """既定値・応答形式は実機プローブ(mho98-afg.md 1章)そのまま。"""
    assert scope.handle(":SOURce1:OUTPut:STATe?") == b"0"
    assert scope.handle(":SOURce1:FUNCtion?") == b"SIN"  # 短形で返る
    assert scope.handle(":SOURce1:FREQuency?") == b"1.000000E+3"
    assert scope.handle(":SOURce1:VOLTage:AMPLitude?") == b"5.000000E+0"
    assert scope.handle(":SOURce1:VOLTage:OFFSet?") == b"0.000000"
    assert scope.handle(":SOURce1:PHASe?") == b"0.000000"
    assert scope.handle(":SOURce1:IMPedance?") == b"OMEG"
    assert scope.handle(":SOURce1:FUNCtion:SQUare:DUTY?") == b"5.000000E+1"
    assert scope.handle(":SOURce1:FUNCtion:RAMP:SYMMetry?") == b"5.000000E+1"
    assert scope.handle(":SOURce2:OUTPut:STATe?") == b"0"


def test_afg_round_trip(scope: FakeScope) -> None:
    scope.handle(":SOURce1:FUNCtion SQUare")
    scope.handle(":SOURce1:FREQuency 2000")
    scope.handle(":SOURce1:VOLTage:AMPLitude 1")
    scope.handle(":SOURce1:IMPedance FIFTy")
    scope.handle(":SOURce1:FUNCtion:SQUare:DUTY 60")
    scope.handle(":SOURce1:FUNCtion:RAMP:SYMMetry 40")

    assert scope.handle(":SOURce1:FUNCtion?") == b"SQU"
    assert scope.handle(":SOURce1:FREQuency?") == b"2.000000E+3"
    assert scope.handle(":SOURce1:VOLTage:AMPLitude?") == b"1.000000E+0"
    # 実機実測: 設定後の返却は長形の `FIFTy`
    assert scope.handle(":SOURce1:IMPedance?") == b"FIFTy"
    assert scope.handle(":SOURce1:FUNCtion:SQUare:DUTY?") == b"6.000000E+1"
    # 波形に依らず保存される(Square中でもRAMPの対称性は書ける)
    assert scope.handle(":SOURce1:FUNCtion:RAMP:SYMMetry?") == b"4.000000E+1"
    # ch2 は独立
    assert scope.handle(":SOURce2:FUNCtion?") == b"SIN"


def test_afg_output_state_round_trip(scope: FakeScope) -> None:
    """出力状態はチャンネルごとに読み書きできる(ON/OFF どちらのトークンも)。"""
    scope.handle(":SOURce2:OUTPut:STATe ON")

    assert scope.handle(":SOURce2:OUTPut:STATe?") == b"1"
    assert scope.handle(":SOURce1:OUTPut:STATe?") == b"0"

    scope.handle(":SOURce2:OUTPut:STATe OFF")

    assert scope.handle(":SOURce2:OUTPut:STATe?") == b"0"


def test_afg_unknown_waveform_is_rejected(scope: FakeScope) -> None:
    """ガイド外トークン。実機は -222 で明示拒否(沈黙しない)するが、
    フェイクはエラー系を一律 SilentTimeout に畳む。ドライバは表に無い
    トークンをそもそも送らないため、この差は実装に影響しない。
    """
    with pytest.raises(SilentTimeout):
        scope.handle(":SOURce1:FUNCtion PULSe")


def test_afg_channel3_is_silent(scope: FakeScope) -> None:
    """`:SOURce3` は実機のSCPIサーバーを沈黙させる(mho98-afg.md 1章)。"""
    with pytest.raises(SilentTimeout):
        scope.handle(":SOURce3:FUNCtion?")

    with pytest.raises(SilentTimeout):
        scope.handle(":SOURce3:OUTPut:STATe?")


# --------------------------------------------------------------------------
# 変調・ARB選択・位相同期(:SOURce<n>:MOD:* / :LOAD:ARBitrary / :PHASe:SYNChronize)
# --------------------------------------------------------------------------


def test_afg_modulation_defaults_match_the_guide(scope: FakeScope) -> None:
    """既定値はガイドの初期値そのまま(mod_type=AM)。"""
    assert scope.handle(":SOURce1:MOD:STATe?") == b"0"
    assert scope.handle(":SOURce1:MOD:TYPe?") == b"AM"
    assert scope.handle(":SOURce1:MOD:AM:DEPTh?") == b"1.000000E+2"
    assert scope.handle(":SOURce1:MOD:AM:INTernal:FREQuency?") == b"1.000000E+2"
    assert scope.handle(":SOURce1:MOD:AM:INTernal:FUNCtion?") == b"SIN"
    assert scope.handle(":SOURce1:MOD:FM:DEViation?") == b"1.000000E+3"
    assert scope.handle(":SOURce1:MOD:PM:DEViation?") == b"9.000000E+1"


def test_afg_modulation_params_ignored_while_off(scope: FakeScope) -> None:
    """実機quirk(2026-08-27実測): MOD:STATe OFF中のパラメータ書き込みは黙って無視。"""
    scope.handle(":SOURce1:MOD:AM:DEPTh 50")

    assert scope.handle(":SOURce1:MOD:AM:DEPTh?") == b"1.000000E+2"  # 既定100のまま
    assert scope.handle(":SYSTem:ERRor?") == NO_ERROR.encode()


def test_afg_modulation_round_trip(scope: FakeScope) -> None:
    scope.handle(":SOURce1:MOD:TYPe FM")
    scope.handle(":SOURce1:MOD:STATe ON")  # 実機quirk: OFF中のパラメータは無視される
    scope.handle(":SOURce1:MOD:FM:DEViation 2000")
    scope.handle(":SOURce1:MOD:FM:INTernal:FREQuency 500")
    scope.handle(":SOURce1:MOD:FM:INTernal:FUNCtion SQUare")

    assert scope.handle(":SOURce1:MOD:TYPe?") == b"FM"
    assert scope.handle(":SOURce1:MOD:FM:DEViation?") == b"2.000000E+3"
    assert scope.handle(":SOURce1:MOD:FM:INTernal:FREQuency?") == b"5.000000E+2"
    assert scope.handle(":SOURce1:MOD:FM:INTernal:FUNCtion?") == b"SQU"
    assert scope.handle(":SOURce1:MOD:STATe?") == b"1"
    # ch2 は独立
    assert scope.handle(":SOURce2:MOD:STATe?") == b"0"
    assert scope.handle(":SOURce2:MOD:TYPe?") == b"AM"


def test_afg_modulation_unknown_type_is_rejected(scope: FakeScope) -> None:
    with pytest.raises(SilentTimeout):
        scope.handle(":SOURce1:MOD:TYPe XX")


def test_afg_arb_file_round_trip(scope: FakeScope) -> None:
    """ARBファイルパスは裸文字列のまま(引用符無し)で往復する。"""
    assert scope.handle(":SOURce1:LOAD:ARBitrary?") == b""

    scope.handle(":SOURce1:LOAD:ARBitrary D:/test.csv")

    assert scope.handle(":SOURce1:LOAD:ARBitrary?") == b"D:/test.csv"
    # ch2 は独立
    assert scope.handle(":SOURce2:LOAD:ARBitrary?") == b""


def test_afg_phase_sync_is_accepted(scope: FakeScope) -> None:
    """引数無し・応答無し。エラーキューも積まない。"""
    assert scope.handle(":SOURce1:PHASe:SYNChronize") is None
    assert scope.handle(":SYSTem:ERRor?") == NO_ERROR.encode("ascii")


# --------------------------------------------------------------------------
# MATH演算(:MATH<n> / MHO900プログラミングガイド 3.16章)
# --------------------------------------------------------------------------


def test_math_defaults_match_the_guide(scope: FakeScope) -> None:
    """既定値・応答形式はガイド3.16章の初期値そのまま(列挙は短形で返る)。"""
    assert scope.handle(":MATH1:DISPlay?") == b"0"
    assert scope.handle(":MATH1:OPERator?") == b"ADD"
    assert scope.handle(":MATH1:SOURce1?") == b"CHAN1"
    assert scope.handle(":MATH1:SOURce2?") == b"CHAN1"
    assert scope.handle(":MATH1:LSOurce1?") == b"CHAN1"
    assert scope.handle(":MATH1:OFFSet?") == b"0.000000E+0"
    assert scope.handle(":MATH1:INVert?") == b"0"
    assert scope.handle(":MATH1:FFT:SOURce?") == b"CHAN1"
    assert scope.handle(":MATH1:FFT:WINDow?") == b"HANN"
    assert scope.handle(":MATH1:FFT:UNIT?") == b"DB"
    assert scope.handle(":MATH1:FFT:MODE?") == b"NORM"
    assert scope.handle(":MATH1:FFT:AVCNt?") == b"10"
    assert scope.handle(":MATH1:FFT:FREQuency:STARt?") == b"1.000000E+0"
    assert scope.handle(":MATH1:FFT:FREQuency:END?") == b"1.000000E+7"
    assert scope.handle(":MATH1:FFT:SEARch:ENABle?") == b"0"
    assert scope.handle(":MATH1:FFT:SEARch:ORDer?") == b"AMP"
    assert scope.handle(":MATH1:FILTer:TYPE?") == b"LPAS"
    assert scope.handle(":SYSTem:ERRor?") == NO_ERROR.encode()


def test_math_short_form_mnemonics_are_accepted(scope: FakeScope) -> None:
    """末尾に番号が付くニモニックも短形(`SOUR1` / `LSO1`)で受理する。"""
    assert scope.handle(":MATH1:SOUR1?") == b"CHAN1"
    assert scope.handle(":MATH1:LSO1?") == b"CHAN1"
    assert scope.handle(":MATH1:LSO2?") == b"CHAN1"


def test_math_round_trip(scope: FakeScope) -> None:
    """書き込み → クエリで機器の短形トークン / NR3(指数1桁)が返る。"""
    scope.handle(":MATH1:DISPlay ON")
    scope.handle(":MATH1:OPERator FFT")
    scope.handle(":MATH1:SOURce2 CHANnel3")
    scope.handle(":MATH1:SCALe 0.2")
    scope.handle(":MATH1:INVert ON")
    scope.handle(":MATH1:FFT:WINDow BLACkman")
    scope.handle(":MATH1:FFT:UNIT VRMS")
    scope.handle(":MATH1:FFT:AVCNt 100")
    scope.handle(":MATH1:FFT:SEARch:ORDer FREQorder")
    scope.handle(":MATH1:FILTer:TYPE BSTop")
    scope.handle(":MATH1:FILTer:W1 2000000")

    assert scope.handle(":MATH1:DISPlay?") == b"1"
    assert scope.handle(":MATH1:OPERator?") == b"FFT"
    assert scope.handle(":MATH1:SOURce2?") == b"CHAN3"
    assert scope.handle(":MATH1:SCALe?") == b"2.000000E-1"
    assert scope.handle(":MATH1:INVert?") == b"1"
    assert scope.handle(":MATH1:FFT:WINDow?") == b"BLAC"
    assert scope.handle(":MATH1:FFT:UNIT?") == b"VRMS"
    assert scope.handle(":MATH1:FFT:AVCNt?") == b"100"
    assert scope.handle(":MATH1:FFT:SEARch:ORDer?") == b"FREQ"
    assert scope.handle(":MATH1:FILTer:TYPE?") == b"BST"
    assert scope.handle(":MATH1:FILTer:W1?") == b"2.000000E+6"
    # 他のMATHチャンネルは独立
    assert scope.handle(":MATH2:OPERator?") == b"ADD"
    assert scope.handle(":SYSTem:ERRor?") == NO_ERROR.encode()


def test_math_unknown_operator_is_rejected(scope: FakeScope) -> None:
    with pytest.raises(SilentTimeout):
        scope.handle(":MATH1:OPERator NOPE")


def test_math_channel5_is_silent(scope: FakeScope) -> None:
    """`:MATH5` はどのパターンにも一致せず沈黙する(実機同様)。"""
    with pytest.raises(SilentTimeout):
        scope.handle(":MATH5:DISPlay?")

    with pytest.raises(SilentTimeout):
        scope.handle(":MATH5:OPERator ADD")


def test_math_fft_peak_table_requires_search_enabled(scope: FakeScope) -> None:
    """ピーク表はサーチ有効時のみ。無効時は空行1本(実機実測)。"""
    assert scope.handle(":MATH1:FFT:SEARch:RES?") == b"\n"

    scope.handle(":MATH1:FFT:SEARch:ENABle ON")
    table = scope.handle(":MATH1:FFT:SEARch:RES?")

    assert table is not None
    # 実機実測の形: 行を改行で区切り、末尾に終端の空行が1本つく
    assert table.endswith(b"\n\n")
    lines = table.decode("ascii").split("\n")
    assert lines[0] == "1,2.50000MHz,-24.98dBV"
    assert lines[4] == "5,6.50125MHz,-32.34dBV"
    assert lines[5:] == ["", ""]
    # 他チャンネルのサーチ状態には影響しない
    assert scope.handle(":MATH2:FFT:SEARch:RES?") == b"\n"


def test_fake_transport_query_lines_stops_at_the_blank_line(scope: FakeScope) -> None:
    transport = FakeTransport(scope)
    transport.open()
    scope.handle(":MATH1:FFT:SEARch:ENABle ON")

    lines = transport.query_lines(":MATH1:FFT:SEARch:RES?")

    assert len(lines) == 5
    assert lines[0] == "1,2.50000MHz,-24.98dBV"
    assert transport.query_lines(":MATH2:FFT:SEARch:RES?") == []


def test_waveform_source_accepts_math(scope: FakeScope) -> None:
    """`:WAVeform:SOURce MATH<n>` を受理する(ガイド3.28.1)。"""
    assert scope.handle(":WAVeform:SOURce MATH2") is None
    assert scope.handle(":WAV:SOUR?") == b"MATH2"
    assert scope.handle(":WAVeform:PREamble?") == (
        b"0,0,1000,1,2.000000E-6,-1.000000E-3,0.000000,6.8267E-02,0,128"
    )
    assert scope.handle(":SYSTem:ERRor?") == NO_ERROR.encode()


def test_waveform_preamble_follows_the_math_offset(scope: FakeScope) -> None:
    """MATHソースのyoriginはMATHチャンネル側のoffsetから決まる。"""
    scope.handle(":WAVeform:SOURce MATH2")
    scope.handle(":MATH2:OFFSet -0.6827")

    preamble = scope.handle(":WAVeform:PREamble?")

    assert preamble is not None
    assert preamble.decode("ascii").split(",")[8] == "-10"


def test_waveform_source_rejects_math5(scope: FakeScope) -> None:
    with pytest.raises(SilentTimeout):
        scope.handle(":WAVeform:SOURce MATH5")


# --------------------------------------------------------------------------
# カーソル測定(:CURSor / ガイド3.8)
# --------------------------------------------------------------------------


def test_cursor_defaults_match_the_guide(scope: FakeScope) -> None:
    assert scope.handle(":CURSor:MODE?") == b"OFF"
    assert scope.handle(":CURSor:MANual:TYPE?") == b"TIME"
    assert scope.handle(":CURSor:MANual:SOURce?") == b"CHAN1"
    assert scope.handle(":CURSor:TRACk:SOURce1?") == b"CHAN1"
    assert scope.handle(":CURSor:TRACk:SOURce2?") == b"CHAN1"
    assert scope.handle(":SYSTem:ERRor?") == NO_ERROR.encode()


def test_cursor_short_form_mnemonics_are_accepted(scope: FakeScope) -> None:
    assert scope.handle(":CURS:MODE?") == b"OFF"
    assert scope.handle(":CURS:MAN:TYPE?") == b"TIME"
    assert scope.handle(":CURS:TRAC:SOUR1?") == b"CHAN1"


def test_cursor_manual_round_trip(scope: FakeScope) -> None:
    scope.handle(":CURSor:MODE MANual")
    scope.handle(":CURSor:MANual:TYPE AMPLitude")
    scope.handle(":CURSor:MANual:SOURce MATH2")
    scope.handle(":CURSor:MANual:CAX 1e-4")
    scope.handle(":CURSor:MANual:CAY -0.5")

    assert scope.handle(":CURSor:MODE?") == b"MAN"
    assert scope.handle(":CURSor:MANual:TYPE?") == b"AMPL"
    assert scope.handle(":CURSor:MANual:SOURce?") == b"MATH2"
    assert scope.handle(":CURSor:MANual:CAX?") == b"1.000000E-4"
    assert scope.handle(":CURSor:MANual:CAY?") == b"-5.000000E-1"
    assert scope.handle(":SYSTem:ERRor?") == NO_ERROR.encode()


def test_cursor_readouts_follow_the_stored_positions(scope: FakeScope) -> None:
    """AXValue? 等は CAX/CAY/CBX/CBY をそのまま返す(実機の読み値と同義)。"""
    scope.handle(":CURSor:MANual:CAX 1e-4")
    scope.handle(":CURSor:MANual:CBX 3e-4")
    scope.handle(":CURSor:MANual:CAY -1")
    scope.handle(":CURSor:MANual:CBY 2")

    assert scope.handle(":CURSor:MANual:AXValue?") == b"1.000000E-4"
    assert scope.handle(":CURSor:MANual:BXValue?") == b"3.000000E-4"
    assert scope.handle(":CURSor:MANual:AYValue?") == b"-1.000000E+0"
    assert scope.handle(":CURSor:MANual:BYValue?") == b"2.000000E+0"


def test_cursor_deltas_are_derived_arithmetic(scope: FakeScope) -> None:
    """ΔX = BX-AX、ΔY = BY-AY、1/ΔX は実際に計算した値を返す。"""
    scope.handle(":CURSor:MANual:CAX 1e-4")
    scope.handle(":CURSor:MANual:CBX 3e-4")
    scope.handle(":CURSor:MANual:CAY -1")
    scope.handle(":CURSor:MANual:CBY 2")

    assert scope.handle(":CURSor:MANual:XDELta?") == b"2.000000E-4"
    assert scope.handle(":CURSor:MANual:YDELta?") == b"3.000000E+0"
    assert scope.handle(":CURSor:MANual:IXDelta?") == b"5.000000E+3"


def test_cursor_ixdelta_guards_zero_delta(scope: FakeScope) -> None:
    """ΔX=0 では測定不能の番兵値(±9.9E37)を返す。"""
    scope.handle(":CURSor:MANual:CAX 1e-4")
    scope.handle(":CURSor:MANual:CBX 1e-4")

    assert scope.handle(":CURSor:MANual:XDELta?") == b"0.000000E+0"
    assert scope.handle(":CURSor:MANual:IXDelta?") == b"9.900000E+37"


def test_cursor_track_subtree_is_independent_of_manual(scope: FakeScope) -> None:
    scope.handle(":CURSor:TRACk:CAX 5e-4")
    scope.handle(":CURSor:TRACk:CBX 6e-4")

    assert scope.handle(":CURSor:TRACk:XDELta?") == b"1.000000E-4"
    assert scope.handle(":CURSor:MANual:CAX?") == b"-2.000000E-4"


def test_cursor_unknown_mode_is_rejected(scope: FakeScope) -> None:
    with pytest.raises(SilentTimeout):
        scope.handle(":CURSor:MODE NOPE")


def test_cursor_out_of_scope_subtrees_are_silent(scope: FakeScope) -> None:
    """XYサブツリー・TUNit/VUNit・インジケータは未実装 = 実機同様に沈黙する。"""
    for command in (
        ":CURSor:XY:AX?",
        ":CURSor:MANual:TUNit?",
        ":CURSor:MANual:VUNit?",
        ":CURSor:MEASure:INDicator?",
    ):
        with pytest.raises(SilentTimeout):
            scope.handle(command)


# --------------------------------------------------------------------------
# 周波数カウンタ(:COUNter / ガイド3.7)
# --------------------------------------------------------------------------


def test_counter_defaults_match_the_guide(scope: FakeScope) -> None:
    assert scope.handle(":COUNter:ENABle?") == b"0"
    assert scope.handle(":COUNter:SOURce?") == b"CHAN1"
    assert scope.handle(":COUNter:MODE?") == b"FREQ"
    assert scope.handle(":COUNter:NDIGits?") == b"4"
    assert scope.handle(":COUNter:TOTalize:ENABle?") == b"0"
    assert scope.handle(":SYSTem:ERRor?") == NO_ERROR.encode()


def test_counter_round_trip(scope: FakeScope) -> None:
    scope.handle(":COUNter:ENABle ON")
    scope.handle(":COUNter:SOURce D3")
    scope.handle(":COUNter:MODE PERiod")
    scope.handle(":COUNter:NDIGits 6")
    scope.handle(":COUNter:TOTalize:ENABle ON")

    assert scope.handle(":COUN:ENAB?") == b"1"
    assert scope.handle(":COUNter:SOURce?") == b"D3"
    assert scope.handle(":COUNter:MODE?") == b"PER"
    assert scope.handle(":COUNter:NDIGits?") == b"6"
    assert scope.handle(":COUNter:TOTalize:ENABle?") == b"1"
    assert scope.handle(":SYSTem:ERRor?") == NO_ERROR.encode()


def test_counter_current_follows_the_mode(scope: FakeScope) -> None:
    assert scope.handle(":COUNter:CURRent?") == b"1.000000E+3"

    scope.handle(":COUNter:MODE PERiod")
    assert scope.handle(":COUNter:CURRent?") == b"1.000000E-3"

    scope.handle(":COUNter:MODE TOTalize")
    assert scope.handle(":COUNter:CURRent?") == b"1.234000E+3"


def test_counter_totalize_clear_zeroes_the_count(scope: FakeScope) -> None:
    scope.handle(":COUNter:MODE TOTalize")

    assert scope.handle(":COUNter:TOTalize:CLEar") is None
    assert scope.handle(":COUNter:CURRent?") == b"0.000000E+0"
    assert scope.handle(":SYSTem:ERRor?") == NO_ERROR.encode()


def test_counter_value_mnemonic_does_not_exist(scope: FakeScope) -> None:
    """現在値は `:COUNter:CURRent?`。`:COUNter:VALue?` は存在しない(ガイド3.7)。"""
    with pytest.raises(SilentTimeout):
        scope.handle(":COUNter:VALue?")


def test_counter_rejects_out_of_range_digits(scope: FakeScope) -> None:
    with pytest.raises(SilentTimeout):
        scope.handle(":COUNter:NDIGits 7")

    assert scope.handle(":SYSTem:ERRor?") == OUT_OF_RANGE.encode()
    assert scope.handle(":COUNter:NDIGits?") == b"4"


# --------------------------------------------------------------------------
# デジタル電圧計(:DVM / ガイド3.10)
# --------------------------------------------------------------------------


def test_dvm_defaults_match_the_guide(scope: FakeScope) -> None:
    assert scope.handle(":DVM:ENABle?") == b"0"
    assert scope.handle(":DVM:SOURce?") == b"CHAN1"
    assert scope.handle(":DVM:MODE?") == b"ACRM"
    assert scope.handle(":SYSTem:ERRor?") == NO_ERROR.encode()


def test_dvm_round_trip(scope: FakeScope) -> None:
    scope.handle(":DVM:ENABle ON")
    scope.handle(":DVM:SOURce CHANnel3")
    scope.handle(":DVM:MODE DCRMs")

    assert scope.handle(":DVM:ENABle?") == b"1"
    assert scope.handle(":DVM:SOURce?") == b"CHAN3"
    assert scope.handle(":DVM:MODE?") == b"DCRM"
    assert scope.handle(":SYSTem:ERRor?") == NO_ERROR.encode()


def test_dvm_current_follows_the_mode(scope: FakeScope) -> None:
    scope.handle(":DVM:ENABle ON")
    assert scope.handle(":DVM:CURRent?") == b"3.500000E-1"

    scope.handle(":DVM:MODE DC")
    assert scope.handle(":DVM:CURRent?") == b"1.000000E+0"


def test_dvm_rejects_digital_sources(scope: FakeScope) -> None:
    """`:DVM:SOURce` はアナログchのみ(ガイド3.10.3)。"""
    with pytest.raises(SilentTimeout):
        scope.handle(":DVM:SOURce D0")

    assert scope.handle(":SYSTem:ERRor?") == OUT_OF_RANGE.encode()
    assert scope.handle(":DVM:SOURce?") == b"CHAN1"


# --------------------------------------------------------------------------
# ヒストグラム(:HISTogram / ガイド3.11)
# --------------------------------------------------------------------------


def test_histogram_defaults_match_the_guide(scope: FakeScope) -> None:
    assert scope.handle(":HISTogram:ENABle?") == b"0"
    assert scope.handle(":HISTogram:TYPE?") == b"HOR"
    assert scope.handle(":HISTogram:SOURce?") == b"CHAN1"
    assert scope.handle(":HISTogram:HEIGht?") == b"2"
    assert scope.handle(":SYSTem:ERRor?") == NO_ERROR.encode()


def test_histogram_round_trip(scope: FakeScope) -> None:
    scope.handle(":HISTogram:ENABle ON")
    scope.handle(":HISTogram:TYPE VERTical")
    scope.handle(":HISTogram:SOURce CHANnel2")
    scope.handle(":HISTogram:HEIGht 4")
    scope.handle(":HISTogram:RANGe:LEFT -1e-3")
    scope.handle(":HISTogram:RANGe:RIGHt 1e-3")
    scope.handle(":HISTogram:RANGe:BOTTom -2")
    scope.handle(":HISTogram:RANGe:TOP 2")

    assert scope.handle(":HIST:ENAB?") == b"1"
    assert scope.handle(":HISTogram:TYPE?") == b"VERT"
    assert scope.handle(":HISTogram:SOURce?") == b"CHAN2"
    assert scope.handle(":HISTogram:HEIGht?") == b"4"
    assert scope.handle(":HISTogram:RANGe:LEFT?") == b"-1.000000E-3"
    assert scope.handle(":HISTogram:RANGe:RIGHt?") == b"1.000000E-3"
    assert scope.handle(":HISTogram:RANGe:BOTTom?") == b"-2.000000E+0"
    assert scope.handle(":HISTogram:RANGe:TOP?") == b"2.000000E+0"
    assert scope.handle(":SYSTem:ERRor?") == NO_ERROR.encode()


def test_histogram_rejects_out_of_range_height(scope: FakeScope) -> None:
    with pytest.raises(SilentTimeout):
        scope.handle(":HISTogram:HEIGht 5")

    assert scope.handle(":SYSTem:ERRor?") == OUT_OF_RANGE.encode()
    assert scope.handle(":HISTogram:HEIGht?") == b"2"


def test_histogram_statistics_result_shape(scope: FakeScope) -> None:
    """M2実機実測: 機器自身がラベルを付けた `[Label:Value, ...]` を返す。"""
    scope.handle(":HISTogram:ENABle ON")

    result = scope.handle(":HISTogram:STATistics:RESult?")

    assert result is not None
    text = result.decode("ascii")
    assert text.startswith("[Sum:374hits, Peaks:38hits, Max:1.562V, ")
    assert text.endswith("meanPlus3Sigma:1.000000]\n")
    # 単位はSI接頭辞付きで返る(接頭辞の換算はパーサの責務)
    assert "Min:-999.9mV" in text
    assert "Bin width:15.62mV" in text


def test_histogram_reset_zeroes_the_hit_counts(scope: FakeScope) -> None:
    scope.handle(":HISTogram:ENABle ON")

    assert scope.handle(":HISTogram:RESet") is None

    result = scope.handle(":HISTogram:STATistics:RESult?")

    assert result is not None
    assert "[Sum:0hits, Peaks:0hits," in result.decode("ascii")
    assert scope.handle(":SYSTem:ERRor?") == NO_ERROR.encode()


def test_histogram_statistics_result_has_no_blank_line_terminator(
    scope: FakeScope,
) -> None:
    """M2実機実測: 統計応答は改行1本で終わる**1行**(FFTピーク表と違い空行が無い)。

    ここが実機と食い違うと `query_lines` の空行待ちが固まる経路を隠してしまう。
    """
    scope.handle(":HISTogram:ENABle ON")
    transport = FakeTransport(scope)
    transport.open()

    text = scope.handle(":HISTogram:STATistics:RESult?")

    assert text is not None
    assert text.decode("ascii").count("\n") == 1
    assert transport.query(":HISTogram:STATistics:RESult?").endswith("]")


def test_histogram_statistics_result_while_disabled_dirties_the_error_queue(
    scope: FakeScope,
) -> None:
    """M2実機実測: 無効時は `[]` を返しつつ **-200 をエラーキューへ積む**(沈黙しない)。

    エラーキューは共有状態なので、この汚染は次の書き込みの検査に化けて出る。
    """
    result = scope.handle(":HISTogram:STATistics:RESult?")

    assert result is not None
    assert result.decode("ascii").startswith("[]")
    assert scope.handle(":SYSTem:ERRor?") == EXECUTE_ERROR.encode()
    assert scope.handle(":SYSTem:ERRor?") == NO_ERROR.encode()


def test_dvm_current_while_disabled_is_an_empty_response(scope: FakeScope) -> None:
    """M2実機実測: 電圧計が無効なら `:DVM:CURRent?` は**空文字**を返す。"""
    assert scope.handle(":DVM:CURRent?") == b""
    assert scope.handle(":SYSTem:ERRor?") == NO_ERROR.encode()

    scope.handle(":DVM:ENABle ON")

    assert scope.handle(":DVM:CURRent?") == b"3.500000E-1"


def test_counter_current_while_disabled_is_still_a_number(scope: FakeScope) -> None:
    """M2実機実測: カウンタは無効でも数値を返す(実機は `0`)。空応答の癖は電圧計だけ。"""
    result = scope.handle(":COUNter:CURRent?")

    assert result is not None
    assert float(result.decode("ascii")) == pytest.approx(1.0e3)
    assert scope.handle(":SYSTem:ERRor?") == NO_ERROR.encode()


def test_histogram_save_csv_is_silent(scope: FakeScope) -> None:
    """機器ストレージへの書き出しはスコープ外 = 未実装(沈黙)。"""
    with pytest.raises(SilentTimeout):
        scope.handle(":HISTogram:SAVE:CSV")


# --------------------------------------------------------------------------
# リファレンス波形(:REFerence / ガイド3.20)
# --------------------------------------------------------------------------
#
# 他のサブシステムと違い、枠番号は**ニモニックではなくコマンド引数**で渡す
# (`:REFerence:VSCale <ref>,<scale>` / `:REFerence:VSCale? <ref>`)。


def test_reference_defaults_match_the_device(scope: FakeScope) -> None:
    """既定値は実機の工場出荷状態(firmware 00.01.00)の実測値。"""
    assert scope.handle(":REFerence:SOURce? 1") == b"CHAN1"
    assert scope.handle(":REFerence:VSCale? 1") == b"5.000000E-2"
    assert scope.handle(":REFerence:VOFFset? 1") == b"0.000000E+0"
    assert scope.handle(":REFerence:COLor? 1") == b"ORAN"
    assert scope.handle(":REFerence:LABel:CONTent? 1") == b"REF1"
    assert scope.handle(":REFerence:LABel:ENABle?") == b"0"
    assert scope.handle(":SYSTem:ERRor?") == NO_ERROR.encode()


def test_reference_default_colors_cycle_across_the_slots(scope: FakeScope) -> None:
    """実測: 枠1から ORAN / RED / BLUE / GREE / GRAY の巡回。枠4と枠9が緑。"""
    colors = [scope.handle(f":REFerence:COLor? {n}") for n in range(1, 11)]

    assert colors == [
        b"ORAN", b"RED", b"BLUE", b"GREE", b"GRAY",
        b"ORAN", b"RED", b"BLUE", b"GREE", b"GRAY",
    ]
    assert scope.handle(":SYSTem:ERRor?") == NO_ERROR.encode()


def test_reference_round_trip_takes_the_slot_as_an_argument(scope: FakeScope) -> None:
    """`<ref>,<値>` で書き、`? <ref>` で読む。枠どうしは独立している。"""
    scope.handle(":REFerence:SOURce 2,CHANnel3")
    scope.handle(":REFerence:VSCale 2,0.5")
    scope.handle(":REFerence:VOFFset 2,-1.5")
    scope.handle(":REFerence:COLor 2,GREen")
    scope.handle(":REFerence:LABel:CONTent 2,probe_a")

    assert scope.handle(":REFerence:SOURce? 2") == b"CHAN3"
    assert scope.handle(":REFerence:VSCale? 2") == b"5.000000E-1"
    assert scope.handle(":REFerence:VOFFset? 2") == b"-1.500000E+0"
    assert scope.handle(":REFerence:COLor? 2") == b"GREE"  # 実測(ガイドは GRE)
    assert scope.handle(":REFerence:LABel:CONTent? 2") == b"probe_a"
    # 枠1は触れていない
    assert scope.handle(":REFerence:SOURce? 1") == b"CHAN1"
    assert scope.handle(":REFerence:VSCale? 1") == b"5.000000E-2"
    assert scope.handle(":SYSTem:ERRor?") == NO_ERROR.encode()


def test_reference_short_form_mnemonics_and_slot10_are_accepted(
    scope: FakeScope,
) -> None:
    scope.handle(":REF:VSC 10,2")

    assert scope.handle(":REF:VSC? 10") == b"2.000000E+0"
    assert scope.handle(":REFerence:VSCale? 10") == b"2.000000E+0"


def test_reference_source_accepts_digital_and_math(scope: FakeScope) -> None:
    """ガイド3.20.2 の値域は `{D0..D15|CHANnel1..4|MATH1..4}`。

    「現在有効なチャンネルのみ選択できる」というガイドのRemarksは**機器側の
    条件**で、拒否のされ方(沈黙 / エラーキュー)が実機未検証のため、フェイクは
    値域だけを見る(勝手な挙動を作らない)。
    """
    scope.handle(":REFerence:SOURce 1,D7")
    assert scope.handle(":REFerence:SOURce? 1") == b"D7"

    scope.handle(":REFerence:SOURce 1,MATH2")
    assert scope.handle(":REFerence:SOURce? 1") == b"MATH2"

    with pytest.raises(SilentTimeout):
        scope.handle(":REFerence:SOURce 1,REF2")


def test_reference_unknown_color_is_rejected(scope: FakeScope) -> None:
    with pytest.raises(SilentTimeout):
        scope.handle(":REFerence:COLor 1,PURPle")

    assert scope.handle(":SYSTem:ERRor?") == OUT_OF_RANGE.encode()
    assert scope.handle(":REFerence:COLor? 1") == b"ORAN"


def test_reference_slot_out_of_range_is_silent(scope: FakeScope) -> None:
    """枠は1〜10。`11` はどのパターンにも一致せず沈黙する(実機同様)。"""
    with pytest.raises(SilentTimeout):
        scope.handle(":REFerence:VSCale? 11")

    with pytest.raises(SilentTimeout):
        scope.handle(":REFerence:VSCale 11,1")

    with pytest.raises(SilentTimeout):
        scope.handle(":REFerence:VSCale? 0")


def test_reference_query_without_the_slot_is_silent(scope: FakeScope) -> None:
    """枠番号は引数なので、省いた問い合わせは未定義ヘッダ扱い(沈黙)。"""
    with pytest.raises(SilentTimeout):
        scope.handle(":REFerence:VSCale?")

    with pytest.raises(SilentTimeout):
        scope.handle(":REFerence:VSCale 1")


def test_reference_save_is_write_only_and_marks_the_slot(scope: FakeScope) -> None:
    """`:REFerence:SAVE <ref>` はクエリの無い書き込み専用(応答なし)。"""
    assert scope.reference[3]["saved"] is False

    assert scope.handle(":REFerence:SAVE 3") is None

    assert scope.reference[3]["saved"] is True
    assert scope.reference[1]["saved"] is False
    assert scope.handle(":SYSTem:ERRor?") == NO_ERROR.encode()

    with pytest.raises(SilentTimeout):
        scope.handle(":REFerence:SAVE? 3")


def test_reference_reset_restores_the_vertical_defaults(scope: FakeScope) -> None:
    """`:REFerence:RESet <ref>` は垂直スケールと位置だけを既定へ戻す。"""
    scope.handle(":REFerence:VSCale 4,0.25")
    scope.handle(":REFerence:VOFFset 4,2")
    scope.handle(":REFerence:COLor 4,RED")

    assert scope.handle(":REFerence:RESet 4") is None

    assert scope.handle(":REFerence:VSCale? 4") == b"5.000000E-2"
    assert scope.handle(":REFerence:VOFFset? 4") == b"0.000000E+0"
    assert scope.handle(":REFerence:COLor? 4") == b"RED"  # 色は戻らない
    assert scope.handle(":SYSTem:ERRor?") == NO_ERROR.encode()


def test_reference_label_enable_is_global(scope: FakeScope) -> None:
    """`:REFerence:LABel:ENABle` だけは枠引数を取らない(全枠共通)。"""
    scope.handle(":REFerence:LABel:ENABle ON")

    assert scope.handle(":REFerence:LABel:ENABle?") == b"1"

    # 枠番号を付けた形は存在しない
    with pytest.raises(SilentTimeout):
        scope.handle(":REFerence:LABel:ENABle 1,ON")


def test_reference_current_is_not_implemented(scope: FakeScope) -> None:
    """`:REFerence:CURRent` はM3スコープ外 = 未実装(沈黙)。"""
    with pytest.raises(SilentTimeout):
        scope.handle(":REFerence:CURRent 1")


# -- パラレルのデータソース(ガイド3.4.10.1・実機実測 mho98-phase4.md 5章)----


def test_bus_parallel_bus_round_trip(scope: FakeScope) -> None:
    """既定は CHANnel1(ガイド3.4.10.1)。デジタルグループとUSERも往復する。"""
    assert scope.handle(":BUS1:PARallel:BUS?") == b"CHAN1"

    scope.handle(":BUS1:PARallel:BUS USER")
    assert scope.handle(":BUS1:PARallel:BUS?") == b"USER"

    scope.handle(":BUS1:PARallel:BUS D7D0")
    assert scope.handle(":BUS1:PARallel:BUS?") == b"D7D0"


def test_bus_parallel_width_requires_user_source(scope: FakeScope) -> None:
    """実機実測: データソースが USER でなければ `:WIDTh` は -200 で拒否される。

    **沈黙はしない**(エラーキューに積むだけ)ので、値も変わらないことを確認する。
    """
    before = scope.handle(":BUS1:PARallel:WIDTh?")

    assert scope.handle(":BUS1:PARallel:WIDTh 4") is None
    assert scope.handle(":SYSTem:ERRor?") == EXECUTE_ERROR.encode()
    assert scope.handle(":BUS1:PARallel:WIDTh?") == before

    scope.handle(":BUS1:PARallel:BUS USER")
    scope.handle(":BUS1:PARallel:WIDTh 4")

    assert scope.handle(":BUS1:PARallel:WIDTh?") == b"4"
    assert scope.handle(":SYSTem:ERRor?") == NO_ERROR.encode()


def test_bus_parallel_bitx_requires_user_source(scope: FakeScope) -> None:
    assert scope.handle(":BUS1:PARallel:BITX 1") is None
    assert scope.handle(":SYSTem:ERRor?") == EXECUTE_ERROR.encode()


def test_bus_parallel_source_requires_user_source(scope: FakeScope) -> None:
    assert scope.handle(":BUS1:PARallel:SOURce CHANnel2") is None
    assert scope.handle(":SYSTem:ERRor?") == EXECUTE_ERROR.encode()


def test_bus_parallel_source_follows_the_selected_bit(scope: FakeScope) -> None:
    """`:SOURce` は `:BITX` で選択中のビットにだけ効く(ガイド3.4.10.5/3.4.10.6)。"""
    scope.handle(":BUS1:PARallel:BUS USER")
    scope.handle(":BUS1:PARallel:BITX 1")
    scope.handle(":BUS1:PARallel:SOURce CHANnel2")

    assert scope.handle(":BUS1:PARallel:BITX?") == b"1"
    assert scope.handle(":BUS1:PARallel:SOURce?") == b"CHAN2"

    scope.handle(":BUS1:PARallel:BITX 0")
    scope.handle(":BUS1:PARallel:SOURce D3")

    assert scope.handle(":BUS1:PARallel:SOURce?") == b"D3"

    scope.handle(":BUS1:PARallel:BITX 1")
    assert scope.handle(":BUS1:PARallel:SOURce?") == b"CHAN2"
