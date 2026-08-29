"""service/measurement.py のテスト。

無効値(機器の番兵値)を正常値としてLLMに解釈させないこと(tools.md 5章)を
warnings で確認する。
"""

import json

import pytest

from rigol_oscilloscope_mcp.driver.scope import ScopeDriver
from rigol_oscilloscope_mcp.driver.session import ScpiSession
from rigol_oscilloscope_mcp.errors import ErrorCode, ScopeError
from rigol_oscilloscope_mcp.profiles import load_profile
from rigol_oscilloscope_mcp.service.measurement import get_meter_value, measure
from rigol_oscilloscope_mcp.testing import FakeScope, FakeTransport
from rigol_oscilloscope_mcp.testing import fake_scope as fake_scope_module


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


# --------------------------------------------------------------------------
# 正常系
# --------------------------------------------------------------------------


def test_measure_returns_channel(driver: ScopeDriver) -> None:
    assert measure(driver, "CH1", ["frequency"])["channel"] == "CH1"


def test_measure_normalizes_channel_name(driver: ScopeDriver) -> None:
    assert measure(driver, "chan1", ["frequency"])["channel"] == "CH1"


def test_measure_values_use_si_suffixed_keys(driver: ScopeDriver) -> None:
    result = measure(driver, "CH1", ["frequency", "vpp", "duty", "rise_time"])

    assert list(result["values"]) == [
        "frequency_hz",
        "vpp_v",
        "duty_ratio",
        "rise_time_s",
    ]


def test_measure_values_match_fake_readings(driver: ScopeDriver) -> None:
    result = measure(driver, "CH1", ["frequency", "vpp"])

    assert result["values"]["frequency_hz"] == pytest.approx(1000.1)
    assert result["values"]["vpp_v"] == pytest.approx(3.268)


def test_measure_quality_is_keyed_by_semantic_name(driver: ScopeDriver) -> None:
    result = measure(driver, "CH1", ["frequency", "vpp"])

    assert result["quality"] == {"frequency": "valid", "vpp": "valid"}


def test_measure_has_no_warnings_when_all_valid(driver: ScopeDriver) -> None:
    assert measure(driver, "CH1", ["frequency", "vpp"])["warnings"] == []


def test_measure_result_is_json_serializable(driver: ScopeDriver) -> None:
    text = json.dumps(measure(driver, "CH1", ["frequency"]))

    assert json.loads(text)["values"]["frequency_hz"] == pytest.approx(1000.1)


def test_measure_dedupes_preserving_order(driver: ScopeDriver, scope: FakeScope) -> None:
    scope.command_log.clear()

    result = measure(driver, "CH1", ["vpp", "frequency", "vpp", "frequency"])

    assert list(result["values"]) == ["vpp_v", "frequency_hz"]
    assert len(scope.command_log) == 2


# --------------------------------------------------------------------------
# 異常系
# --------------------------------------------------------------------------


def test_measure_rejects_empty_list(driver: ScopeDriver) -> None:
    with pytest.raises(ScopeError) as excinfo:
        measure(driver, "CH1", [])

    assert excinfo.value.code == ErrorCode.INVALID_PARAMETER


def test_measure_rejects_empty_list_without_sending(
    driver: ScopeDriver, scope: FakeScope
) -> None:
    scope.command_log.clear()

    with pytest.raises(ScopeError):
        measure(driver, "CH1", [])

    assert scope.command_log == []


def test_measure_rejects_unknown_channel(driver: ScopeDriver) -> None:
    with pytest.raises(ScopeError) as excinfo:
        measure(driver, "CH9", ["frequency"])

    assert excinfo.value.code == ErrorCode.INVALID_PARAMETER


def test_measure_propagates_unsupported_feature(generic_driver: ScopeDriver) -> None:
    with pytest.raises(ScopeError) as excinfo:
        measure(generic_driver, "CH1", ["rise_time"])

    assert excinfo.value.code == ErrorCode.UNSUPPORTED_FEATURE


# --------------------------------------------------------------------------
# 無効値(番兵値)
# --------------------------------------------------------------------------


def test_measure_invalid_value_is_none_with_warning(
    driver: ScopeDriver, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setitem(fake_scope_module._MEASURE_VALUES, "VPP", "9.9E+37")

    result = measure(driver, "CH1", ["frequency", "vpp"])

    assert result["values"]["vpp_v"] is None
    assert result["quality"]["vpp"] == "unknown"
    assert result["warnings"] == [
        "vpp measurement is invalid (possibly no signal or not yet settled)"
    ]


def test_measure_valid_items_are_unaffected_by_invalid_one(
    driver: ScopeDriver, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setitem(fake_scope_module._MEASURE_VALUES, "VPP", "9.9E+37")

    result = measure(driver, "CH1", ["frequency", "vpp"])

    assert result["values"]["frequency_hz"] == pytest.approx(1000.1)
    assert result["quality"]["frequency"] == "valid"


def test_measure_warns_once_per_invalid_item(
    driver: ScopeDriver, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setitem(fake_scope_module._MEASURE_VALUES, "VPP", "9.9E+37")
    monkeypatch.setitem(fake_scope_module._MEASURE_VALUES, "VMIN", "-9.9E+37")

    warnings = measure(driver, "CH1", ["vpp", "vmin"])["warnings"]

    assert len(warnings) == 2
    assert warnings[0].startswith("vpp ")
    assert warnings[1].startswith("vmin ")


# --------------------------------------------------------------------------
# get_meter_value(周波数カウンタ・電圧計 / Phase M2)
# --------------------------------------------------------------------------


def test_get_meter_value_composes_the_unit_from_the_mode(
    driver: ScopeDriver, scope: FakeScope
) -> None:
    """値だけでは意味が定まらない(カウンタの単位はモード依存)。"""
    scope.counter["enable"] = True
    assert get_meter_value(driver, "counter") == {
        "kind": "counter",
        "enabled": True,
        "source": "CH1",
        "mode": "frequency",
        "digits": 4,
        "totalize_enabled": False,
        "value": pytest.approx(1.0e3),
        "unit": "Hz",
    }


def test_get_meter_value_unit_follows_the_counter_mode(
    driver: ScopeDriver, scope: FakeScope
) -> None:
    scope.counter["enable"] = True
    scope.counter["mode"] = "PER"
    assert get_meter_value(driver, "counter")["unit"] == "s"

    scope.counter["mode"] = "TOT"
    result = get_meter_value(driver, "counter")
    assert result["unit"] == "counts"
    assert result["value"] == pytest.approx(1234.0)


def test_get_meter_value_dvm_is_always_volts(
    driver: ScopeDriver, scope: FakeScope
) -> None:
    scope.dvm["enable"] = True
    for mode, expected in (("ACRM", 0.35), ("DC", 1.0), ("DCRM", 1.06)):
        scope.dvm["mode"] = mode
        result = get_meter_value(driver, "dvm")
        assert result["unit"] == "V"
        assert result["value"] == pytest.approx(expected)


def test_get_meter_value_reports_an_invalid_reading_as_none(
    driver: ScopeDriver, scope: FakeScope
) -> None:
    """測定不能の番兵値(±9.9E37)を正常値として解釈させない。"""
    scope.counter["enable"] = True
    scope.counter["mode"] = "TOT"
    scope.counter["total"] = 9.9e37

    assert get_meter_value(driver, "counter")["value"] is None


def test_get_meter_value_while_disabled_says_why_the_value_is_missing(
    driver: ScopeDriver, scope: FakeScope
) -> None:
    """無効な計の値は `None`。理由(`enabled: false`)は同じ返却が持つ。"""
    result = get_meter_value(driver, "dvm")

    assert result["enabled"] is False
    assert result["value"] is None
    assert result["unit"] == "V"  # 単位はモードから決まる(値の有無と無関係)
    assert ":DVM:CURRent?" not in scope.command_log


def test_get_meter_value_rejects_an_unknown_kind(
    driver: ScopeDriver, scope: FakeScope
) -> None:
    with pytest.raises(ScopeError) as excinfo:
        get_meter_value(driver, "voltmeter")

    assert excinfo.value.code == ErrorCode.INVALID_PARAMETER
    assert scope.command_log == []


def test_get_meter_value_unsupported_profile_sends_nothing(
    generic_driver: ScopeDriver, scope: FakeScope
) -> None:
    with pytest.raises(ScopeError) as excinfo:
        get_meter_value(generic_driver, "dvm")

    assert excinfo.value.code == ErrorCode.UNSUPPORTED_FEATURE
    assert scope.command_log == []


def test_measure_exported_from_service_package() -> None:
    from rigol_oscilloscope_mcp import service

    assert service.measure is measure
    assert service.get_meter_value is get_meter_value


def test_measure_passes_second_source_for_delay(driver: ScopeDriver) -> None:
    """遅延・位相は第2ソースが要る(ガイド3.17.2 の <item>[,<src>[,<src>]])。"""
    result = measure(driver, "CH1", ["delay_rise_rise"], channel_b="CH2")

    assert result["channel"] == "CH1"
    assert result["channel_b"] == "CH2"
    assert "delay_rise_rise_s" in result["values"]


def test_measure_omits_channel_b_key_when_unused(driver: ScopeDriver) -> None:
    result = measure(driver, "CH1", ["vpp"])

    assert "channel_b" not in result


def test_measure_warns_when_too_many_items_are_requested(driver: ScopeDriver) -> None:
    """**実機実測**: 同時に有効化する項目が多いと一部が番兵値になる。

    MHO98では16項目以上で毎回ランダムに数項目が `unknown` になり、読み直しても
    収束しない(12項目以下なら2巡目で収束する)。呼び出し側が「測れない」と
    誤解しないよう警告を添える。
    """
    from rigol_oscilloscope_mcp.driver.scope import MEASUREMENT_KEYS

    names = list(MEASUREMENT_KEYS)[:16]
    result = measure(driver, "CH1", names)

    assert any("16" in w or "at once" in w for w in result["warnings"])


def test_measure_does_not_warn_for_a_small_batch(driver: ScopeDriver) -> None:
    result = measure(driver, "CH1", ["frequency", "vpp"])

    assert result["warnings"] == []
