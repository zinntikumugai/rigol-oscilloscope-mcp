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

    assert ":AUToscale" in scope.command_log
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
