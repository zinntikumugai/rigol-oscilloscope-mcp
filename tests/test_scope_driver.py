"""driver/scope.py のテスト。

意味的操作 → SCPI生成 → 応答解釈の全経路を、フェイク機器(MHO98方言)で検証する。
プロファイル依存の分岐(未確認ニモニック / 能力チェック)は generic プロファイルと
比較して確認する。
"""

import pytest

from rigol_oscilloscope_mcp.driver.scope import ScopeDriver, WaveformPreamble
from rigol_oscilloscope_mcp.driver.session import ScpiSession
from rigol_oscilloscope_mcp.errors import ErrorCode, ScopeError
from rigol_oscilloscope_mcp.profiles import Profile, load_profile
from rigol_oscilloscope_mcp.testing import FakeScope, FakeTransport
from rigol_oscilloscope_mcp.testing import fake_scope as fake_scope_module

PNG_MAGIC = b"\x89PNG\r\n\x1a\n"


def make_driver(scope: FakeScope, profile_name: str = "mho98") -> ScopeDriver:
    transport = FakeTransport(scope)
    transport.open()
    return ScopeDriver(ScpiSession(transport), load_profile(profile_name))


@pytest.fixture
def scope() -> FakeScope:
    return FakeScope()


@pytest.fixture
def driver(scope: FakeScope) -> ScopeDriver:
    return make_driver(scope)


@pytest.fixture
def generic_driver(scope: FakeScope) -> ScopeDriver:
    return make_driver(scope, "rigol-generic")


@pytest.fixture
def dho_driver(scope: FakeScope) -> ScopeDriver:
    """ガイドベースのDHO900プロファイル(方言差の検証用。実機未検証)。"""
    return make_driver(scope, "dho900")


def sent(scope: FakeScope, needle: str) -> list[str]:
    return [c for c in scope.command_log if needle in c.upper()]


# --------------------------------------------------------------------------
# 識別
# --------------------------------------------------------------------------


def test_identify(driver: ScopeDriver) -> None:
    idn = driver.identify()

    assert idn.manufacturer == "RIGOL TECHNOLOGIES"
    assert idn.model == "MHO98"
    assert idn.serial == "FAKE0000000001"
    assert idn.firmware == "00.01.00"


# --------------------------------------------------------------------------
# 取得
# --------------------------------------------------------------------------


def test_get_channel_defaults(driver: ScopeDriver) -> None:
    state = driver.get_channel("CH1")

    assert state.channel == "CH1"
    assert state.enabled is True
    # 指数1桁のNR3(`1.000000E+1`)を正しく10.0として解釈する
    assert state.scale_v_per_div == 10.0
    assert state.offset_v == 0.0
    assert state.coupling == "DC"
    assert state.impedance == "1M"
    assert state.probe_ratio == 10.0
    assert state.bandwidth_limit is False


def test_get_channel_disabled(driver: ScopeDriver) -> None:
    assert driver.get_channel("CH2").enabled is False


def test_get_channel_skips_impedance_without_capability(
    generic_driver: ScopeDriver, scope: FakeScope
) -> None:
    """impedance_control 未宣言なら :IMPedance? を送らない(未対応機で5秒タイムアウト)。"""
    state = generic_driver.get_channel("CH1")

    assert state.impedance == "unknown"
    assert sent(scope, ":IMPEDANCE") == []


def test_get_channel_uses_long_form_commands(driver: ScopeDriver, scope: FakeScope) -> None:
    driver.get_channel("CH1")

    assert ":CHANnel1:SCALe?" in scope.command_log


def test_get_channel_rejects_out_of_range(driver: ScopeDriver, scope: FakeScope) -> None:
    with pytest.raises(ScopeError) as excinfo:
        driver.get_channel("CH5")

    assert excinfo.value.code == ErrorCode.INVALID_PARAMETER
    assert scope.command_log == []


def test_get_channel_rejects_garbage(driver: ScopeDriver) -> None:
    with pytest.raises(ScopeError) as excinfo:
        driver.get_channel("nope")

    assert excinfo.value.code == ErrorCode.INVALID_PARAMETER


def test_get_timebase(driver: ScopeDriver) -> None:
    state = driver.get_timebase()

    assert state.scale_s_per_div == pytest.approx(2.0e-4)
    assert state.position_s == 0.0
    assert state.sample_rate_sa_per_s == pytest.approx(5.0e6)
    assert state.memory_depth == pytest.approx(1.0e4)


def test_get_trigger(driver: ScopeDriver) -> None:
    state = driver.get_trigger()

    assert state.type == "edge"
    assert state.source == "CH1"
    assert state.level_v == 0.0
    assert state.slope == "rising"
    assert state.sweep_mode == "auto"
    assert state.status == "TD"


def test_get_trigger_status_is_raw(driver: ScopeDriver, scope: FakeScope) -> None:
    scope.acquisition = "STOP"

    assert driver.get_trigger_status() == "STOP"


# --------------------------------------------------------------------------
# 測定
# --------------------------------------------------------------------------


def test_measure_uses_profile_mnemonic(driver: ScopeDriver, scope: FakeScope) -> None:
    """MHO98では VAVerage が拒否されるため、プロファイルは VAVG を指定する。"""
    results = driver.measure("CH1", ["vavg"])

    assert len(results) == 1
    assert results[0].name == "vavg"
    assert results[0].key == "vavg_v"
    assert results[0].value == pytest.approx(1.634)
    assert results[0].quality == "valid"

    measure_commands = sent(scope, ":MEAS")
    assert measure_commands == [":MEASure:ITEM? VAVG,CHANnel1"]
    assert not any("VAVERAGE" in c.upper() for c in scope.command_log)


def test_measure_multiple_keys_carry_si_units(driver: ScopeDriver) -> None:
    names = [
        "frequency",
        "period",
        "vpp",
        "vmax",
        "vmin",
        "vavg",
        "rms",
        "duty",
        "rise_time",
        "fall_time",
    ]
    results = driver.measure("CH1", names)

    assert [r.key for r in results] == [
        "frequency_hz",
        "period_s",
        "vpp_v",
        "vmax_v",
        "vmin_v",
        "vavg_v",
        "rms_v",
        "duty_ratio",
        "rise_time_s",
        "fall_time_s",
    ]
    assert all(r.quality == "valid" for r in results)
    assert results[0].value == pytest.approx(1000.1)


def test_autoset_sends_the_dialect_command(
    driver: ScopeDriver, scope: FakeScope
) -> None:
    """MHO900の正式ニモニックは :AUToset(ガイド3.2.1。:AUToscale は誤り)。"""
    scope.command_log.clear()

    driver.autoset()

    assert [c for c in scope.command_log if "?" not in c] == [":AUToset"]


def test_autoset_unsupported_profile_sends_nothing(
    generic_driver: ScopeDriver, scope: FakeScope
) -> None:
    """ニモニックは世代で分岐し得るため、未宣言プロファイルではゼロ送信で拒否。"""
    with pytest.raises(ScopeError) as excinfo:
        generic_driver.autoset()

    assert excinfo.value.code == ErrorCode.UNSUPPORTED_FEATURE
    assert scope.command_log == []


class _DgStatusScope(FakeScope):
    """DHO系の :SYSTem:DGSTatus? に応答するフェイク(AFG搭載可否ゲートの検証用)。"""

    def __init__(self, dg_status: bool) -> None:
        super().__init__()
        self.dg_status = dg_status

    def handle(self, command: str) -> bytes | None:
        text = command.strip().upper()
        if text in (":SYSTEM:DGSTATUS?", ":SYST:DGST?"):
            self.command_log.append(command)
            return b"1" if self.dg_status else b"0"
        return super().handle(command)


def test_dho900_afg_invalid_presence_query_sends_nothing() -> None:
    """afg_presence_query が不正値(空文字等)なら送信ゼロでフェイルクローズ。"""
    scope = _DgStatusScope(dg_status=True)
    driver = make_driver(scope, "dho900")
    driver.profile.dialect["afg_presence_query"] = "  "

    with pytest.raises(ScopeError) as excinfo:
        driver.get_afg_config(1)

    assert excinfo.value.code == ErrorCode.UNSUPPORTED_FEATURE
    assert scope.command_log == []


def test_dho900_afg_without_module_is_unsupported() -> None:
    """DGSTatus=0(非S型)はAFGコマンドを1つも送らず UNSUPPORTED_FEATURE。"""
    scope = _DgStatusScope(dg_status=False)
    driver = make_driver(scope, "dho900")

    with pytest.raises(ScopeError) as excinfo:
        driver.get_afg_config(1)

    assert excinfo.value.code == ErrorCode.UNSUPPORTED_FEATURE
    assert scope.command_log == [":SYSTem:DGSTatus?"]


def test_dho900_afg_presence_is_cached() -> None:
    scope = _DgStatusScope(dg_status=False)
    driver = make_driver(scope, "dho900")

    for _ in range(2):
        with pytest.raises(ScopeError):
            driver.get_afg_config(1)

    assert scope.command_log == [":SYSTem:DGSTatus?"]


def test_dho900_afg_with_module_passes_the_gate() -> None:
    """DGSTatus=1(S型)はゲート通過後、番号なし :SOURce で送信する。

    FakeScopeはMHO方言(番号つき)のため後続は沈黙=TIMEOUTになるが、
    「最初にゲートを照会し、番号なしプレフィクスを組み立てた」ことは
    送信ログで確認できる。
    """
    scope = _DgStatusScope(dg_status=True)
    driver = make_driver(scope, "dho900")

    with pytest.raises(ScopeError) as excinfo:
        driver.get_afg_config(1)

    assert excinfo.value.code == ErrorCode.TIMEOUT
    assert scope.command_log[0] == ":SYSTem:DGSTatus?"
    assert scope.command_log[1].startswith(":SOURce:")  # 番号なしプレフィクス


def test_dho900_afg_channel_2_rejected_before_sending() -> None:
    scope = _DgStatusScope(dg_status=True)
    driver = make_driver(scope, "dho900")

    with pytest.raises(ScopeError) as excinfo:
        driver.get_afg_config(2)

    assert excinfo.value.code == ErrorCode.INVALID_PARAMETER
    assert scope.command_log == []


def test_clear_measurements_sends_the_dialect_command(
    driver: ScopeDriver, scope: FakeScope
) -> None:
    """実機検証済みニモニック(mho98-measure-clear.md)だけを送る。"""
    scope.command_log.clear()

    driver.clear_measurements()

    assert [c for c in scope.command_log if "?" not in c] == [":MEASure:DELete"]


def test_clear_measurements_unsupported_profile_sends_nothing(
    generic_driver: ScopeDriver, scope: FakeScope
) -> None:
    with pytest.raises(ScopeError) as excinfo:
        generic_driver.clear_measurements()

    assert excinfo.value.code == ErrorCode.UNSUPPORTED_FEATURE
    assert scope.command_log == []


# --------------------------------------------------------------------------
# DHO800/900 方言(ガイドベース・実機未検証)
# --------------------------------------------------------------------------


def test_dho_autoset_sends_the_dialect_command(
    dho_driver: ScopeDriver, scope: FakeScope
) -> None:
    scope.command_log.clear()

    dho_driver.autoset()

    assert [c for c in scope.command_log if "?" not in c] == [":AUToset"]


def test_dho_clear_measurements_sends_clear_not_delete(
    dho_driver: ScopeDriver, scope: FakeScope
) -> None:
    """DHO800/900系のニモニックは :MEASure:CLEar(ガイド3.17.3)。"""
    scope.command_log.clear()

    dho_driver.clear_measurements()

    assert [c for c in scope.command_log if "?" not in c] == [":MEASure:CLEar"]


def test_dho_screenshot_sends_png_argument(
    dho_driver: ScopeDriver, scope: FakeScope
) -> None:
    """DHO系の既定はBMPなので、PNG引数を付けたコマンドをそのまま送る。"""
    scope.command_log.clear()

    assert dho_driver.capture_screenshot_bytes().startswith(PNG_MAGIC)
    assert scope.command_log == [":DISPlay:DATA? PNG"]


def test_dho_decode_is_unsupported_and_sends_nothing(
    dho_driver: ScopeDriver, scope: FakeScope
) -> None:
    """デコードは実機検証まで未宣言 — 不在がそのままゲート(送信ゼロ)。"""
    with pytest.raises(ScopeError) as excinfo:
        dho_driver.configure_decode(1, "uart")

    assert excinfo.value.code == ErrorCode.UNSUPPORTED_FEATURE
    assert scope.command_log == []


def test_measure_unsupported_name_is_not_sent(
    generic_driver: ScopeDriver, scope: FakeScope
) -> None:
    """genericプロファイルに vavg は無い。実機へ送らず UNSUPPORTED_FEATURE。"""
    with pytest.raises(ScopeError) as excinfo:
        generic_driver.measure("CH1", ["vavg"])

    error = excinfo.value
    assert error.code == ErrorCode.UNSUPPORTED_FEATURE
    assert error.detail["measurement"] == "vavg"
    assert sent(scope, ":MEAS") == []


def test_measure_rejects_unknown_name_without_sending(
    driver: ScopeDriver, scope: FakeScope
) -> None:
    with pytest.raises(ScopeError) as excinfo:
        driver.measure("CH1", ["bogus"])

    assert excinfo.value.code == ErrorCode.UNSUPPORTED_FEATURE
    assert sent(scope, ":MEAS") == []


def test_measure_invalid_value_is_unknown_quality(
    driver: ScopeDriver, monkeypatch: pytest.MonkeyPatch
) -> None:
    """実機は測定不能時に 9.9E+37 を返す。値としては採用しない。"""
    monkeypatch.setitem(fake_scope_module._MEASURE_VALUES, "VPP", "9.9E+37")

    result = driver.measure("CH1", ["vpp"])[0]

    assert result.value is None
    assert result.quality == "unknown"


def test_measure_negative_invalid_value_is_unknown_quality(
    driver: ScopeDriver, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setitem(fake_scope_module._MEASURE_VALUES, "VMIN", "-9.9E+37")

    result = driver.measure("CH1", ["vmin"])[0]

    assert result.value is None
    assert result.quality == "unknown"


# --------------------------------------------------------------------------
# スクリーンショット
# --------------------------------------------------------------------------


def test_capture_screenshot_returns_png(driver: ScopeDriver) -> None:
    assert driver.capture_screenshot_bytes().startswith(PNG_MAGIC)


def test_capture_screenshot_unsupported(scope: FakeScope) -> None:
    profile = Profile(name="x", confidence="generic", capabilities={"screenshot": False})
    driver = ScopeDriver(ScpiSession(FakeTransport(scope)), profile)

    with pytest.raises(ScopeError) as excinfo:
        driver.capture_screenshot_bytes()

    assert excinfo.value.code == ErrorCode.UNSUPPORTED_FEATURE
    assert scope.command_log == []


# --------------------------------------------------------------------------
# 波形
# --------------------------------------------------------------------------


def test_waveform_preamble_parse() -> None:
    preamble = WaveformPreamble.parse(
        "0,0,1000,1,2.000000E-6,-1.000000E-3,0.000000,6.8267E-02,0,128"
    )

    assert preamble.format == 0
    assert preamble.type == 0
    assert preamble.points == 1000
    assert preamble.count == 1
    assert preamble.xincrement == pytest.approx(2.0e-6)
    assert preamble.xorigin == pytest.approx(-1.0e-3)
    assert preamble.xreference == 0.0
    assert preamble.yincrement == pytest.approx(6.8267e-2)
    assert preamble.yorigin == 0.0
    assert preamble.yreference == 128.0


def test_waveform_preamble_rejects_wrong_element_count() -> None:
    with pytest.raises(ScopeError) as excinfo:
        WaveformPreamble.parse("0,0,1000")

    assert excinfo.value.code == ErrorCode.SCPI_ERROR


def test_read_waveform(driver: ScopeDriver, scope: FakeScope) -> None:
    raw = driver.read_waveform("CH1")

    assert raw.preamble.points == 1000
    assert len(raw.data) == 1000
    assert scope.waveform["source"] == "CHAN1"
    assert scope.waveform["mode"] == "NORM"
    assert scope.waveform["format"] == "BYTE"


def test_read_waveform_sends_long_form_setup(driver: ScopeDriver, scope: FakeScope) -> None:
    driver.read_waveform("CH1")

    for command in (
        ":WAVeform:SOURce CHANnel1",
        ":WAVeform:MODE NORMal",
        ":WAVeform:FORMat BYTE",
        ":WAVeform:PREamble?",
        ":WAVeform:DATA?",
    ):
        assert command in scope.command_log


def test_read_waveform_without_max_points_does_not_set_range(
    driver: ScopeDriver, scope: FakeScope
) -> None:
    driver.read_waveform("CH1")

    assert sent(scope, ":WAVEFORM:STOP") == []
    assert sent(scope, ":WAVEFORM:STAR") == []


def test_read_waveform_limits_points(driver: ScopeDriver, scope: FakeScope) -> None:
    driver.read_waveform("CH1", max_points=500)

    assert ":WAVeform:STARt 1" in scope.command_log
    assert ":WAVeform:STOP 500" in scope.command_log
    assert scope.waveform["stop"] == 500


def test_read_waveform_max_points_above_available_uses_points(
    driver: ScopeDriver, scope: FakeScope
) -> None:
    driver.read_waveform("CH1", max_points=5000)

    assert ":WAVeform:STOP 1000" in scope.command_log


def test_read_waveform_unsupported(scope: FakeScope) -> None:
    profile = Profile(
        name="x", confidence="generic", capabilities={"waveform_download": False}
    )
    driver = ScopeDriver(ScpiSession(FakeTransport(scope)), profile)

    with pytest.raises(ScopeError) as excinfo:
        driver.read_waveform("CH1")

    assert excinfo.value.code == ErrorCode.UNSUPPORTED_FEATURE
    assert scope.command_log == []


# --------------------------------------------------------------------------
# 設定(SAFE_WRITE)
# --------------------------------------------------------------------------


def test_set_channel_enabled(driver: ScopeDriver, scope: FakeScope) -> None:
    assert driver.set_channel_enabled("CH2", True) is True
    assert scope.channels[2]["display"] is True
    assert ":CHANnel2:DISPlay ON" in scope.command_log

    assert driver.set_channel_enabled("CH2", False) is False
    assert ":CHANnel2:DISPlay OFF" in scope.command_log


def test_set_channel_scale_is_applied_verbatim(driver: ScopeDriver, scope: FakeScope) -> None:
    """MHO98は1-2-5にスナップしない(実測)。3.0がそのまま適用される。"""
    applied = driver.set_channel_scale("CH1", 3.0)

    assert applied == pytest.approx(3.0)
    assert ":CHANnel1:SCALe 3.0" in scope.command_log


def test_set_channel_scale_snapping_device_reports_applied() -> None:
    """スナップする機種では requested≠applied となり、appliedを返す。"""
    scope = FakeScope(snap_to_125=True)
    driver = make_driver(scope)

    assert driver.set_channel_scale("CH1", 3.0) == pytest.approx(2.0)


def test_set_channel_offset(driver: ScopeDriver) -> None:
    assert driver.set_channel_offset("CH1", -1.5) == pytest.approx(-1.5)


def test_set_channel_coupling(driver: ScopeDriver, scope: FakeScope) -> None:
    assert driver.set_channel_coupling("CH1", "AC") == "AC"
    assert ":CHANnel1:COUPling AC" in scope.command_log


def test_set_channel_coupling_normalizes_case(driver: ScopeDriver) -> None:
    assert driver.set_channel_coupling("CH1", "gnd") == "GND"


def test_set_channel_coupling_rejects_unknown(driver: ScopeDriver, scope: FakeScope) -> None:
    with pytest.raises(ScopeError) as excinfo:
        driver.set_channel_coupling("CH1", "XY")

    assert excinfo.value.code == ErrorCode.INVALID_PARAMETER
    assert scope.command_log == []


def test_set_channel_probe_ratio(driver: ScopeDriver, scope: FakeScope) -> None:
    assert driver.set_channel_probe_ratio("CH1", 1.0) == pytest.approx(1.0)
    assert ":CHANnel1:PROBe 1.0" in scope.command_log


def test_set_channel_probe_ratio_validated_against_limits(
    driver: ScopeDriver, scope: FakeScope
) -> None:
    with pytest.raises(ScopeError) as excinfo:
        driver.set_channel_probe_ratio("CH1", 3.0)

    assert excinfo.value.code == ErrorCode.INVALID_PARAMETER
    assert scope.command_log == []


def test_set_channel_probe_ratio_passthrough_without_limits(
    generic_driver: ScopeDriver,
) -> None:
    """limitsに probe_ratio が無いプロファイルでは検証せず実機に委ねる。"""
    assert generic_driver.set_channel_probe_ratio("CH1", 3.0) == pytest.approx(3.0)


def test_set_channel_bwlimit(driver: ScopeDriver, scope: FakeScope) -> None:
    assert driver.set_channel_bwlimit("CH1", True) is True
    assert scope.channels[1]["bwlimit"] != "OFF"

    assert driver.set_channel_bwlimit("CH1", False) is False
    assert scope.channels[1]["bwlimit"] == "OFF"


def test_set_channel_bwlimit_uses_profile_token(driver: ScopeDriver, scope: FakeScope) -> None:
    driver.set_channel_bwlimit("CH1", True)

    assert ":CHANnel1:BWLimit 20M" in scope.command_log


def test_set_channel_bwlimit_reflected_in_channel_state(driver: ScopeDriver) -> None:
    driver.set_channel_bwlimit("CH1", True)

    assert driver.get_channel("CH1").bandwidth_limit is True


def test_set_channel_bwlimit_on_undeclared_is_not_sent(
    generic_driver: ScopeDriver, scope: FakeScope
) -> None:
    """帯域制限の「入」の値は機種依存。dialect 未宣言なら送らない。"""
    with pytest.raises(ScopeError) as excinfo:
        generic_driver.set_channel_bwlimit("CH1", True)

    assert excinfo.value.code == ErrorCode.UNSUPPORTED_FEATURE
    assert excinfo.value.detail["dialect"] == "bwlimit_on"
    assert scope.command_log == []


def test_set_channel_bwlimit_off_allowed_without_dialect(
    generic_driver: ScopeDriver, scope: FakeScope
) -> None:
    """`OFF` は全機種共通ニモニックなので未宣言でも送れる。"""
    assert generic_driver.set_channel_bwlimit("CH1", False) is False
    assert ":CHANnel1:BWLimit OFF" in scope.command_log


def test_set_channel_impedance(driver: ScopeDriver, scope: FakeScope) -> None:
    assert driver.set_channel_impedance("CH1", "50") == "50"
    assert ":CHANnel1:IMPedance FIFT" in scope.command_log
    assert driver.set_channel_impedance("CH1", "1M") == "1M"


def test_set_channel_impedance_50_unsupported_by_profile(
    generic_driver: ScopeDriver, scope: FakeScope
) -> None:
    with pytest.raises(ScopeError) as excinfo:
        generic_driver.set_channel_impedance("CH1", "50")

    assert excinfo.value.code == ErrorCode.UNSUPPORTED_FEATURE
    assert scope.command_log == []


def test_set_channel_impedance_1m_unsupported_without_capability(
    generic_driver: ScopeDriver, scope: FakeScope
) -> None:
    """`:CHANnel<n>:IMPedance` 自体が未確認の機種には "1M" も送らない。"""
    with pytest.raises(ScopeError) as excinfo:
        generic_driver.set_channel_impedance("CH1", "1M")

    assert excinfo.value.code == ErrorCode.UNSUPPORTED_FEATURE
    assert excinfo.value.detail["capability"] == "impedance_control"
    assert scope.command_log == []


def test_set_channel_impedance_rejects_unknown(driver: ScopeDriver) -> None:
    with pytest.raises(ScopeError) as excinfo:
        driver.set_channel_impedance("CH1", "75")

    assert excinfo.value.code == ErrorCode.INVALID_PARAMETER


def test_set_timebase_scale(driver: ScopeDriver, scope: FakeScope) -> None:
    assert driver.set_timebase_scale(1e-3) == pytest.approx(1e-3)
    assert ":TIMebase:MAIN:SCALe 0.001" in scope.command_log


def test_set_timebase_position(driver: ScopeDriver) -> None:
    assert driver.set_timebase_position(2e-4) == pytest.approx(2e-4)


# --------------------------------------------------------------------------
# トリガ
# --------------------------------------------------------------------------


def test_set_trigger_edge_all_fields(driver: ScopeDriver, scope: FakeScope) -> None:
    state = driver.set_trigger_edge(
        source="CH2", level_v=1.25, slope="falling", sweep_mode="normal"
    )

    assert state.source == "CH2"
    assert state.level_v == pytest.approx(1.25)
    assert state.slope == "falling"
    assert state.sweep_mode == "normal"
    assert ":TRIGger:MODE EDGE" in scope.command_log
    assert ":TRIGger:EDGE:SOURce CHANnel2" in scope.command_log
    assert ":TRIGger:EDGE:SLOPe NEGative" in scope.command_log
    assert ":TRIGger:SWEep NORMal" in scope.command_log


def test_set_trigger_edge_omits_unspecified(driver: ScopeDriver, scope: FakeScope) -> None:
    """未指定の項目には**書き込まない**(最後の read-back での問い合わせは除く)。"""
    driver.set_trigger_edge(level_v=0.5)

    writes = [c for c in scope.command_log if "?" not in c]
    assert writes == [":TRIGger:MODE EDGE", ":TRIGger:EDGE:LEVel 0.5"]
    assert scope.trigger["source"] == "CHAN1"
    assert scope.trigger["slope"] == "POS"
    assert scope.trigger["sweep"] == "AUTO"


def test_set_trigger_edge_rejects_bad_slope(driver: ScopeDriver, scope: FakeScope) -> None:
    with pytest.raises(ScopeError) as excinfo:
        driver.set_trigger_edge(slope="sideways")

    assert excinfo.value.code == ErrorCode.INVALID_PARAMETER
    assert scope.command_log == []


def test_set_trigger_edge_rejects_bad_source(driver: ScopeDriver, scope: FakeScope) -> None:
    with pytest.raises(ScopeError) as excinfo:
        driver.set_trigger_edge(source="CH9")

    assert excinfo.value.code == ErrorCode.INVALID_PARAMETER
    assert scope.command_log == []


# --------------------------------------------------------------------------
# Acquisition
# --------------------------------------------------------------------------


def test_run_stop_single(driver: ScopeDriver, scope: FakeScope) -> None:
    driver.stop()
    assert scope.acquisition == "STOP"

    driver.run()
    assert scope.acquisition == "RUN"

    driver.single()
    assert scope.acquisition == "SINGLE"
    assert driver.get_trigger_status() == "WAIT"


def test_autoset(driver: ScopeDriver, scope: FakeScope) -> None:
    driver.autoset()

    assert ":AUToset" in scope.command_log
    assert scope.acquisition == "RUN"


# --------------------------------------------------------------------------
# スクリーンショットのタイムアウト猶予
# --------------------------------------------------------------------------


class RecordingTransport(FakeTransport):
    """query_binary に渡る timeout_s を記録するだけの薄いラッパ。"""

    def __init__(self, scope: FakeScope) -> None:
        super().__init__(scope)
        self.binary_timeouts: list[float | None] = []

    def query_binary(self, command: str, timeout_s: float | None = None) -> bytes:
        self.binary_timeouts.append(timeout_s)
        return super().query_binary(command, timeout_s)


def test_capture_screenshot_uses_long_timeout(scope: FakeScope) -> None:
    """約97KBの転送が既定5秒で切れないよう、方言既定の30秒を渡す。"""
    transport = RecordingTransport(scope)
    transport.open()
    driver = ScopeDriver(ScpiSession(transport), load_profile("mho98"))

    driver.capture_screenshot_bytes()

    assert transport.binary_timeouts == [30.0]


def test_capture_screenshot_timeout_from_dialect(scope: FakeScope) -> None:
    transport = RecordingTransport(scope)
    transport.open()
    profile = Profile(
        name="x",
        confidence="generic",
        capabilities={"screenshot": True},
        dialect={"screenshot_timeout_s": 5},
    )
    driver = ScopeDriver(ScpiSession(transport), profile)

    driver.capture_screenshot_bytes()

    assert transport.binary_timeouts == [5.0]


# --------------------------------------------------------------------------
# トリガソース(非チャンネル)
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("given", "expected"),
    [
        ("EXT", "EXT"),
        ("ext", "EXT"),
        ("EXT5", "EXT5"),
        ("ACLINE", "ACLINE"),
        ("ACLine", "ACLINE"),
        ("ACL", "ACLINE"),
        ("D0", "D0"),
        ("d15", "D15"),
        ("CHAN2", "CHANnel2"),
    ],
)
def test_trigger_source_accepts_non_channel_sources(
    driver: ScopeDriver, given: str, expected: str
) -> None:
    assert driver._trigger_source(given) == expected


@pytest.mark.parametrize("given", ["D16", "FOO", "EXT2", ""])
def test_trigger_source_rejects_unknown(driver: ScopeDriver, given: str) -> None:
    with pytest.raises(ScopeError) as excinfo:
        driver._trigger_source(given)

    assert excinfo.value.code == ErrorCode.INVALID_PARAMETER
    assert excinfo.value.detail["source"] == given


@pytest.mark.parametrize("raw", ["EXT", "ACL", "D3", "CHAN2"])
def test_read_back_source_is_writable(driver: ScopeDriver, raw: str) -> None:
    """読み取り正規化を通した値は、そのまま書き戻し先の検証を通る(往復成立)。"""
    driver._trigger_source(driver._normalize_source(raw))


def test_set_trigger_edge_roundtrips_channel_source(
    driver: ScopeDriver, scope: FakeScope
) -> None:
    """get_trigger が返した source をそのまま書き戻せる(FakeScopeはCH系のみ受理)。"""
    state = driver.get_trigger()

    assert driver.set_trigger_edge(source=state.source).source == state.source
    assert ":TRIGger:EDGE:SOURce CHANnel1" in scope.command_log


def test_set_trigger_edge_sends_ext_source(driver: ScopeDriver, scope: FakeScope) -> None:
    """EXT はチャンネル数検証を通さず、正規化形のまま送る。"""
    with pytest.raises(ScopeError):  # FakeScope は EXT 書き込みを受理しない
        driver.set_trigger_edge(source="ext")

    assert ":TRIGger:EDGE:SOURce EXT" in scope.command_log


# --------------------------------------------------------------------------
# オプション照会(docs/verification/mho98-unlicensed.md)
# --------------------------------------------------------------------------

OPTION_QUERY = ":SYSTem:OPTion:STATus?"
OPTION_TYPES = load_profile("mho98").dialect["option_types"]


class _BrokenOptionScope(FakeScope):
    """BND だけ壊れた応答をするフェイク(部分失敗が他を巻き込まない検証用)。"""

    def __init__(self, response: bytes | None) -> None:
        super().__init__()
        self.response = response

    def handle(self, command: str) -> bytes | None:
        if command.strip().upper().endswith(" BND"):
            self.command_log.append(command)
            if self.response is None:  # 無応答(実機の沈黙)
                raise self._silent(fake_scope_module.COMMAND_ERROR)
            return self.response
        return super().handle(command)


def test_installed_options_queries_every_declared_type(
    driver: ScopeDriver, scope: FakeScope
) -> None:
    options = driver.installed_options()

    assert set(options) == set(OPTION_TYPES)
    assert all(value is True for value in options.values())  # FakeScopeの既定
    sent_queries = sent(scope, OPTION_QUERY.upper())
    assert len(sent_queries) == len(OPTION_TYPES)
    assert sorted(c.split(" ", 1)[1] for c in sent_queries) == sorted(
        OPTION_TYPES.values()
    )


def test_installed_options_reports_missing_licenses() -> None:
    """未ライセンス実機相当(AFG50 / RLU-05 のみ導入済み)。"""
    scope = FakeScope(options={"AFG50": True, "RLU-05": True})
    driver = make_driver(scope)

    options = driver.installed_options()

    assert options["afg_50mhz"] is True
    assert options["memory_500mpts"] is True
    assert options["bundle"] is False
    assert options["can_fd"] is False


def test_installed_options_is_cached(driver: ScopeDriver, scope: FakeScope) -> None:
    """ライセンスは接続中に変わらない。2回目は1コマンドも送らない。"""
    first = driver.installed_options()
    count = len(scope.command_log)

    assert driver.installed_options() == first
    assert len(scope.command_log) == count


def test_installed_options_unsupported_profile_sends_nothing(
    generic_driver: ScopeDriver, scope: FakeScope
) -> None:
    """genericプロファイルは `:SYSTem:OPTion:*` を宣言しない → 送信ゼロ。"""
    with pytest.raises(ScopeError) as excinfo:
        generic_driver.installed_options()

    assert excinfo.value.code == ErrorCode.UNSUPPORTED_FEATURE
    assert scope.command_log == []


def test_installed_options_without_type_table_sends_nothing(scope: FakeScope) -> None:
    profile = Profile(
        name="x", confidence="generic", dialect={"option_query": OPTION_QUERY}
    )
    driver = ScopeDriver(ScpiSession(FakeTransport(scope)), profile)

    with pytest.raises(ScopeError) as excinfo:
        driver.installed_options()

    assert excinfo.value.code == ErrorCode.UNSUPPORTED_FEATURE
    assert scope.command_log == []


@pytest.mark.parametrize("response", [b"YES", None])
def test_installed_options_isolates_a_broken_type(response: bytes | None) -> None:
    """1件が解釈不能/無応答でも、残りの結果は失われない。"""
    driver = make_driver(_BrokenOptionScope(response))

    options = driver.installed_options()

    assert options["bundle"] is None
    assert options["afg_50mhz"] is True
    assert len(options) == len(OPTION_TYPES)


class _DisconnectingOptionScope(FakeScope):
    """BND の照会で切断相当の失敗を起こすフェイク。"""

    def handle(self, command: str) -> bytes | None:
        if command.strip().upper().endswith(" BND"):
            raise ScopeError(ErrorCode.DEVICE_DISCONNECTED, "link lost", {})
        return super().handle(command)


def test_installed_options_propagates_disconnection() -> None:
    """切断はNoneに降格せず伝播する(全Noneをキャッシュして成功に見せない)。"""
    driver = make_driver(_DisconnectingOptionScope())

    with pytest.raises(ScopeError) as exc_info:
        driver.installed_options()

    assert exc_info.value.code == ErrorCode.DEVICE_DISCONNECTED


def test_installed_options_cache_returns_a_copy(driver: ScopeDriver) -> None:
    """返却dictを書き換えてもキャッシュへ波及しない。"""
    first = driver.installed_options()
    first["bundle"] = None

    assert driver.installed_options()["bundle"] is True


# --------------------------------------------------------------------------
# シリアルデコード(docs/verification/mho98-unlicensed.md 3章)
# --------------------------------------------------------------------------


def bus_writes(scope: FakeScope) -> list[str]:
    """`:BUS` への書き込みのみ(問い合わせ・エラーキュー確認を除く)。"""
    return [c for c in scope.command_log if "?" not in c and c.upper().startswith(":BUS")]


UART_SETTINGS = {
    "tx_source": "CH1",
    "baud_bps": 115200,
    "data_bits": 8,
    "parity": "none",
    "stop_bits": 1,
    "tx_threshold_v": 1.65,
}


def test_event_table_with_enabled_false_is_rejected_before_sending(
    driver: ScopeDriver, scope: FakeScope
) -> None:
    """表示OFF指定のままイベントテーブルは有効化できない(送信前拒否)。"""
    with pytest.raises(ScopeError) as excinfo:
        driver.configure_decode(1, "uart", enabled=False, event_table=True)

    assert excinfo.value.code == ErrorCode.INVALID_PARAMETER
    assert bus_writes(scope) == []


def test_event_table_requires_display_already_on_when_enabled_omitted(
    driver: ScopeDriver, scope: FakeScope
) -> None:
    """enabled省略時は現在の表示状態を確認し、OFFなら送信前に拒否する。"""
    with pytest.raises(ScopeError) as excinfo:
        driver.configure_decode(1, "uart", event_table=True)

    assert excinfo.value.code == ErrorCode.INVALID_PARAMETER
    assert bus_writes(scope) == []


def test_event_table_with_display_already_on_succeeds(driver: ScopeDriver) -> None:
    driver.configure_decode(1, "uart", enabled=True)

    applied = driver.configure_decode(1, "uart", event_table=True)

    assert applied["event_table"] is True


def test_configure_decode_uart_sends_mode_first(
    driver: ScopeDriver, scope: FakeScope
) -> None:
    """順序: :MODE → 表示形式 → プロトコル設定 → :DISPlay → :EVENt。"""
    driver.configure_decode(
        1,
        "uart",
        enabled=True,
        event_table=True,
        data_format="ascii",
        settings=UART_SETTINGS,
    )

    assert bus_writes(scope) == [
        ":BUS1:MODE RS232",
        ":BUS1:FORMat ASCii",
        ":BUS1:RS232:TX CHANnel1",
        ":BUS1:RS232:BAUD 115200",
        ":BUS1:RS232:DBITs 8",
        ":BUS1:RS232:PARity NONE",
        ":BUS1:RS232:SBITs 1",
        ":BUS1:THReshold 1.65,TX",
        ":BUS1:DISPlay ON",
        ":BUS1:EVENt ON",
    ]


def test_configure_decode_returns_applied_readback(driver: ScopeDriver) -> None:
    applied = driver.configure_decode(
        1, "uart", enabled=True, data_format="ascii", settings=UART_SETTINGS
    )

    assert applied == {
        "bus": 1,
        "protocol": "uart",
        "enabled": True,
        "data_format": "ascii",
        "settings": {
            "tx_source": "CH1",
            "baud_bps": 115200,
            "data_bits": 8,
            "parity": "none",
            "stop_bits": 1,
            "tx_threshold_v": pytest.approx(1.65),
        },
    }


def test_configure_decode_omits_unspecified_items(
    driver: ScopeDriver, scope: FakeScope
) -> None:
    """未指定の項目にはコマンドを1件も送らない。"""
    applied = driver.configure_decode(2, "lin")

    assert bus_writes(scope) == [":BUS2:MODE LIN"]
    assert applied == {"bus": 2, "protocol": "lin", "settings": {}}


def test_configure_decode_i2c(driver: ScopeDriver, scope: FakeScope) -> None:
    driver.configure_decode(
        1,
        "i2c",
        settings={
            "scl_source": "CH1",
            "sda_source": "D3",
            "swap_sda_scl": True,
            "address_bits": 10,
            "scl_threshold_v": 1.4,
        },
    )

    assert bus_writes(scope) == [
        ":BUS1:MODE IIC",
        ":BUS1:IIC:SCLK:SOURce CHANnel1",
        ":BUS1:IIC:SDA:SOURce D3",
        ":BUS1:IIC:EXCHange ON",
        ":BUS1:IIC:ADDBits 10",
        ":BUS1:THReshold 1.4,SCL",
    ]


def test_configure_decode_spi(driver: ScopeDriver, scope: FakeScope) -> None:
    applied = driver.configure_decode(
        3,
        "spi",
        settings={
            "clk_source": "CH1",
            "clk_slope": "rising",
            "mosi_source": "CH2",
            "miso_source": "off",
            "cs_source": "CH3",
            "cs_polarity": "low",
            "frame_mode": "timeout",
            "timeout_s": 1e-6,
            "data_bits": 16,
            "endian": "msb",
            "polarity": "high",
            "cs_threshold_v": 1.2,
        },
    )

    assert bus_writes(scope) == [
        ":BUS3:MODE SPI",
        ":BUS3:SPI:SCLK:SOURce CHANnel1",
        ":BUS3:SPI:SCLK:SLOPe POSitive",
        ":BUS3:SPI:MOSI:SOURce CHANnel2",
        ":BUS3:SPI:MISO:SOURce OFF",
        ":BUS3:SPI:SS:SOURce CHANnel3",
        ":BUS3:SPI:SS:POLarity LOW",
        ":BUS3:SPI:MODE TIMeout",
        ":BUS3:SPI:TIMeout:TIME 1e-06",
        ":BUS3:SPI:DBITs 16",
        ":BUS3:SPI:ENDian MSB",
        ":BUS3:SPI:POLarity HIGH",
        ":BUS3:THReshold 1.2,CS",
    ]
    assert applied["settings"]["miso_source"] == "OFF"
    assert applied["settings"]["timeout_s"] == pytest.approx(1e-6)


def test_configure_decode_can(driver: ScopeDriver, scope: FakeScope) -> None:
    driver.configure_decode(
        4,
        "can",
        settings={
            "source": "CH2",
            "signal_type": "differential",
            "baud_bps": 500000,
            "sample_point_percent": 75,
            "threshold_v": 2.0,
        },
    )

    assert bus_writes(scope) == [
        ":BUS4:MODE CAN",
        ":BUS4:CAN:SOURce CHANnel2",
        ":BUS4:CAN:STYPe DIFFerential",
        ":BUS4:CAN:BAUD 500000",
        ":BUS4:CAN:SPOint 75",
        ":BUS4:THReshold 2.0,CAN",
    ]


def test_configure_decode_lin(driver: ScopeDriver, scope: FakeScope) -> None:
    driver.configure_decode(
        1,
        "lin",
        settings={
            "source": "D0",
            "baud_bps": 19200,
            "parity_enabled": True,
            "standard": "v2x",
            "threshold_v": 1.0,
        },
    )

    assert bus_writes(scope) == [
        ":BUS1:MODE LIN",
        ":BUS1:LIN:SOURce D0",
        ":BUS1:LIN:BAUD 19200",
        ":BUS1:LIN:PARity ON",
        ":BUS1:LIN:STANdard V2X",
        ":BUS1:THReshold 1.0,LIN",
    ]


def test_configure_decode_parallel(driver: ScopeDriver, scope: FakeScope) -> None:
    applied = driver.configure_decode(
        1,
        "parallel",
        settings={
            "clk_source": "CH4",
            "clk_slope": "falling",
            "bus": "user",
            "bus_width": 2,
            "bit_sources": ["CH1", "D3"],
            "endian": "lsb",
            "polarity": "negative",
        },
    )

    assert bus_writes(scope) == [
        ":BUS1:MODE PARallel",
        ":BUS1:PARallel:CLK CHANnel4",
        ":BUS1:PARallel:SLOPe NEGative",
        ":BUS1:PARallel:BUS USER",
        ":BUS1:PARallel:WIDTh 2",
        ":BUS1:PARallel:ENDian LSB",
        ":BUS1:PARallel:POLarity NEGative",
        ":BUS1:PARallel:BITX 0",
        ":BUS1:PARallel:SOURce CHANnel1",
        ":BUS1:PARallel:BITX 1",
        ":BUS1:PARallel:SOURce D3",
    ]
    assert applied["settings"]["bus"] == "user"
    assert applied["settings"]["bus_width"] == 2
    assert applied["settings"]["bit_sources"] == ["CH1", "D3"]


def test_configure_decode_parallel_sends_bus_before_width(
    driver: ScopeDriver, scope: FakeScope
) -> None:
    """送信順は**呼び出し側のキー順ではなく表の並び**(`:BUS` が `:WIDTh` の前提)。"""
    driver.configure_decode(1, "parallel", settings={"bus_width": 4, "bus": "user"})

    assert bus_writes(scope) == [
        ":BUS1:MODE PARallel",
        ":BUS1:PARallel:BUS USER",
        ":BUS1:PARallel:WIDTh 4",
    ]


def test_configure_decode_parallel_width_without_user_bus_is_device_rejected(
    driver: ScopeDriver,
) -> None:
    """`bus` を `user` にしないままの `bus_width` は**機器が** `-200` で拒す。

    ホスト側では結合を検証しない(機器自身がエラーを自己申告する経路に任せる)。
    """
    with pytest.raises(ScopeError) as excinfo:
        driver.configure_decode(1, "parallel", settings={"bus_width": 4})

    assert excinfo.value.code == ErrorCode.SCPI_ERROR


def test_configure_decode_parallel_bit_sources_use_the_current_width(
    driver: ScopeDriver, scope: FakeScope
) -> None:
    """`bus_width` を同時に指定しないときは現在のバス幅(FakeScope既定=8)で検証する。"""
    driver.configure_decode(1, "parallel", settings={"bus": "user"})
    scope.command_log.clear()

    driver.configure_decode(1, "parallel", settings={"bit_sources": ["D0", "D1"]})

    assert bus_writes(scope) == [
        ":BUS1:MODE PARallel",
        ":BUS1:PARallel:BITX 0",
        ":BUS1:PARallel:SOURce D0",
        ":BUS1:PARallel:BITX 1",
        ":BUS1:PARallel:SOURce D1",
    ]


@pytest.mark.parametrize(
    "settings",
    [
        {"bus": "d16_d0"},
        {"bus_width": 17},
        {"bit_sources": "CH1"},
        {"bit_sources": []},
        {"bit_sources": ["CH1", "CH9"]},
        {"bit_sources": ["D16"]},
        {"bus_width": 2, "bit_sources": ["CH1", "CH2", "CH3"]},
    ],
)
def test_configure_decode_parallel_rejects_bad_bit_sources_sends_nothing(
    driver: ScopeDriver, scope: FakeScope, settings: dict
) -> None:
    with pytest.raises(ScopeError) as excinfo:
        driver.configure_decode(1, "parallel", settings=settings)

    assert excinfo.value.code == ErrorCode.INVALID_PARAMETER
    assert scope.command_log == []


# -- 送信前に失敗する検証 --------------------------------------------------


def test_configure_decode_unknown_protocol_sends_nothing(
    driver: ScopeDriver, scope: FakeScope
) -> None:
    """オプション必須のプロトコル(i2s等)は宣言が無い = 送信前に拒否。"""
    with pytest.raises(ScopeError) as excinfo:
        driver.configure_decode(1, "i2s")

    assert excinfo.value.code == ErrorCode.UNSUPPORTED_FEATURE
    assert "uart" in excinfo.value.message
    assert scope.command_log == []


def test_configure_decode_unsupported_profile_sends_nothing(
    generic_driver: ScopeDriver, scope: FakeScope
) -> None:
    with pytest.raises(ScopeError) as excinfo:
        generic_driver.configure_decode(1, "uart", settings={"baud_bps": 9600})

    assert excinfo.value.code == ErrorCode.UNSUPPORTED_FEATURE
    assert scope.command_log == []


@pytest.mark.parametrize("bus", [0, 5, -1])
def test_configure_decode_bus_out_of_range_sends_nothing(
    driver: ScopeDriver, scope: FakeScope, bus: int
) -> None:
    with pytest.raises(ScopeError) as excinfo:
        driver.configure_decode(bus, "uart")

    assert excinfo.value.code == ErrorCode.INVALID_PARAMETER
    assert scope.command_log == []


def test_configure_decode_unknown_setting_key_sends_nothing(
    driver: ScopeDriver, scope: FakeScope
) -> None:
    """他プロトコルのキーを混ぜた場合、許容キー一覧を detail で返す。"""
    with pytest.raises(ScopeError) as excinfo:
        driver.configure_decode(1, "uart", settings={"scl_source": "CH1"})

    error = excinfo.value
    assert error.code == ErrorCode.INVALID_PARAMETER
    assert error.detail["allowed"] == sorted(
        [
            "tx_source",
            "rx_source",
            "baud_bps",
            "data_bits",
            "parity",
            "stop_bits",
            "endian",
            "polarity",
            "tx_threshold_v",
            "rx_threshold_v",
        ]
    )
    assert scope.command_log == []


@pytest.mark.parametrize(
    ("protocol", "settings"),
    [
        ("uart", {"baud_bps": 0}),
        ("uart", {"baud_bps": 20_000_001}),
        ("uart", {"data_bits": 4}),
        ("uart", {"stop_bits": 3}),
        ("uart", {"parity": "mark"}),
        ("uart", {"tx_source": "CH9"}),
        ("spi", {"data_bits": 33}),
        ("spi", {"timeout_s": 11.0}),
        ("spi", {"frame_mode": "auto"}),
        ("can", {"baud_bps": 9600}),
        ("can", {"sample_point_percent": 95}),
        ("lin", {"baud_bps": 1200}),
        ("lin", {"standard": "v3x"}),
        ("parallel", {"bus_width": 0}),
    ],
)
def test_configure_decode_rejects_out_of_range_sends_nothing(
    driver: ScopeDriver, scope: FakeScope, protocol: str, settings: dict
) -> None:
    with pytest.raises(ScopeError) as excinfo:
        driver.configure_decode(1, protocol, settings=settings)

    assert excinfo.value.code == ErrorCode.INVALID_PARAMETER
    assert scope.command_log == []


@pytest.mark.parametrize(
    ("protocol", "settings"),
    [
        ("uart", {"tx_source": "off", "rx_source": "OFF"}),
        ("spi", {"mosi_source": "off", "miso_source": "off"}),
    ],
)
def test_configure_decode_rejects_both_sources_off(
    driver: ScopeDriver, scope: FakeScope, protocol: str, settings: dict
) -> None:
    """両方OFFはデコード対象が無い(機器も受理しない)。"""
    with pytest.raises(ScopeError) as excinfo:
        driver.configure_decode(1, protocol, settings=settings)

    assert excinfo.value.code == ErrorCode.INVALID_PARAMETER
    assert scope.command_log == []


# -- 読み取り --------------------------------------------------------------


def test_get_decode_config_round_trip(driver: ScopeDriver) -> None:
    driver.configure_decode(
        1, "uart", enabled=True, event_table=True, settings=UART_SETTINGS
    )

    config = driver.get_decode_config(1)

    assert config["bus"] == 1
    assert config["protocol"] == "uart"
    assert config["enabled"] is True
    assert config["event_table"] is True
    assert config["data_format"] == "hex"
    assert config["settings"]["baud_bps"] == 115200
    assert config["settings"]["tx_source"] == "CH1"
    assert config["settings"]["rx_threshold_v"] == pytest.approx(0.0)
    assert set(config["settings"]) == set(UART_SETTINGS) | {
        "rx_source",
        "endian",
        "polarity",
        "rx_threshold_v",
    }


def test_get_decode_config_parallel_reads_bit_sources_by_walking_bitx(
    driver: ScopeDriver, scope: FakeScope
) -> None:
    """ビット別ソースは一括問い合わせが無いので `:BITX` で1ビットずつ選び直して読む。"""
    driver.configure_decode(
        1,
        "parallel",
        settings={"bus": "user", "bus_width": 3, "bit_sources": ["CH1", "D3", "CH2"]},
    )
    scope.buses[1]["PARallel"]["bitx"] = 2  # 読み取り後に復元されること
    scope.command_log.clear()

    config = driver.get_decode_config(1, include_bit_sources=True)

    assert config["settings"]["bus"] == "user"
    assert config["settings"]["bit_sources"] == ["CH1", "D3", "CH2"]
    assert scope.buses[1]["PARallel"]["bitx"] == 2
    assert sent(scope, ":BITX") == [
        ":BUS1:PARallel:BITX?",
        ":BUS1:PARallel:BITX 0",
        ":BUS1:PARallel:BITX 1",
        ":BUS1:PARallel:BITX 2",
        ":BUS1:PARallel:BITX 2",
    ]


def test_get_decode_config_omits_bit_sources_by_default(
    driver: ScopeDriver, scope: FakeScope
) -> None:
    """既定の読み取りは**書き込みを一切行わない**(`:BITX` を1本も送らない)。"""
    driver.configure_decode(
        1,
        "parallel",
        settings={"bus": "user", "bus_width": 2, "bit_sources": ["CH1", "D3"]},
    )
    scope.command_log.clear()

    config = driver.get_decode_config(1)

    assert config["settings"]["bus"] == "user"
    assert "bit_sources" not in config["settings"]
    assert [c for c in scope.command_log if "?" not in c] == []


def test_get_decode_config_enum_readback_is_repeatable(driver: ScopeDriver) -> None:
    """同じトークンを2度読んでも同じ意味的な値になる(列挙表を壊さない)。

    `CHAN1` のように**短形が長形の前置になっていない**トークンは、読み取りが
    表を破壊すると2回目で `SCPI_ERROR` になる。
    """
    driver.configure_decode(1, "parallel", settings={"bus": "ch2"})

    assert driver.get_decode_config(1)["settings"]["bus"] == "ch2"
    assert driver.get_decode_config(1)["settings"]["bus"] == "ch2"


def test_get_decode_config_parallel_skips_bit_sources_unless_user(
    driver: ScopeDriver, scope: FakeScope
) -> None:
    """データソースが USER 以外のバスでは `:BITX` を1本も送らない(機器が拒否する)。"""
    driver.configure_decode(1, "parallel", settings={"bus": "ch1"})
    scope.command_log.clear()

    config = driver.get_decode_config(1, include_bit_sources=True)

    assert config["settings"]["bus"] == "ch1"
    assert "bit_sources" not in config["settings"]
    assert sent(scope, ":BITX") == []
    assert sent(scope, ":PARALLEL:SOUR") == []


def test_get_decode_config_uses_threshold_query_form(
    driver: ScopeDriver, scope: FakeScope
) -> None:
    """閾値の問い合わせは `:BUS<n>:THReshold? <type>`(実機実測の形)。"""
    driver.configure_decode(1, "uart")
    scope.command_log.clear()

    driver.get_decode_config(1)

    assert ":BUS1:THReshold? TX" in scope.command_log


def test_get_decode_config_reports_unsupported_mode_as_is(
    driver: ScopeDriver, scope: FakeScope
) -> None:
    """オプション必須プロトコルに設定されたバスは、生の名前だけ返す。

    ライセンス適用後に `:BUS1:MODE?` が `IIS` を返しても読み取りが壊れず、かつ
    未確認ニモニック(`:BUS1:IIS:...`)を1件も送らないこと。
    """
    scope.buses[1]["mode"] = "IIS"

    config = driver.get_decode_config(1)

    assert config["protocol"] == "iis"
    assert config["settings"] == {}
    assert sent(scope, ":IIS") == []


def test_get_decode_config_unsupported_profile_sends_nothing(
    generic_driver: ScopeDriver, scope: FakeScope
) -> None:
    with pytest.raises(ScopeError) as excinfo:
        generic_driver.get_decode_config(1)

    assert excinfo.value.code == ErrorCode.UNSUPPORTED_FEATURE
    assert scope.command_log == []


# -- イベントテーブル(get_decode_events)-----------------------------------


class EventTableScope(FakeScope):
    """`:BUS<n>:DATA?` のペイロードだけ差し替えるフェイク(異常系の観測用)。"""

    payload = b""

    def handle(self, command: str) -> bytes | None:
        text = command.strip().upper()
        if text.startswith(":BUS") and text.endswith(":DATA?"):
            self.command_log.append(command)
            length = f"{len(self.payload):09d}".encode("ascii")
            return b"#9" + length + self.payload + b"\n"
        return super().handle(command)


def enable_event_table(driver: ScopeDriver, scope: FakeScope, protocol: str) -> None:
    driver.configure_decode(1, protocol, enabled=True, event_table=True)
    scope.command_log.clear()


def test_get_decode_events_parses_the_guide_example(
    driver: ScopeDriver, scope: FakeScope
) -> None:
    """プログラミングガイド 3.4 の PARALLEL 例をそのまま解釈する。"""
    enable_event_table(driver, scope, "parallel")

    result = driver.get_decode_events(1)

    assert result["bus"] == 1
    assert result["protocol"] == "parallel"
    assert result["columns"] == ["time_s", "data"]
    assert result["events"] == [
        {"time_s": pytest.approx(-2.47e-6), "data": "0"},
        {"time_s": pytest.approx(-2.444e-6), "data": "1"},
    ]
    assert isinstance(result["events"][0]["time_s"], float)


def test_get_decode_events_parses_the_device_rs232_columns(
    driver: ScopeDriver, scope: FakeScope
) -> None:
    """実機実測のRS232ヘッダ(`Time,Tx/Rx,Data,Error,`)を列追加なしで扱える。"""
    enable_event_table(driver, scope, "uart")

    result = driver.get_decode_events(1)

    assert result["protocol"] == "uart"
    assert result["columns"] == ["time_s", "tx_rx", "data", "error"]
    assert result["events"][0] == {
        "time_s": pytest.approx(-2.47e-6),
        "tx_rx": "Tx",
        "data": "0x55",
        "error": "",
    }


def test_get_decode_events_accepts_an_empty_table() -> None:
    """実機実測: 信号なしでは行が0件(ヘッダのみ)でも正常に返る。"""
    scope = EventTableScope()
    scope.payload = b"RS232\nTime,Tx/Rx,Data,Error,\n"
    driver = make_driver(scope)
    enable_event_table(driver, scope, "uart")
    driver.stop()

    result = driver.get_decode_events(1)

    assert result["columns"] == ["time_s", "tx_rx", "data", "error"]
    assert result["events"] == []
    assert result["warnings"] == []


def test_get_decode_events_accepts_an_empty_payload() -> None:
    scope = EventTableScope()
    driver = make_driver(scope)
    enable_event_table(driver, scope, "uart")

    result = driver.get_decode_events(1)

    assert result["columns"] == []
    assert result["events"] == []


def test_get_decode_events_rejects_a_malformed_payload() -> None:
    scope = EventTableScope()
    scope.payload = b"RS232\nnot a table at all\n"
    driver = make_driver(scope)
    enable_event_table(driver, scope, "uart")

    with pytest.raises(ScopeError) as excinfo:
        driver.get_decode_events(1)

    assert excinfo.value.code == ErrorCode.SCPI_ERROR
    assert "not a table at all" in excinfo.value.detail["raw"]


def test_get_decode_events_rejects_a_malformed_time_column() -> None:
    scope = EventTableScope()
    scope.payload = b"RS232\nTime,Data,\nlater,0x55,\n"
    driver = make_driver(scope)
    enable_event_table(driver, scope, "uart")

    with pytest.raises(ScopeError) as excinfo:
        driver.get_decode_events(1)

    assert excinfo.value.code == ErrorCode.SCPI_ERROR
    assert excinfo.value.detail["raw"] == "later"


def test_get_decode_events_skips_the_query_while_the_bus_is_off(
    driver: ScopeDriver, scope: FakeScope
) -> None:
    """バス非表示ならデータ問い合わせを送らずに警告を返す。"""
    driver.configure_decode(1, "uart")
    scope.command_log.clear()

    result = driver.get_decode_events(1)

    assert result["events"] == []
    assert result["columns"] == []
    assert result["protocol"] == "uart"
    assert any("configure_decode(bus=1, enabled=true)" in w for w in result["warnings"])
    assert sent(scope, ":DATA?") == []


def test_get_decode_events_skips_the_query_while_the_event_table_is_off(
    driver: ScopeDriver, scope: FakeScope
) -> None:
    driver.configure_decode(1, "uart", enabled=True)
    scope.command_log.clear()

    result = driver.get_decode_events(1)

    assert result["events"] == []
    assert any(
        "configure_decode(bus=1, event_table=true)" in w for w in result["warnings"]
    )
    assert sent(scope, ":DATA?") == []


def test_get_decode_events_warns_while_acquisition_is_running(
    driver: ScopeDriver, scope: FakeScope
) -> None:
    enable_event_table(driver, scope, "uart")

    warnings = driver.get_decode_events(1)["warnings"]

    assert any("acquisition is running" in w for w in warnings)
    # 警告するだけで取り込みは止めない(read-only)
    assert sent(scope, "STOP") == []


def test_get_decode_events_is_quiet_while_stopped(
    driver: ScopeDriver, scope: FakeScope
) -> None:
    enable_event_table(driver, scope, "uart")
    driver.stop()

    assert driver.get_decode_events(1)["warnings"] == []


def test_get_decode_events_checks_the_error_queue(
    driver: ScopeDriver, scope: FakeScope
) -> None:
    """値が返ってもエラーが積まれることがある(オプションゲート済みの実機挙動)。"""
    enable_event_table(driver, scope, "uart")
    scope.error_queue.append(fake_scope_module.OUT_OF_RANGE)

    with pytest.raises(ScopeError) as excinfo:
        driver.get_decode_events(1)

    assert excinfo.value.code == ErrorCode.SCPI_ERROR


@pytest.mark.parametrize("bus", [0, 5])
def test_get_decode_events_bus_out_of_range_sends_nothing(
    driver: ScopeDriver, scope: FakeScope, bus: int
) -> None:
    with pytest.raises(ScopeError) as excinfo:
        driver.get_decode_events(bus)

    assert excinfo.value.code == ErrorCode.INVALID_PARAMETER
    assert scope.command_log == []


def test_get_decode_events_unsupported_profile_sends_nothing(
    generic_driver: ScopeDriver, scope: FakeScope
) -> None:
    with pytest.raises(ScopeError) as excinfo:
        generic_driver.get_decode_events(1)

    assert excinfo.value.code == ErrorCode.UNSUPPORTED_FEATURE
    assert scope.command_log == []


# --------------------------------------------------------------------------
# 信号発生(docs/verification/mho98-afg.md)
# --------------------------------------------------------------------------


def afg_writes(scope: FakeScope) -> list[str]:
    """`:SOURce` への書き込みのみ(問い合わせ・エラーキュー確認を除く)。"""
    return [
        c for c in scope.command_log if "?" not in c and c.upper().startswith(":SOUR")
    ]


def test_configure_afg_sends_items_in_the_fixed_order(
    driver: ScopeDriver, scope: FakeScope
) -> None:
    """順序: 波形 → インピーダンス → 周波数 → 振幅 → オフセット → 位相 → デューティ → 対称性。

    インピーダンスと周波数が振幅の、振幅がオフセットの許容範囲を決めるため、
    範囲の広い側から順に送る(ガイド3.25)。
    """
    driver.configure_afg(
        1,
        waveform="square",
        frequency_hz=2000.0,
        amplitude_vpp=1.0,
        offset_v=0.5,
        phase_deg=90.0,
        duty_percent=60.0,
        symmetry_percent=40.0,
        impedance="50",
    )

    assert afg_writes(scope) == [
        ":SOURce1:FUNCtion SQUare",
        ":SOURce1:IMPedance FIFTy",
        ":SOURce1:FREQuency 2000.0",
        ":SOURce1:VOLTage:AMPLitude 1.0",
        ":SOURce1:VOLTage:OFFSet 0.5",
        ":SOURce1:PHASe 90.0",
        ":SOURce1:FUNCtion:SQUare:DUTY 60.0",
        ":SOURce1:FUNCtion:RAMP:SYMMetry 40.0",
    ]


def test_configure_afg_never_touches_the_output(
    driver: ScopeDriver, scope: FakeScope
) -> None:
    """設定は出力状態に一切触れない(有効化は別Toolの責務)。"""
    driver.configure_afg(1, waveform="sine", amplitude_vpp=1.0, frequency_hz=1000.0)

    assert [c for c in scope.command_log if "OUTP" in c.upper()] == []


def test_configure_afg_omits_unspecified_items(
    driver: ScopeDriver, scope: FakeScope
) -> None:
    """未指定の項目にはコマンドを1件も送らない。"""
    applied = driver.configure_afg(2, frequency_hz=2500.0)

    assert afg_writes(scope) == [":SOURce2:FREQuency 2500.0"]
    assert applied == {"channel": 2, "frequency_hz": pytest.approx(2500.0)}


def test_configure_afg_returns_applied_readback(driver: ScopeDriver) -> None:
    """応答は短形(`SQU`)と長形(`FIFTy`)が混在する。両方を意味的な値へ戻す。"""
    applied = driver.configure_afg(
        2, waveform="square", frequency_hz=2000.0, impedance="50"
    )

    assert applied == {
        "channel": 2,
        "waveform": "square",
        "impedance": "50",
        "frequency_hz": pytest.approx(2000.0),
    }


def test_configure_afg_accepts_every_declared_waveform(driver: ScopeDriver) -> None:
    """プロファイルが宣言する13種すべてを送信・解釈できること。"""
    waveforms = load_profile("mho98").dialect["afg_waveforms"]

    for name in waveforms:
        assert driver.configure_afg(1, waveform=name)["waveform"] == name


@pytest.mark.parametrize(
    "kwargs",
    [
        {"channel": 0, "waveform": "sine"},
        {"channel": 3, "waveform": "sine"},  # :SOURce3 は実機を沈黙させる
        {"channel": True, "waveform": "sine"},
        {"channel": 1, "waveform": "pulse"},  # 実機に存在しない波形
        {"channel": 1, "impedance": "1M"},  # オシロ入力側の値は使えない
        {"channel": 1, "phase_deg": 361.0},
        {"channel": 1, "phase_deg": -1.0},
        {"channel": 1, "duty_percent": 0.0},
        {"channel": 1, "duty_percent": 100.0},
        {"channel": 1, "symmetry_percent": -1.0},
        {"channel": 1, "symmetry_percent": 101.0},
        {"channel": 1, "frequency_hz": 0.0},
        {"channel": 1, "amplitude_vpp": 0.0},
        {"channel": 1, "amplitude_vpp": "1"},
        {"channel": 1},  # 変更する項目が1件も無い
    ],
)
def test_configure_afg_rejects_before_sending(
    driver: ScopeDriver, scope: FakeScope, kwargs: dict
) -> None:
    with pytest.raises(ScopeError) as excinfo:
        driver.configure_afg(kwargs.pop("channel"), **kwargs)

    assert excinfo.value.code == ErrorCode.INVALID_PARAMETER
    assert scope.command_log == []


def test_configure_afg_validates_every_item_before_sending(
    driver: ScopeDriver, scope: FakeScope
) -> None:
    """1項目でも不正なら、正しい項目も含めて1コマンドも送らない。"""
    with pytest.raises(ScopeError):
        driver.configure_afg(1, waveform="sine", duty_percent=0.0)

    assert scope.command_log == []


def test_configure_afg_unsupported_profile_sends_nothing(
    generic_driver: ScopeDriver, scope: FakeScope
) -> None:
    """`afg_prefix` 未宣言(=非対応)。宣言の不在がそのままゲート。"""
    with pytest.raises(ScopeError) as excinfo:
        generic_driver.configure_afg(1, waveform="sine")

    assert excinfo.value.code == ErrorCode.UNSUPPORTED_FEATURE
    assert scope.command_log == []


def test_get_afg_config_round_trip(driver: ScopeDriver, scope: FakeScope) -> None:
    driver.configure_afg(1, waveform="ramp", frequency_hz=2500.0, symmetry_percent=40.0)
    scope.command_log.clear()

    config = driver.get_afg_config(1)

    assert config == {
        "channel": 1,
        "output": False,
        "waveform": "ramp",
        "impedance": "highz",
        "frequency_hz": pytest.approx(2500.0),
        "amplitude_vpp": pytest.approx(5.0),
        "offset_v": pytest.approx(0.0),
        "phase_deg": pytest.approx(0.0),
        "duty_percent": pytest.approx(50.0),
        "symmetry_percent": pytest.approx(40.0),
        "modulation": {
            "enabled": False,
            "type": "am",
            "am_depth_percent": pytest.approx(100.0),
            "frequency_hz": pytest.approx(100.0),
            "waveform": "sine",
        },
    }
    # 問い合わせ9件(既存項目)+ 変調5件(STATe/TYPe/DEPTh/INTernal FREQ/FUNC)= 14件
    assert len(scope.command_log) == 14
    assert [c for c in scope.command_log if "?" not in c] == []


def test_get_afg_config_reads_the_output_state(
    driver: ScopeDriver, scope: FakeScope
) -> None:
    """出力状態は**読むだけ**(書き込みは別Toolの責務)。"""
    scope.afg[1]["output"] = True

    config = driver.get_afg_config(1)

    assert config["output"] is True
    assert [c for c in scope.command_log if "OUTP" in c.upper()] == [
        ":SOURce1:OUTPut:STATe?"
    ]


@pytest.mark.parametrize("channel", [0, 3])
def test_get_afg_config_channel_out_of_range_sends_nothing(
    driver: ScopeDriver, scope: FakeScope, channel: int
) -> None:
    with pytest.raises(ScopeError) as excinfo:
        driver.get_afg_config(channel)

    assert excinfo.value.code == ErrorCode.INVALID_PARAMETER
    assert scope.command_log == []


def test_get_afg_config_unsupported_profile_sends_nothing(
    generic_driver: ScopeDriver, scope: FakeScope
) -> None:
    with pytest.raises(ScopeError) as excinfo:
        generic_driver.get_afg_config(1)

    assert excinfo.value.code == ErrorCode.UNSUPPORTED_FEATURE
    assert scope.command_log == []


def test_set_afg_output_on_sets_and_reads_back(
    driver: ScopeDriver, scope: FakeScope
) -> None:
    """出力ON: set → エラーキュー確認 → read-back の1往復のみ。"""
    assert driver.set_afg_output(1, True) is True

    assert afg_writes(scope) == [":SOURce1:OUTPut:STATe ON"]
    assert ":SOURce1:OUTPut:STATe?" in scope.command_log
    assert scope.afg[1]["output"] is True


def test_set_afg_output_off_uses_the_off_token(
    driver: ScopeDriver, scope: FakeScope
) -> None:
    scope.afg[2]["output"] = True

    assert driver.set_afg_output(2, False) is False

    assert afg_writes(scope) == [":SOURce2:OUTPut:STATe OFF"]
    assert scope.afg[2]["output"] is False


def test_set_afg_output_touches_no_other_item(
    driver: ScopeDriver, scope: FakeScope
) -> None:
    """出力制御は設定項目に一切触れない(波形も振幅も読まない・書かない)。"""
    driver.set_afg_output(1, True)

    assert [c for c in scope.command_log if "OUTP" not in c.upper()] == [
        ":SYSTem:ERRor?"
    ]


@pytest.mark.parametrize("channel", [0, 3, True])
def test_set_afg_output_channel_out_of_range_sends_nothing(
    driver: ScopeDriver, scope: FakeScope, channel: object
) -> None:
    """`:SOURce3` は実機のSCPIサーバーを沈黙させる。範囲検証は送信前に行う。"""
    with pytest.raises(ScopeError) as excinfo:
        driver.set_afg_output(channel, True)

    assert excinfo.value.code == ErrorCode.INVALID_PARAMETER
    assert scope.command_log == []


def test_set_afg_output_unsupported_profile_sends_nothing(
    generic_driver: ScopeDriver, scope: FakeScope
) -> None:
    with pytest.raises(ScopeError) as excinfo:
        generic_driver.set_afg_output(1, True)

    assert excinfo.value.code == ErrorCode.UNSUPPORTED_FEATURE
    assert scope.command_log == []


# --------------------------------------------------------------------------
# 信号発生: 変調(ガイド3.25.15-25)
# --------------------------------------------------------------------------


def test_configure_afg_modulation_sends_the_fixed_order(
    driver: ScopeDriver, scope: FakeScope
) -> None:
    """順序: TYPe → STATe ON → 深さ/偏移 → FREQuency → FUNCtion(quirk対応)。

    実機はMOD:STATe OFF中のパラメータ書き込みを黙って無視するため、
    有効化はパラメータより先に送る(mho98-afg.md 6章)。"""
    applied = driver.configure_afg(
        1,
        modulation={
            "enabled": True,
            "type": "am",
            "am_depth_percent": 50.0,
            "frequency_hz": 1000.0,
            "waveform": "sine",
        },
    )

    assert afg_writes(scope) == [
        ":SOURce1:MOD:TYPe AM",
        ":SOURce1:MOD:STATe ON",
        ":SOURce1:MOD:AM:DEPTh 50.0",
        ":SOURce1:MOD:AM:INTernal:FREQuency 1000.0",
        ":SOURce1:MOD:AM:INTernal:FUNCtion SINusoid",
    ]
    assert applied["modulation"] == {
        "type": "am",
        "am_depth_percent": pytest.approx(50.0),
        "frequency_hz": pytest.approx(1000.0),
        "waveform": "sine",
        "enabled": True,
    }


def test_configure_afg_modulation_routes_frequency_to_the_given_type(
    driver: ScopeDriver, scope: FakeScope
) -> None:
    """`type="fm"` と同時指定した `frequency_hz` はFM配下へ送る(現在値は問い合わせない)。"""
    driver.configure_afg(
        1, modulation={"type": "fm", "frequency_hz": 250.0, "enabled": True}
    )

    assert afg_writes(scope) == [
        ":SOURce1:MOD:TYPe FM",
        ":SOURce1:MOD:STATe ON",
        ":SOURce1:MOD:FM:INTernal:FREQuency 250.0",
    ]
    # :MOD:TYPe? はTYPeコマンドのread-back1回のみ(ルーティング用の別問い合わせは無い)
    assert [c for c in scope.command_log if c == ":SOURce1:MOD:TYPe?"] == [
        ":SOURce1:MOD:TYPe?"
    ]


def test_configure_afg_modulation_routes_to_the_current_device_type(
    driver: ScopeDriver, scope: FakeScope
) -> None:
    """`type` 未指定の `frequency_hz` は現在のtypeを1回だけ問い合わせてルーティングする。"""
    scope.afg[1]["mod_type"] = "FM"
    scope.afg[1]["mod_state"] = True  # OFF中はパラメータが無視されるため前提を作る

    driver.configure_afg(1, modulation={"frequency_hz": 300.0})

    assert scope.command_log[0] == ":SOURce1:MOD:TYPe?"
    assert afg_writes(scope) == [":SOURce1:MOD:FM:INTernal:FREQuency 300.0"]


@pytest.mark.parametrize(
    "path",
    [
        "D:/x.csv;:SYSTem:ERRor?",  # ';' はSCPIコマンドセパレータ(注入)
        "D:/x.csv:extra",  # ':' もヘッダ区切りのため接頭辞以外では拒否
        "D:/x'y.csv",
    ],
)
def test_configure_afg_arb_file_rejects_scpi_metacharacters(
    driver: ScopeDriver, scope: FakeScope, path: str
) -> None:
    """接頭辞以降はホワイトリスト検証(SCPIインジェクション対策)。"""
    with pytest.raises(ScopeError) as excinfo:
        driver.configure_afg(1, arb_file=path)

    assert excinfo.value.code == ErrorCode.INVALID_PARAMETER
    assert afg_writes(scope) == []


def test_configure_afg_modulation_params_while_off_reject_before_sending(
    driver: ScopeDriver, scope: FakeScope
) -> None:
    """MOD OFF中のパラメータのみ指定は書き込みゼロで拒否(実機は黙って無視するため)。"""
    with pytest.raises(ScopeError) as excinfo:
        driver.configure_afg(1, modulation={"am_depth_percent": 50.0})

    assert excinfo.value.code == ErrorCode.INVALID_PARAMETER
    assert afg_writes(scope) == []


def test_configure_afg_modulation_disable_sends_state_last(
    driver: ScopeDriver, scope: FakeScope
) -> None:
    """無効化はパラメータをON中に書いてから STATe OFF(最後)。"""
    scope.afg[1]["mod_state"] = True

    driver.configure_afg(
        1, modulation={"am_depth_percent": 40.0, "enabled": False}
    )

    assert afg_writes(scope) == [
        ":SOURce1:MOD:AM:DEPTh 40.0",
        ":SOURce1:MOD:STATe OFF",
    ]
    assert scope.afg[1]["am"]["depth"] == 40.0


def test_configure_afg_modulation_unknown_key_rejects_before_sending(
    driver: ScopeDriver, scope: FakeScope
) -> None:
    with pytest.raises(ScopeError) as excinfo:
        driver.configure_afg(1, modulation={"depth": 50.0})

    assert excinfo.value.code == ErrorCode.INVALID_PARAMETER
    assert excinfo.value.detail["allowed"] == sorted(
        {
            "enabled",
            "type",
            "am_depth_percent",
            "fm_deviation_hz",
            "pm_deviation_deg",
            "frequency_hz",
            "waveform",
        }
    )
    assert scope.command_log == []


@pytest.mark.parametrize(
    "modulation",
    [
        {"am_depth_percent": 121.0},
        {"am_depth_percent": -1.0},
        {"pm_deviation_deg": 361.0},
        {"pm_deviation_deg": -1.0},
        {"fm_deviation_hz": 0.0},
        {"frequency_hz": 0.0, "type": "am"},
        {"type": "xx"},
        {"waveform": "pulse", "type": "am"},
        {"enabled": "on"},
    ],
)
def test_configure_afg_modulation_rejects_out_of_range_before_sending(
    driver: ScopeDriver, scope: FakeScope, modulation: dict
) -> None:
    with pytest.raises(ScopeError) as excinfo:
        driver.configure_afg(1, modulation=modulation)

    assert excinfo.value.code == ErrorCode.INVALID_PARAMETER
    assert scope.command_log == []


def test_configure_afg_modulation_unsupported_profile_sends_nothing(
    generic_driver: ScopeDriver, scope: FakeScope
) -> None:
    """`afg_prefix` 未宣言(=AFG自体が非対応)。宣言の不在がそのままゲート。"""
    with pytest.raises(ScopeError) as excinfo:
        generic_driver.configure_afg(1, modulation={"enabled": True})

    assert excinfo.value.code == ErrorCode.UNSUPPORTED_FEATURE
    assert scope.command_log == []


def test_get_afg_config_reads_the_effective_modulation_type_only(
    driver: ScopeDriver, scope: FakeScope
) -> None:
    driver.configure_afg(
        1, modulation={"type": "pm", "pm_deviation_deg": 45.0, "enabled": True}
    )
    scope.command_log.clear()

    config = driver.get_afg_config(1)

    assert config["modulation"] == {
        "enabled": True,
        "type": "pm",
        "pm_deviation_deg": pytest.approx(45.0),
        "frequency_hz": pytest.approx(100.0),
        "waveform": "sine",
    }
    assert [c for c in scope.command_log if "?" not in c] == []


# --------------------------------------------------------------------------
# 信号発生: ARBファイル選択(ガイド3.25.3)
# --------------------------------------------------------------------------


def test_configure_afg_arb_file_sent_after_waveform_before_frequency(
    driver: ScopeDriver, scope: FakeScope
) -> None:
    applied = driver.configure_afg(
        1, waveform="arb", arb_file="D:/test.csv", frequency_hz=1000.0
    )

    assert afg_writes(scope) == [
        ":SOURce1:FUNCtion ARB",
        ":SOURce1:LOAD:ARBitrary D:/test.csv",
        ":SOURce1:FREQuency 1000.0",
    ]
    assert applied["arb_file"] == "D:/test.csv"


@pytest.mark.parametrize(
    "arb_file",
    [
        "test.csv",  # プレフィックス無し
        "E:/test.csv",  # C:/ でもD:/でもない
        "D:/no suffix",  # 拡張子無し
        "D:/bad name.csv",  # 空白を含む
        "D:/bad\tname.csv",  # 制御文字を含む
        "",
        123,
    ],
)
def test_configure_afg_arb_file_rejects_before_sending(
    driver: ScopeDriver, scope: FakeScope, arb_file: object
) -> None:
    with pytest.raises(ScopeError) as excinfo:
        driver.configure_afg(1, waveform="sine", arb_file=arb_file)

    assert excinfo.value.code == ErrorCode.INVALID_PARAMETER
    assert scope.command_log == []


def test_configure_afg_arb_file_only_is_accepted(
    driver: ScopeDriver, scope: FakeScope
) -> None:
    """`waveform` を指定しなくても `arb_file` 単独で送れる。"""
    applied = driver.configure_afg(1, arb_file="C:/local.bin")

    assert afg_writes(scope) == [":SOURce1:LOAD:ARBitrary C:/local.bin"]
    assert applied == {"channel": 1, "arb_file": "C:/local.bin"}


# --------------------------------------------------------------------------
# 位相同期(sync_afg_phase、ガイド3.25.7)
# --------------------------------------------------------------------------


def test_sync_afg_phase_sends_exactly_the_command(
    driver: ScopeDriver, scope: FakeScope
) -> None:
    driver.sync_afg_phase(1)

    assert [c for c in scope.command_log if "?" not in c] == [
        ":SOURce1:PHASe:SYNChronize"
    ]


def test_sync_afg_phase_channel_out_of_range_sends_nothing(
    driver: ScopeDriver, scope: FakeScope
) -> None:
    with pytest.raises(ScopeError) as excinfo:
        driver.sync_afg_phase(3)

    assert excinfo.value.code == ErrorCode.INVALID_PARAMETER
    assert scope.command_log == []


def test_sync_afg_phase_unsupported_profile_sends_nothing(
    generic_driver: ScopeDriver, scope: FakeScope
) -> None:
    with pytest.raises(ScopeError) as excinfo:
        generic_driver.sync_afg_phase(1)

    assert excinfo.value.code == ErrorCode.UNSUPPORTED_FEATURE
    assert scope.command_log == []


# --------------------------------------------------------------------------
# MATH演算(:MATH<n>)のゲート
# --------------------------------------------------------------------------


def test_math_channels_comes_from_the_profile(
    driver: ScopeDriver, generic_driver: ScopeDriver
) -> None:
    """MHO98は4ch。未宣言のgenericは 0(= 非対応)。"""
    assert driver.math_channels == 4
    assert generic_driver.math_channels == 0


def test_math_prefix_builds_the_command_head(driver: ScopeDriver) -> None:
    assert driver._math_prefix(3) == (3, ":MATH3")


def test_math_unsupported_profile_sends_nothing(
    generic_driver: ScopeDriver, scope: FakeScope
) -> None:
    """`math_channels` 未宣言のプロファイルへは1バイトも送らない。"""
    with pytest.raises(ScopeError) as excinfo:
        generic_driver._math_prefix(1)

    assert excinfo.value.code == ErrorCode.UNSUPPORTED_FEATURE
    assert scope.command_log == []


def test_math_channel_out_of_range_sends_nothing(
    driver: ScopeDriver, scope: FakeScope
) -> None:
    """`:MATH5` は実機のSCPIサーバーを沈黙させ得るため、送信前に拒否する。"""
    with pytest.raises(ScopeError) as excinfo:
        driver._math_prefix(5)

    assert excinfo.value.code == ErrorCode.INVALID_PARAMETER
    assert scope.command_log == []

    with pytest.raises(ScopeError) as excinfo:
        driver._math_prefix(0)

    assert excinfo.value.code == ErrorCode.INVALID_PARAMETER
    assert scope.command_log == []


# --------------------------------------------------------------------------
# MATH演算: configure_math(ガイド3.16章)
# --------------------------------------------------------------------------


def math_writes(scope: FakeScope) -> list[str]:
    """`:MATH` への書き込みのみ(問い合わせ・エラーキュー確認を除く)。"""
    return [
        c for c in scope.command_log if "?" not in c and c.upper().startswith(":MATH")
    ]


def test_configure_math_sends_exact_scpi(
    driver: ScopeDriver, scope: FakeScope
) -> None:
    """演算子・ソースはガイド逐語のニモニックで送る。"""
    applied = driver.configure_math(
        1, operator="subtract", source1="CH2", source2="REF3"
    )

    assert math_writes(scope) == [
        ":MATH1:OPERator SUBTract",
        ":MATH1:SOURce1 CHANnel2",
        ":MATH1:SOURce2 REF3",
    ]
    assert applied == {
        "channel": 1,
        "operator": "subtract",
        "source1": "CH2",
        "source2": "REF3",
    }


def test_configure_math_fft_subtree_uses_guide_mnemonics(
    driver: ScopeDriver, scope: FakeScope
) -> None:
    applied = driver.configure_math(
        2,
        operator="fft",
        fft={
            "window": "blackman",
            "unit": "vrms",
            "mode": "average",
            "average_count": 100,
            "freq_start_hz": 0.0,
            "freq_end_hz": 1.0e7,
            "search_enabled": True,
            "search_order": "frequency",
        },
    )

    assert math_writes(scope) == [
        ":MATH2:OPERator FFT",
        ":MATH2:FFT:WINDow BLACkman",
        ":MATH2:FFT:UNIT VRMS",
        ":MATH2:FFT:MODE AVERage",
        ":MATH2:FFT:AVCNt 100",
        ":MATH2:FFT:FREQuency:STARt 0.0",
        ":MATH2:FFT:FREQuency:END 10000000.0",
        ":MATH2:FFT:SEARch:ENABle ON",
        ":MATH2:FFT:SEARch:ORDer FREQorder",
    ]
    assert applied["fft"] == {
        "window": "blackman",
        "unit": "vrms",
        "mode": "average",
        "average_count": 100,
        "freq_start_hz": 0.0,
        "freq_end_hz": 1.0e7,
        "search_enabled": True,
        "search_order": "frequency",
    }


def test_configure_math_fft_source_uses_its_own_command(
    driver: ScopeDriver, scope: FakeScope
) -> None:
    """FFTの入力chは `:FFT:SOURce`(`:SOURce1` ではない。ガイド3.16.14)。"""
    applied = driver.configure_math(2, operator="fft", fft={"source": "CH3"})

    assert math_writes(scope) == [
        ":MATH2:OPERator FFT",
        ":MATH2:FFT:SOURce CHANnel3",
    ]
    assert applied["fft"]["source"] == "CH3"


def test_configure_math_fft_source_obeys_the_cascade_rule(
    driver: ScopeDriver, scope: FakeScope
) -> None:
    """`SOURce1` と同じトークン検証(カスケードは m<n のみ)。送信ゼロで拒否。"""
    with pytest.raises(ScopeError) as excinfo:
        driver.configure_math(1, fft={"source": "MATH2"})

    assert excinfo.value.code == ErrorCode.INVALID_PARAMETER
    assert scope.command_log == []


def test_configure_math_filter_subtree(driver: ScopeDriver, scope: FakeScope) -> None:
    applied = driver.configure_math(
        1, operator="bandpass", filter={"type": "bandpass", "w1_hz": 1e5, "w2_hz": 1e6}
    )

    assert math_writes(scope) == [
        ":MATH1:OPERator BPASs",
        ":MATH1:FILTer:TYPE BPASs",
        ":MATH1:FILTer:W1 100000.0",
        ":MATH1:FILTer:W2 1000000.0",
    ]
    assert applied["filter"] == {"type": "bandpass", "w1_hz": 1e5, "w2_hz": 1e6}


def test_configure_math_display_on_is_sent_first(
    driver: ScopeDriver, scope: FakeScope
) -> None:
    """表示OFF中の書き込み無視quirk対策(AFGの変調STATeと同じ流儀)。"""
    driver.configure_math(1, display=True, operator="add", scale=0.5)

    assert math_writes(scope) == [
        ":MATH1:DISPlay ON",
        ":MATH1:OPERator ADD",
        ":MATH1:SCALe 0.5",
    ]


def test_configure_math_display_off_is_sent_last(
    driver: ScopeDriver, scope: FakeScope
) -> None:
    driver.configure_math(1, display=False, operator="add", invert=True)

    assert math_writes(scope) == [
        ":MATH1:OPERator ADD",
        ":MATH1:INVert ON",
        ":MATH1:DISPlay OFF",
    ]


def test_configure_math_logic_sources(driver: ScopeDriver, scope: FakeScope) -> None:
    applied = driver.configure_math(1, operator="and", lsource1="D0", lsource2="CH4")

    assert math_writes(scope) == [
        ":MATH1:OPERator AND",
        ":MATH1:LSOurce1 D0",
        ":MATH1:LSOurce2 CHANnel4",
    ]
    assert applied["lsource1"] == "D0"
    assert applied["lsource2"] == "CH4"


def test_configure_math_cascade_rule_rejects_same_or_higher_math(
    driver: ScopeDriver, scope: FakeScope
) -> None:
    """`:MATH<n>` のソースに使える MATH<m> は m<n のみ(ガイド3.16.3 Remarks)。"""
    with pytest.raises(ScopeError) as excinfo:
        driver.configure_math(1, source1="MATH1")

    assert excinfo.value.code == ErrorCode.INVALID_PARAMETER
    assert scope.command_log == []

    assert driver.configure_math(2, source1="MATH1")["source1"] == "MATH1"


def test_configure_math_rejects_ref_out_of_range(
    driver: ScopeDriver, scope: FakeScope
) -> None:
    with pytest.raises(ScopeError) as excinfo:
        driver.configure_math(1, source1="REF11")

    assert excinfo.value.code == ErrorCode.INVALID_PARAMETER
    assert scope.command_log == []


def test_configure_math_reports_missing_ref_and_digital_support(
    scope: FakeScope,
) -> None:
    """REF/デジタルを持たない機種では「範囲外」でなく「非対応」と伝える。

    `REF1-REF0` / `D0-D-1` のような無意味な範囲を出さないこと(Copilotレビュー指摘)。
    """
    base = load_profile("mho98")
    profile = Profile(
        name=base.name,
        confidence=base.confidence,
        capabilities={**base.capabilities, "ref_channels": 0, "digital_channels": 0},
        dialect=base.dialect,
        limits=base.limits,
    )
    transport = FakeTransport(scope)
    transport.open()
    driver = ScopeDriver(ScpiSession(transport), profile)

    with pytest.raises(ScopeError) as ref_error:
        driver.configure_math(1, source1="REF1")
    assert ref_error.value.code == ErrorCode.INVALID_PARAMETER
    assert "REF0" not in ref_error.value.message
    assert "does not support reference waveforms" in ref_error.value.message

    with pytest.raises(ScopeError) as digital_error:
        driver.configure_math(1, lsource1="D0")
    assert digital_error.value.code == ErrorCode.INVALID_PARAMETER
    assert "D-1" not in digital_error.value.message
    assert "does not support digital channels" in digital_error.value.message

    assert scope.command_log == []


def test_configure_math_rejects_unknown_source_and_lsource(
    driver: ScopeDriver, scope: FakeScope
) -> None:
    for kwargs in ({"source1": "EXT"}, {"lsource1": "D16"}, {"lsource2": "REF1"}):
        with pytest.raises(ScopeError) as excinfo:
            driver.configure_math(1, **kwargs)
        assert excinfo.value.code == ErrorCode.INVALID_PARAMETER
        assert scope.command_log == []


def test_configure_math_rejects_unknown_enum_value(
    driver: ScopeDriver, scope: FakeScope
) -> None:
    with pytest.raises(ScopeError) as excinfo:
        driver.configure_math(1, operator="convolve")

    assert excinfo.value.code == ErrorCode.INVALID_PARAMETER
    assert scope.command_log == []

    with pytest.raises(ScopeError) as excinfo:
        driver.configure_math(1, operator="fft", fft={"window": "kaiser"})

    assert excinfo.value.code == ErrorCode.INVALID_PARAMETER
    assert scope.command_log == []


def test_configure_math_rejects_unknown_subtree_keys(
    driver: ScopeDriver, scope: FakeScope
) -> None:
    with pytest.raises(ScopeError) as excinfo:
        driver.configure_math(1, fft={"windows": "blackman"})

    assert excinfo.value.code == ErrorCode.INVALID_PARAMETER
    assert scope.command_log == []

    with pytest.raises(ScopeError) as excinfo:
        driver.configure_math(1, filter={"w3_hz": 1.0})

    assert excinfo.value.code == ErrorCode.INVALID_PARAMETER
    assert scope.command_log == []


def test_configure_math_without_any_item_sends_nothing(
    driver: ScopeDriver, scope: FakeScope
) -> None:
    with pytest.raises(ScopeError) as excinfo:
        driver.configure_math(1)

    assert excinfo.value.code == ErrorCode.INVALID_PARAMETER
    assert scope.command_log == []


def test_configure_math_unsupported_profile_sends_nothing(
    generic_driver: ScopeDriver, scope: FakeScope
) -> None:
    with pytest.raises(ScopeError) as excinfo:
        generic_driver.configure_math(1, operator="add")

    assert excinfo.value.code == ErrorCode.UNSUPPORTED_FEATURE
    assert scope.command_log == []


def test_configure_math_returns_semantic_readback(
    driver: ScopeDriver, scope: FakeScope
) -> None:
    """機器は短形式(`BLAC`)を返すが、appliedはセマンティック名で返る。"""
    applied = driver.configure_math(1, operator="fft", fft={"window": "blackman"})

    assert scope.math[1]["fft_window"] == "BLAC"
    assert applied["operator"] == "fft"
    assert applied["fft"]["window"] == "blackman"


# --------------------------------------------------------------------------
# MATH演算: get_math_config(条件付き読み取り)
# --------------------------------------------------------------------------


def test_get_math_config_reads_display_first(
    driver: ScopeDriver, scope: FakeScope
) -> None:
    driver.get_math_config(1)

    assert scope.command_log[0] == ":MATH1:DISPlay?"


def test_get_math_config_arithmetic_skips_fft_and_filter_subtrees(
    driver: ScopeDriver, scope: FakeScope
) -> None:
    scope.math[1]["operator"] = "ADD"
    config = driver.get_math_config(1)

    assert config["operator"] == "add"
    assert config["source1"] == "CH1"
    assert config["scale"] == 1.0
    assert config["offset_v"] == 0.0
    assert config["invert"] is False
    assert "lsource1" not in config
    assert "fft" not in config
    assert "filter" not in config
    assert sent(scope, ":MATH1:FFT") == []
    assert sent(scope, ":MATH1:FILT") == []


def test_get_math_config_logic_reads_lsources_but_not_scale(
    driver: ScopeDriver, scope: FakeScope
) -> None:
    scope.math[1]["operator"] = "AND"
    config = driver.get_math_config(1)

    assert config["lsource1"] == "CH1"
    assert config["lsource2"] == "CH1"
    assert "scale" not in config
    assert "offset_v" not in config
    assert sent(scope, ":MATH1:SCAL") == []


def test_get_math_config_fft_reads_the_subtree_without_peaks(
    driver: ScopeDriver, scope: FakeScope
) -> None:
    """探索が無効ならピーク表は読まない(未検証サブツリーを突かない)。"""
    scope.math[1]["operator"] = "FFT"
    config = driver.get_math_config(1)

    assert config["fft"]["source"] == "CH1"
    assert config["fft"]["window"] == "hanning"
    assert config["fft"]["unit"] == "db"
    assert config["fft"]["search_enabled"] is False
    assert "peaks" not in config
    assert "scale" not in config
    assert sent(scope, "SEARCH:RES?") == []


def test_get_math_config_fft_with_search_returns_peaks(
    driver: ScopeDriver, scope: FakeScope
) -> None:
    scope.math[1]["operator"] = "FFT"
    scope.math[1]["fft_search"] = True
    config = driver.get_math_config(1)

    assert config["fft"]["search_enabled"] is True
    assert config["peaks"][0] == {
        "index": 1,
        "frequency_hz": 2.5e6,
        "amplitude": -24.98,
        "amplitude_unit": "dBV",
    }
    # 実機のピーク表は複数行(1行読みだと先頭行しか取れない)
    assert len(config["peaks"]) == 5
    assert config["peaks"][-1]["index"] == 5
    assert "peak_warnings" not in config


def test_get_math_config_filter_operator_reads_the_filter_subtree(
    driver: ScopeDriver, scope: FakeScope
) -> None:
    scope.math[1]["operator"] = "BPAS"
    config = driver.get_math_config(1)

    assert config["operator"] == "bandpass"
    assert config["filter"] == {
        "type": "lowpass",
        "w1_hz": 1.0e6,
        "w2_hz": 1.0e7,
    }
    assert "fft" not in config


def test_get_math_config_unsupported_profile_sends_nothing(
    generic_driver: ScopeDriver, scope: FakeScope
) -> None:
    with pytest.raises(ScopeError) as excinfo:
        generic_driver.get_math_config(1)

    assert excinfo.value.code == ErrorCode.UNSUPPORTED_FEATURE
    assert scope.command_log == []


# --------------------------------------------------------------------------
# MATH演算: 波形経路(:WAVeform:SOURce MATH<n>)
# --------------------------------------------------------------------------


def test_read_waveform_accepts_a_math_source(
    driver: ScopeDriver, scope: FakeScope
) -> None:
    raw = driver.read_waveform("MATH2")

    assert ":WAVeform:SOURce MATH2" in scope.command_log
    assert len(raw.data) > 0


def test_read_waveform_math_out_of_range_sends_nothing(
    driver: ScopeDriver, scope: FakeScope
) -> None:
    with pytest.raises(ScopeError) as excinfo:
        driver.read_waveform("MATH5")

    assert excinfo.value.code == ErrorCode.INVALID_PARAMETER
    assert scope.command_log == []


def test_read_waveform_math_unsupported_profile_sends_nothing(
    generic_driver: ScopeDriver, scope: FakeScope
) -> None:
    with pytest.raises(ScopeError) as excinfo:
        generic_driver.read_waveform("MATH1")

    assert excinfo.value.code == ErrorCode.UNSUPPORTED_FEATURE
    assert scope.command_log == []


def test_read_waveform_analog_channel_sends_no_math_command(
    driver: ScopeDriver, scope: FakeScope
) -> None:
    driver.read_waveform("CH1")

    assert sent(scope, ":MATH") == []


def test_get_math_operator_returns_the_semantic_name(
    driver: ScopeDriver, scope: FakeScope
) -> None:
    scope.math[3]["operator"] = "FFT"

    assert driver.get_math_operator(3) == "fft"
    assert scope.command_log == [":MATH3:OPERator?"]


def test_get_math_fft_start_hz_reads_one_field(
    driver: ScopeDriver, scope: FakeScope
) -> None:
    """FFTトレースの開始周波数だけを1本の問い合わせで読む。

    プリアンブルの xorigin は時間軸の値が残るため(実機実測)、開始周波数は
    こちらから読む必要がある。
    """
    scope.math[1]["fft_freq_start"] = 250.0

    assert driver.get_math_fft_start_hz(1) == pytest.approx(250.0)
    assert scope.command_log == [":MATH1:FFT:FREQuency:STARt?"]


def test_get_math_fft_start_hz_rejects_an_unknown_channel(driver: ScopeDriver) -> None:
    with pytest.raises(ScopeError) as exc:
        driver.get_math_fft_start_hz(9)

    assert exc.value.code == ErrorCode.INVALID_PARAMETER


# --------------------------------------------------------------------------
# カーソル測定(:CURSor / ガイド3.8)
# --------------------------------------------------------------------------


def writes(scope: FakeScope, head: str) -> list[str]:
    """指定サブシステムへの書き込みのみ(問い合わせ・エラーキュー確認を除く)。"""
    return [
        c for c in scope.command_log if "?" not in c and c.upper().startswith(head)
    ]


def test_configure_cursor_manual_sends_exact_scpi(
    driver: ScopeDriver, scope: FakeScope
) -> None:
    applied = driver.configure_cursor(
        mode="manual", type="amplitude", source="CH2", ax=-1e-4, bx=1e-4
    )

    assert writes(scope, ":CURS") == [
        ":CURSor:MODE MANual",
        ":CURSor:MANual:TYPE AMPLitude",
        ":CURSor:MANual:SOURce CHANnel2",
        ":CURSor:MANual:CAX -0.0001",
        ":CURSor:MANual:CBX 0.0001",
    ]
    assert applied == {
        "mode": "manual",
        "type": "amplitude",
        "source": "CH2",
        "ax": -1e-4,
        "bx": 1e-4,
    }


def test_configure_cursor_track_uses_the_track_subtree(
    driver: ScopeDriver, scope: FakeScope
) -> None:
    applied = driver.configure_cursor(
        mode="track", source1="CH1", source2="MATH2", ax=1e-4, ay=0.5
    )

    assert writes(scope, ":CURS") == [
        ":CURSor:MODE TRACk",
        ":CURSor:TRACk:SOURce1 CHANnel1",
        ":CURSor:TRACk:SOURce2 MATH2",
        ":CURSor:TRACk:CAX 0.0001",
        ":CURSor:TRACk:CAY 0.5",
    ]
    assert applied["source2"] == "MATH2"
    assert scope.cursor["manual"]["cax"] == -2.0e-4  # MANual側は触らない


def test_configure_cursor_uses_the_current_mode_when_not_given(
    driver: ScopeDriver, scope: FakeScope
) -> None:
    """モード未指定なら現在の `:CURSor:MODE?` でサブツリーを決める。"""
    scope.cursor["mode"] = "TRAC"

    driver.configure_cursor(ax=1e-4)

    assert scope.command_log[0] == ":CURSor:MODE?"
    assert writes(scope, ":CURS") == [":CURSor:TRACk:CAX 0.0001"]


def test_configure_cursor_accepts_xy_and_none(
    driver: ScopeDriver, scope: FakeScope
) -> None:
    """`xy` はガイドが列挙する正規の値。位置サブツリーはM2では扱わない。"""
    assert driver.configure_cursor(mode="xy") == {"mode": "xy"}
    assert driver.configure_cursor(mode="manual", source="NONE")["source"] == "NONE"


def test_configure_cursor_rejects_positions_without_an_active_subtree(
    driver: ScopeDriver, scope: FakeScope
) -> None:
    """OFF/XY では書き込み先のサブツリーが定まらない。書き込みゼロで拒否する。"""
    with pytest.raises(ScopeError) as exc:
        driver.configure_cursor(mode="off", ax=1e-4)

    assert exc.value.code == ErrorCode.INVALID_PARAMETER
    assert scope.command_log == []


def test_configure_cursor_rejects_keys_of_the_other_subtree(
    driver: ScopeDriver, scope: FakeScope
) -> None:
    with pytest.raises(ScopeError) as exc:
        driver.configure_cursor(mode="manual", source1="CH1")

    assert exc.value.code == ErrorCode.INVALID_PARAMETER
    assert scope.command_log == []

    with pytest.raises(ScopeError) as exc:
        driver.configure_cursor(mode="track", type="time")

    assert exc.value.code == ErrorCode.INVALID_PARAMETER
    assert scope.command_log == []


def test_configure_cursor_rejects_bad_sources(
    driver: ScopeDriver, scope: FakeScope
) -> None:
    """カーソルのソースは CH / MATH / NONE のみ(REF・デジタルは取らない)。"""
    for source in ("REF1", "D0", "CH9", "MATH9"):
        with pytest.raises(ScopeError) as exc:
            driver.configure_cursor(mode="manual", source=source)

        assert exc.value.code == ErrorCode.INVALID_PARAMETER
        assert scope.command_log == []


def test_configure_cursor_without_any_item_sends_nothing(
    driver: ScopeDriver, scope: FakeScope
) -> None:
    with pytest.raises(ScopeError) as exc:
        driver.configure_cursor()

    assert exc.value.code == ErrorCode.INVALID_PARAMETER
    assert scope.command_log == []


def test_configure_cursor_unsupported_profile_sends_nothing(
    generic_driver: ScopeDriver, scope: FakeScope
) -> None:
    with pytest.raises(ScopeError) as exc:
        generic_driver.configure_cursor(mode="manual")

    assert exc.value.code == ErrorCode.UNSUPPORTED_FEATURE
    assert scope.command_log == []


def test_get_cursor_config_reads_only_the_active_subtree(
    driver: ScopeDriver, scope: FakeScope
) -> None:
    scope.cursor["mode"] = "MAN"

    config = driver.get_cursor_config()

    assert config["mode"] == "manual"
    assert config["type"] == "time"
    assert config["source"] == "CH1"
    assert config["ax"] == pytest.approx(-2.0e-4)
    assert "source1" not in config
    assert sent(scope, ":CURSOR:TRAC") == []


def test_get_cursor_config_off_reads_nothing_else(
    driver: ScopeDriver, scope: FakeScope
) -> None:
    config = driver.get_cursor_config()

    assert config == {"mode": "off"}
    assert scope.command_log == [":CURSor:MODE?"]


def test_get_cursor_measurement_returns_the_deltas(
    driver: ScopeDriver, scope: FakeScope
) -> None:
    scope.cursor["mode"] = "MAN"
    scope.cursor["manual"].update({"cax": 1e-4, "cbx": 3e-4, "cay": -1.0, "cby": 2.0})

    result = driver.get_cursor_measurement()

    assert result == {
        "mode": "manual",
        "ax_s": pytest.approx(1e-4),
        "ay_v": pytest.approx(-1.0),
        "bx_s": pytest.approx(3e-4),
        "by_v": pytest.approx(2.0),
        "xdelta_s": pytest.approx(2e-4),
        "ydelta_v": pytest.approx(3.0),
        "ixdelta_hz": pytest.approx(5e3),
    }


def test_get_cursor_measurement_reads_the_track_subtree(
    driver: ScopeDriver, scope: FakeScope
) -> None:
    scope.cursor["mode"] = "TRAC"

    driver.get_cursor_measurement()

    assert sent(scope, ":CURSOR:MAN") == []
    assert ":CURSor:TRACk:XDELta?" in scope.command_log


def test_get_cursor_measurement_off_returns_the_mode_only(
    driver: ScopeDriver, scope: FakeScope
) -> None:
    """カーソルOFFでは読める値が無い。キーの不在でそれを表す(問い合わせ1本)。"""
    result = driver.get_cursor_measurement()

    assert result == {"mode": "off"}
    assert scope.command_log == [":CURSor:MODE?"]


def test_get_cursor_measurement_reports_an_invalid_reading_as_none(
    driver: ScopeDriver, scope: FakeScope
) -> None:
    """ΔX=0 では 1/ΔX が番兵値(±9.9E37)。測定不能は None で返す。"""
    scope.cursor["mode"] = "MAN"
    scope.cursor["manual"].update({"cax": 1e-4, "cbx": 1e-4})

    result = driver.get_cursor_measurement()

    assert result["xdelta_s"] == 0.0
    assert result["ixdelta_hz"] is None


def test_get_cursor_measurement_unsupported_profile_sends_nothing(
    generic_driver: ScopeDriver, scope: FakeScope
) -> None:
    with pytest.raises(ScopeError) as exc:
        generic_driver.get_cursor_measurement()

    assert exc.value.code == ErrorCode.UNSUPPORTED_FEATURE
    assert scope.command_log == []


# --------------------------------------------------------------------------
# 周波数カウンタ・電圧計(:COUNter / :DVM / ガイド3.7・3.10)
# --------------------------------------------------------------------------


def test_configure_meter_counter_sends_exact_scpi(
    driver: ScopeDriver, scope: FakeScope
) -> None:
    applied = driver.configure_meter(
        "counter",
        enabled=True,
        source="D3",
        mode="period",
        digits=6,
        totalize_enabled=True,
    )

    assert writes(scope, ":COUN") == [
        ":COUNter:ENABle ON",
        ":COUNter:SOURce D3",
        ":COUNter:MODE PERiod",
        ":COUNter:NDIGits 6",
        ":COUNter:TOTalize:ENABle ON",
    ]
    assert applied == {
        "kind": "counter",
        "enabled": True,
        "source": "D3",
        "mode": "period",
        "digits": 6,
        "totalize_enabled": True,
    }


def test_configure_meter_dvm_sends_exact_scpi(
    driver: ScopeDriver, scope: FakeScope
) -> None:
    applied = driver.configure_meter("dvm", enabled=True, source="CH3", mode="dc_rms")

    assert writes(scope, ":DVM") == [
        ":DVM:ENABle ON",
        ":DVM:SOURce CHANnel3",
        ":DVM:MODE DCRMs",
    ]
    assert applied == {
        "kind": "dvm",
        "enabled": True,
        "source": "CH3",
        "mode": "dc_rms",
    }


def test_configure_meter_dvm_rejects_digital_sources(
    driver: ScopeDriver, scope: FakeScope
) -> None:
    """`:DVM:SOURce` はアナログchのみ(ガイド3.10.3)。送信前に拒否する。"""
    with pytest.raises(ScopeError) as exc:
        driver.configure_meter("dvm", source="D0")

    assert exc.value.code == ErrorCode.INVALID_PARAMETER
    assert scope.command_log == []


def test_configure_meter_dvm_rejects_counter_only_settings(
    driver: ScopeDriver, scope: FakeScope
) -> None:
    with pytest.raises(ScopeError) as exc:
        driver.configure_meter("dvm", digits=4)

    assert exc.value.code == ErrorCode.INVALID_PARAMETER
    assert scope.command_log == []


def test_configure_meter_rejects_an_unknown_kind(
    driver: ScopeDriver, scope: FakeScope
) -> None:
    with pytest.raises(ScopeError) as exc:
        driver.configure_meter("voltmeter", enabled=True)

    assert exc.value.code == ErrorCode.INVALID_PARAMETER
    assert scope.command_log == []


def test_configure_meter_rejects_digits_out_of_range(
    driver: ScopeDriver, scope: FakeScope
) -> None:
    """分解能は3〜6(ガイド3.7.5)。範囲外は送信ゼロで拒否する。"""
    for digits in (2, 7):
        with pytest.raises(ScopeError) as exc:
            driver.configure_meter("counter", digits=digits)

        assert exc.value.code == ErrorCode.INVALID_PARAMETER
        assert scope.command_log == []


def test_configure_meter_counter_rejects_a_digital_channel_out_of_range(
    driver: ScopeDriver, scope: FakeScope
) -> None:
    with pytest.raises(ScopeError) as exc:
        driver.configure_meter("counter", source="D16")

    assert exc.value.code == ErrorCode.INVALID_PARAMETER
    assert scope.command_log == []


def test_configure_meter_without_any_item_sends_nothing(
    driver: ScopeDriver, scope: FakeScope
) -> None:
    with pytest.raises(ScopeError) as exc:
        driver.configure_meter("counter")

    assert exc.value.code == ErrorCode.INVALID_PARAMETER
    assert scope.command_log == []


def test_configure_meter_unsupported_profile_sends_nothing(
    generic_driver: ScopeDriver, scope: FakeScope
) -> None:
    for kind in ("counter", "dvm"):
        with pytest.raises(ScopeError) as exc:
            generic_driver.configure_meter(kind, enabled=True)

        assert exc.value.code == ErrorCode.UNSUPPORTED_FEATURE
        assert scope.command_log == []


def test_get_meter_config_returns_semantic_values(
    driver: ScopeDriver, scope: FakeScope
) -> None:
    scope.counter.update({"mode": "TOT", "source": "D5"})

    assert driver.get_meter_config("counter") == {
        "kind": "counter",
        "enabled": False,
        "source": "D5",
        "mode": "totalize",
        "digits": 4,
        "totalize_enabled": False,
    }
    assert driver.get_meter_config("dvm") == {
        "kind": "dvm",
        "enabled": False,
        "source": "CH1",
        "mode": "ac_rms",
    }
    assert sent(scope, ":DVM:NDIG") == []


def test_get_meter_value_reads_the_current_command(
    driver: ScopeDriver, scope: FakeScope
) -> None:
    """現在値は `:COUNter:CURRent?` / `:DVM:CURRent?`(`:VALue` は存在しない)。

    無効な計は現在値を問い合わせないので、有効化してから読む(実機と同じ前提)。
    """
    scope.counter["enable"] = True
    scope.dvm["enable"] = True

    assert driver.get_meter_value("counter") == pytest.approx(1.0e3)
    assert scope.command_log == [":COUNter:ENABle?", ":COUNter:CURRent?"]

    assert driver.get_meter_value("dvm") == pytest.approx(0.35)
    assert scope.command_log[-1] == ":DVM:CURRent?"


def test_get_meter_value_reports_an_invalid_reading_as_none(
    driver: ScopeDriver, scope: FakeScope
) -> None:
    scope.counter["enable"] = True
    scope.counter["mode"] = "TOT"
    scope.counter["total"] = 9.9e37

    assert driver.get_meter_value("counter") is None


def test_get_meter_value_while_disabled_does_not_query_the_current(
    driver: ScopeDriver, scope: FakeScope
) -> None:
    """M2実機実測: 無効な電圧計の `:DVM:CURRent?` は**空応答**でパースできない。

    無効な計を読むのは普通の操作なので機器故障(SCPI_ERROR)に見せてはならない。
    `:ENABle?` を先に読み、無効なら現在値クエリ自体を送らない。
    """
    assert driver.get_meter_value("dvm") is None
    assert scope.command_log == [":DVM:ENABle?"]

    scope.command_log.clear()
    assert driver.get_meter_value("counter") is None
    assert scope.command_log == [":COUNter:ENABle?"]


def test_get_meter_value_tolerates_a_blank_current_response(
    driver: ScopeDriver, scope: FakeScope, monkeypatch: pytest.MonkeyPatch
) -> None:
    """有効化直後に空応答が返る取り合わせでも、パースエラーにはしない。"""
    scope.dvm["enable"] = True
    real_query = driver.session.query
    monkeypatch.setattr(
        driver.session,
        "query",
        lambda command: "" if command.endswith(":CURRent?") else real_query(command),
    )

    assert driver.get_meter_value("dvm") is None


def test_clear_counter_totalize_sends_the_action_command(
    driver: ScopeDriver, scope: FakeScope
) -> None:
    driver.clear_counter_totalize()

    assert writes(scope, ":COUN") == [":COUNter:TOTalize:CLEar"]
    assert scope.counter["total"] == 0.0


def test_clear_counter_totalize_unsupported_profile_sends_nothing(
    generic_driver: ScopeDriver, scope: FakeScope
) -> None:
    with pytest.raises(ScopeError) as exc:
        generic_driver.clear_counter_totalize()

    assert exc.value.code == ErrorCode.UNSUPPORTED_FEATURE
    assert scope.command_log == []


# --------------------------------------------------------------------------
# ヒストグラム(:HISTogram / ガイド3.11)
# --------------------------------------------------------------------------


def test_configure_histogram_sends_exact_scpi(
    driver: ScopeDriver, scope: FakeScope
) -> None:
    applied = driver.configure_histogram(
        enabled=True,
        type="vertical",
        source="CH2",
        height=4,
        left_s=-1e-3,
        right_s=1e-3,
        bottom_v=-2.0,
        top_v=2.0,
    )

    assert writes(scope, ":HIST") == [
        ":HISTogram:ENABle ON",
        ":HISTogram:TYPE VERTical",
        ":HISTogram:SOURce CHANnel2",
        ":HISTogram:HEIGht 4",
        ":HISTogram:RANGe:LEFT -0.001",
        ":HISTogram:RANGe:RIGHt 0.001",
        ":HISTogram:RANGe:BOTTom -2.0",
        ":HISTogram:RANGe:TOP 2.0",
    ]
    assert applied["type"] == "vertical"
    assert applied["height"] == 4
    assert applied["left_s"] == pytest.approx(-1e-3)


def test_configure_histogram_rejects_reversed_ranges(
    driver: ScopeDriver, scope: FakeScope
) -> None:
    """LEFT < RIGHt / BOTTom < TOP はガイド明記の制約(両端指定時のみ検証)。"""
    with pytest.raises(ScopeError) as exc:
        driver.configure_histogram(left_s=1e-3, right_s=-1e-3)

    assert exc.value.code == ErrorCode.INVALID_PARAMETER
    assert scope.command_log == []

    with pytest.raises(ScopeError) as exc:
        driver.configure_histogram(bottom_v=2.0, top_v=-2.0)

    assert exc.value.code == ErrorCode.INVALID_PARAMETER
    assert scope.command_log == []


def test_configure_histogram_allows_a_single_bound(
    driver: ScopeDriver, scope: FakeScope
) -> None:
    """片側だけの指定は現在値との突合が要るため機器に委ねる(M1の結合方針)。"""
    driver.configure_histogram(left_s=1e-3)

    assert writes(scope, ":HIST") == [":HISTogram:RANGe:LEFT 0.001"]


def test_configure_histogram_rejects_bad_height_and_source(
    driver: ScopeDriver, scope: FakeScope
) -> None:
    for kwargs in ({"height": 5}, {"height": 0}, {"source": "D0"}, {"source": "MATH1"}):
        with pytest.raises(ScopeError) as exc:
            driver.configure_histogram(**kwargs)

        assert exc.value.code == ErrorCode.INVALID_PARAMETER
        assert scope.command_log == []


def test_configure_histogram_without_any_item_sends_nothing(
    driver: ScopeDriver, scope: FakeScope
) -> None:
    with pytest.raises(ScopeError) as exc:
        driver.configure_histogram()

    assert exc.value.code == ErrorCode.INVALID_PARAMETER
    assert scope.command_log == []


def test_get_histogram_config_returns_semantic_values(
    driver: ScopeDriver, scope: FakeScope
) -> None:
    config = driver.get_histogram_config()

    assert config["enabled"] is False
    assert config["type"] == "horizontal"
    assert config["source"] == "CH1"
    assert config["height"] == 2
    assert config["left_s"] == pytest.approx(-2.0e-4)
    assert config["top_v"] == pytest.approx(1.0)


def test_get_histogram_result_keeps_the_raw_response(
    driver: ScopeDriver, scope: FakeScope
) -> None:
    """機器がラベル付きで返す統計値を正規化キーで返す。生文字列も必ず残す。"""
    scope.histogram["enable"] = True

    result = driver.get_histogram_result()

    assert result["raw"].startswith("[Sum:374hits,")
    assert result["stats"]["sum"] == pytest.approx(374.0)
    assert result["stats"]["sum_unit"] == "hits"
    assert result["stats"]["min"] == pytest.approx(-0.9999)
    assert result["stats"]["min_unit"] == "V"
    assert result["stats"]["mean_plus_sigma"] == pytest.approx(0.581421)
    assert "warnings" not in result


def test_get_histogram_result_reads_one_line_not_a_multi_line_table(
    driver: ScopeDriver, scope: FakeScope, monkeypatch: pytest.MonkeyPatch
) -> None:
    """M2実機実測: 統計応答は終端の空行を持たない1行。

    `query_lines` は空行が来るまで読むため、実機ではソケットのタイムアウトまで
    固まってしまう(FFTピーク表とは書式が違う)。必ず `query` で読むこと。
    """
    scope.histogram["enable"] = True

    def _forbidden(command: str) -> list[str]:
        raise AssertionError(f"query_lines must not be used here: {command}")

    monkeypatch.setattr(driver.session, "query_lines", _forbidden)

    assert driver.get_histogram_result()["stats"]["max"] == pytest.approx(1.562)


def test_get_histogram_result_warns_on_an_unparsable_response(
    driver: ScopeDriver, scope: FakeScope, monkeypatch: pytest.MonkeyPatch
) -> None:
    scope.histogram["enable"] = True
    real_query = driver.session.query
    monkeypatch.setattr(
        driver.session,
        "query",
        lambda command: (
            "something else" if "STATistics" in command else real_query(command)
        ),
    )

    result = driver.get_histogram_result()

    assert result["raw"] == "something else"
    assert "stats" not in result
    assert result["warnings"]


def test_get_histogram_result_while_disabled_skips_the_statistics_query(
    driver: ScopeDriver, scope: FakeScope
) -> None:
    """M2実機実測: 無効時の統計クエリは `[]` を返す上に**エラーキューを汚す**。

    無効なら問い合わせ自体を送らず、なぜ空なのかを warnings で伝える。
    """
    result = driver.get_histogram_result()

    assert scope.command_log == [":HISTogram:ENABle?"]
    assert result["raw"] == ""
    assert "stats" not in result
    assert "disabled" in result["warnings"][0]


def test_get_histogram_result_while_disabled_leaves_the_error_queue_clean(
    driver: ScopeDriver, scope: FakeScope
) -> None:
    """回帰: 汚れたエラーキューは**次の無関係な書き込み**に化けて出る。

    修正前は統計クエリが積んだ -200 を後続の set_and_verify が拾い、
    まったく別のコマンドが SCPI_ERROR で落ちていた。
    """
    driver.get_histogram_result()

    applied = driver.configure_histogram(height=3)

    assert applied == {"height": 3}
    assert not scope.error_queue


def test_reset_histogram_sends_the_action_command(
    driver: ScopeDriver, scope: FakeScope
) -> None:
    driver.reset_histogram()

    assert writes(scope, ":HIST") == [":HISTogram:RESet"]
    assert scope.histogram["hits"] == 0


def test_histogram_unsupported_profile_sends_nothing(
    generic_driver: ScopeDriver, scope: FakeScope
) -> None:
    for call in (
        lambda: generic_driver.configure_histogram(enabled=True),
        generic_driver.get_histogram_config,
        generic_driver.get_histogram_result,
        generic_driver.reset_histogram,
    ):
        with pytest.raises(ScopeError) as exc:
            call()

        assert exc.value.code == ErrorCode.UNSUPPORTED_FEATURE
        assert scope.command_log == []


# --------------------------------------------------------------------------
# リファレンス波形(:REFerence / ガイド3.20)
# --------------------------------------------------------------------------


def test_configure_reference_sends_the_slot_as_a_command_argument(
    driver: ScopeDriver, scope: FakeScope
) -> None:
    """枠番号はニモニックではなく**引数**(`<ref>,<値>` / 問い合わせは `? <ref>`)。"""
    applied = driver.configure_reference(
        2, source="CH3", scale=0.5, offset_v=-1.5, color="green", label="probe_a"
    )

    assert writes(scope, ":REF") == [
        ":REFerence:SOURce 2,CHANnel3",
        ":REFerence:VSCale 2,0.5",
        ":REFerence:VOFFset 2,-1.5",
        ":REFerence:COLor 2,GREen",
        ":REFerence:LABel:CONTent 2,probe_a",
    ]
    assert [c for c in scope.command_log if c.startswith(":REFerence") and "?" in c] == [
        ":REFerence:SOURce? 2",
        ":REFerence:VSCale? 2",
        ":REFerence:VOFFset? 2",
        ":REFerence:COLor? 2",
        ":REFerence:LABel:CONTent? 2",
    ]
    assert applied == {
        "ref": 2,
        "source": "CH3",
        "scale": 0.5,
        "offset_v": -1.5,
        "color": "green",
        "label": "probe_a",
    }


def test_configure_reference_label_display_takes_no_slot(
    driver: ScopeDriver, scope: FakeScope
) -> None:
    """ラベル表示は全枠共通のスイッチ(ガイド3.20.6)。枠引数を付けてはならない。"""
    applied = driver.configure_reference(3, label_display=True)

    assert writes(scope, ":REF") == [":REFerence:LABel:ENABle ON"]
    assert applied == {"ref": 3, "label_display": True}


def test_configure_reference_accepts_digital_and_math_sources(
    driver: ScopeDriver, scope: FakeScope
) -> None:
    assert driver.configure_reference(1, source="D7")["source"] == "D7"
    assert driver.configure_reference(1, source="MATH2")["source"] == "MATH2"
    assert writes(scope, ":REF") == [
        ":REFerence:SOURce 1,D7",
        ":REFerence:SOURce 1,MATH2",
    ]


def test_configure_reference_rejects_bad_sources(
    driver: ScopeDriver, scope: FakeScope
) -> None:
    """値域は CH / MATH / D0-D15 のみ(REF自身も NONE も取らない)。"""
    for source in ("REF1", "NONE", "CH9", "MATH9", "D16", 1):
        with pytest.raises(ScopeError) as exc:
            driver.configure_reference(1, source=source)

        assert exc.value.code == ErrorCode.INVALID_PARAMETER, source
        assert scope.command_log == [], source


def test_configure_reference_rejects_a_slot_out_of_range(
    driver: ScopeDriver, scope: FakeScope
) -> None:
    for ref in (0, 11, "1", True):
        with pytest.raises(ScopeError) as exc:
            driver.configure_reference(ref, scale=1.0)

        assert exc.value.code == ErrorCode.INVALID_PARAMETER, ref
        assert scope.command_log == [], ref


def test_configure_reference_rejects_an_unknown_color(
    driver: ScopeDriver, scope: FakeScope
) -> None:
    with pytest.raises(ScopeError) as exc:
        driver.configure_reference(1, color="purple")

    assert exc.value.code == ErrorCode.INVALID_PARAMETER
    assert scope.command_log == []


def test_configure_reference_rejects_an_unsafe_label(
    driver: ScopeDriver, scope: FakeScope
) -> None:
    """ラベルは引用符無しで埋め込むため、`;`(SCPIの区切り)や空白は拒否する。"""
    for label in ("a;b", "a b", "", 'a"b', 1):
        with pytest.raises(ScopeError) as exc:
            driver.configure_reference(1, label=label)

        assert exc.value.code == ErrorCode.INVALID_PARAMETER, label
        assert scope.command_log == [], label


def test_configure_reference_without_any_item_sends_nothing(
    driver: ScopeDriver, scope: FakeScope
) -> None:
    with pytest.raises(ScopeError) as exc:
        driver.configure_reference(1)

    assert exc.value.code == ErrorCode.INVALID_PARAMETER
    assert scope.command_log == []


def test_configure_reference_unsupported_profile_sends_nothing(
    generic_driver: ScopeDriver, scope: FakeScope
) -> None:
    with pytest.raises(ScopeError) as exc:
        generic_driver.configure_reference(1, scale=1.0)

    assert exc.value.code == ErrorCode.UNSUPPORTED_FEATURE
    assert scope.command_log == []


def test_get_reference_config_returns_semantic_values(
    driver: ScopeDriver, scope: FakeScope
) -> None:
    scope.reference[5]["source"] = "MATH3"
    scope.reference[5]["vscale"] = 0.2
    scope.reference[5]["color"] = "ORAN"
    scope.reference_global["label_display"] = True

    config = driver.get_reference_config(5)

    assert config == {
        "ref": 5,
        "source": "MATH3",
        "scale": 0.2,
        "offset_v": 0.0,
        "color": "orange",
        "label": "REF5",
        "label_display": True,
    }


def test_get_reference_config_accepts_the_device_color_abbreviation(
    driver: ScopeDriver, scope: FakeScope
) -> None:
    """実測: 実機は緑を `GREE` で返す(ガイド3.20.7 のReturn Formatは `GRE`)。

    工場出荷状態の枠4・枠9が緑なので、これを取りこぼすと未操作の実機で
    `get_reference_state` が丸ごと落ちる。
    """
    scope.reference[4]["color"] = "GREE"

    assert driver.get_reference_config(4)["color"] == "green"


def test_get_reference_config_rejects_an_unknown_slot(
    driver: ScopeDriver, scope: FakeScope
) -> None:
    with pytest.raises(ScopeError) as exc:
        driver.get_reference_config(11)

    assert exc.value.code == ErrorCode.INVALID_PARAMETER
    assert scope.command_log == []


def test_save_reference_sends_the_action_command(
    driver: ScopeDriver, scope: FakeScope
) -> None:
    """`:REFerence:SAVE <ref>` は引数1つ・read-back不能の書き込み専用命令。"""
    driver.save_reference(3)

    assert writes(scope, ":REF") == [":REFerence:SAVE 3"]
    assert scope.reference[3]["saved"] is True


def test_reset_reference_sends_the_action_command(
    driver: ScopeDriver, scope: FakeScope
) -> None:
    scope.reference[4]["vscale"] = 0.25

    driver.reset_reference(4)

    assert writes(scope, ":REF") == [":REFerence:RESet 4"]
    assert scope.reference[4]["vscale"] == 0.05


def test_reference_actions_reject_an_unknown_slot(
    driver: ScopeDriver, scope: FakeScope
) -> None:
    for action in (driver.save_reference, driver.reset_reference):
        with pytest.raises(ScopeError) as exc:
            action(11)

        assert exc.value.code == ErrorCode.INVALID_PARAMETER
        assert scope.command_log == []


def test_reference_actions_unsupported_profile_send_nothing(
    generic_driver: ScopeDriver, scope: FakeScope
) -> None:
    for action in (generic_driver.save_reference, generic_driver.reset_reference):
        with pytest.raises(ScopeError) as exc:
            action(1)

        assert exc.value.code == ErrorCode.UNSUPPORTED_FEATURE
        assert scope.command_log == []


# --------------------------------------------------------------------------
# 測定項目の全面拡張(M4)
# --------------------------------------------------------------------------


def test_measurement_keys_cover_all_41_guide_items() -> None:
    """ガイド3.17.2 の <item> は41トークン。全てに意味的名を与える。"""
    from rigol_oscilloscope_mcp.driver.scope import MEASUREMENT_KEYS

    assert len(MEASUREMENT_KEYS) == 41
    # SI単位付きキーは重複しない(返却dictのキーになるため)
    assert len(set(MEASUREMENT_KEYS.values())) == 41


def test_mho98_declares_every_measurement_item(driver: ScopeDriver) -> None:
    from rigol_oscilloscope_mcp.driver.scope import MEASUREMENT_KEYS

    items = driver.profile.dialect["measurement_items"]
    assert sorted(items) == sorted(MEASUREMENT_KEYS)


@pytest.mark.parametrize(
    ("name", "mnemonic", "key"),
    [
        ("vtop", "VTOP", "vtop_v"),
        ("overshoot", "OVERshoot", "overshoot_ratio"),
        ("area", "MARea", "area_vs"),
        ("pulse_width_pos", "PWIDth", "pulse_width_pos_s"),
        ("slew_rate_pos", "PSLewrate", "slew_rate_pos_v_per_s"),
        ("pulses_pos", "PPULses", "pulses_pos_count"),
        ("ac_rms", "ACRMs", "ac_rms_v"),
    ],
)
def test_measure_new_items_send_guide_mnemonics(
    driver: ScopeDriver, scope: FakeScope, name: str, mnemonic: str, key: str
) -> None:
    results = driver.measure("CH1", [name])

    assert results[0].key == key
    assert sent(scope, ":MEAS") == [f":MEASure:ITEM? {mnemonic},CHANnel1"]


def test_measure_dual_source_item_sends_both_sources(
    driver: ScopeDriver, scope: FakeScope
) -> None:
    """遅延・位相はソース2つ(ガイド3.17.2 の <src>[,<src>])。"""
    driver.measure("CH1", ["delay_rise_rise"], channel_b="CH2")

    assert sent(scope, ":MEAS") == [":MEASure:ITEM? RRDelay,CHANnel1,CHANnel2"]


def test_measure_dual_source_item_requires_channel_b(
    driver: ScopeDriver, scope: FakeScope
) -> None:
    """第2ソース省略時は機器の「最後に選んだソース」に依存するため拒否する。"""
    with pytest.raises(ScopeError) as excinfo:
        driver.measure("CH1", ["phase_rise_rise"])

    assert excinfo.value.code == ErrorCode.INVALID_PARAMETER
    assert scope.command_log == []


def test_measure_rejects_channel_b_without_dual_source_item(
    driver: ScopeDriver, scope: FakeScope
) -> None:
    with pytest.raises(ScopeError) as excinfo:
        driver.measure("CH1", ["vpp"], channel_b="CH2")

    assert excinfo.value.code == ErrorCode.INVALID_PARAMETER
    assert scope.command_log == []


def test_measure_single_source_items_ignore_second_source(
    driver: ScopeDriver, scope: FakeScope
) -> None:
    """単一ソース項目には第2ソースを付けない(混在指定でも)。"""
    driver.measure("CH1", ["vpp", "delay_fall_fall"], channel_b="CH3")

    assert sent(scope, ":MEAS") == [
        ":MEASure:ITEM? VPP,CHANnel1",
        ":MEASure:ITEM? FFDelay,CHANnel1,CHANnel3",
    ]
