"""service/state.py のテスト。

get_state は MCPサーバー層がそのままToolレスポンスにする dict を返すため、
「JSONシリアライズ可能なプリミティブのみ」「指定セクションのみを取得する
(=不要なクエリを送らない)」の2点を退行ガードとして検証する。
"""

import json

import pytest

from rigol_oscilloscope_mcp.driver.scope import ScopeDriver
from rigol_oscilloscope_mcp.driver.session import ScpiSession
from rigol_oscilloscope_mcp.errors import ErrorCode, ScopeError
from rigol_oscilloscope_mcp.profiles import load_profile
from rigol_oscilloscope_mcp.service.state import (
    VALID_SECTIONS,
    get_acquisition_dict,
    get_channel_dict,
    get_state,
    get_timebase_dict,
    get_trigger_dict,
)
from rigol_oscilloscope_mcp.testing import FakeScope, FakeTransport

# get_state 全取得のクエリ数上限(ch 7×4 + timebase 4 + trigger 6 + acquisition 1 = 39)
MAX_FULL_QUERIES = 40


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


def sent(scope: FakeScope, needle: str) -> list[str]:
    return [c for c in scope.command_log if needle in c.upper()]


# --------------------------------------------------------------------------
# 全セクション
# --------------------------------------------------------------------------


def test_get_state_returns_all_sections(driver: ScopeDriver) -> None:
    state = get_state(driver)

    assert set(state) == set(VALID_SECTIONS)


def test_get_state_channels_cover_all_analog_channels(driver: ScopeDriver) -> None:
    channels = get_state(driver)["channels"]

    assert list(channels) == ["CH1", "CH2", "CH3", "CH4"]


def test_get_state_channel_values_match_fake_defaults(driver: ScopeDriver) -> None:
    ch1 = get_state(driver)["channels"]["CH1"]

    assert ch1["enabled"] is True
    assert ch1["scale_v_per_div"] == 10.0
    assert ch1["offset_v"] == 0.0
    assert ch1["coupling"] == "DC"
    assert ch1["impedance"] == "1M"
    assert ch1["probe_ratio"] == 10.0
    assert ch1["bandwidth_limit"] is False


def test_get_state_channels_skip_impedance_without_capability(scope: FakeScope) -> None:
    """impedance_control 未宣言のプロファイルでは :IMPedance? を一切送らない。"""
    channels = get_state(make_driver(scope, "rigol-generic"))["channels"]

    assert all(ch["impedance"] == "unknown" for ch in channels.values())
    assert sent(scope, ":IMPEDANCE") == []


def test_get_state_timebase_values_match_fake_defaults(driver: ScopeDriver) -> None:
    timebase = get_state(driver)["timebase"]

    assert timebase["scale_s_per_div"] == pytest.approx(2.0e-4)
    assert timebase["position_s"] == 0.0
    assert timebase["sample_rate_sa_per_s"] == pytest.approx(5.0e6)
    assert timebase["memory_depth"] == pytest.approx(1.0e4)


def test_get_state_trigger_values_match_fake_defaults(driver: ScopeDriver) -> None:
    trigger = get_state(driver)["trigger"]

    assert trigger["type"] == "edge"
    assert trigger["source"] == "CH1"
    assert trigger["level_v"] == 0.0
    assert trigger["slope"] == "rising"
    assert trigger["sweep_mode"] == "auto"
    assert trigger["status"] == "TD"


def test_get_state_acquisition_running(driver: ScopeDriver) -> None:
    acquisition = get_state(driver)["acquisition"]

    assert acquisition == {"trigger_status": "TD", "running": True}


def test_get_state_acquisition_stopped(driver: ScopeDriver, scope: FakeScope) -> None:
    driver.stop()

    acquisition = get_state(driver)["acquisition"]

    assert acquisition == {"trigger_status": "STOP", "running": False}


def test_get_state_is_json_serializable(driver: ScopeDriver) -> None:
    # server層がそのままToolレスポンスにするため、dataclass等が混ざってはならない
    text = json.dumps(get_state(driver))

    assert json.loads(text)["channels"]["CH1"]["coupling"] == "DC"


# --------------------------------------------------------------------------
# セクション絞り込み
# --------------------------------------------------------------------------


def test_get_state_trigger_only_returns_single_key(driver: ScopeDriver) -> None:
    state = get_state(driver, ["trigger"])

    assert set(state) == {"trigger"}


def test_get_state_trigger_only_sends_no_channel_queries(
    driver: ScopeDriver, scope: FakeScope
) -> None:
    scope.command_log.clear()

    get_state(driver, ["trigger"])

    assert sent(scope, ":CHAN") == []


def test_get_state_accepts_section_combination(driver: ScopeDriver) -> None:
    state = get_state(driver, ["channels", "timebase"])

    assert set(state) == {"channels", "timebase"}


def test_get_state_section_order_does_not_change_keys(driver: ScopeDriver) -> None:
    state = get_state(driver, ["timebase", "channels"])

    assert set(state) == {"channels", "timebase"}


def test_get_state_timebase_only_sends_no_trigger_queries(
    driver: ScopeDriver, scope: FakeScope
) -> None:
    scope.command_log.clear()

    get_state(driver, ["timebase"])

    assert sent(scope, ":TRIG") == []


def test_get_state_rejects_unknown_section(driver: ScopeDriver) -> None:
    with pytest.raises(ScopeError) as excinfo:
        get_state(driver, ["trigger", "bogus", "nope"])

    assert excinfo.value.code == ErrorCode.INVALID_PARAMETER
    assert excinfo.value.detail["invalid_sections"] == ["bogus", "nope"]
    assert excinfo.value.detail["valid"] == list(VALID_SECTIONS)


def test_get_state_rejects_unknown_section_without_sending(
    driver: ScopeDriver, scope: FakeScope
) -> None:
    scope.command_log.clear()

    with pytest.raises(ScopeError):
        get_state(driver, ["bogus"])

    assert scope.command_log == []


def test_get_state_rejects_empty_sections(driver: ScopeDriver) -> None:
    with pytest.raises(ScopeError) as excinfo:
        get_state(driver, [])

    assert excinfo.value.code == ErrorCode.INVALID_PARAMETER


# --------------------------------------------------------------------------
# クエリ数(退行ガード)
# --------------------------------------------------------------------------


def test_get_state_full_query_count_is_bounded(
    driver: ScopeDriver, scope: FakeScope
) -> None:
    scope.command_log.clear()

    get_state(driver)

    assert len(scope.command_log) <= MAX_FULL_QUERIES


def test_get_state_channels_use_seven_queries_per_channel(
    driver: ScopeDriver, scope: FakeScope
) -> None:
    scope.command_log.clear()

    get_state(driver, ["channels"])

    assert len(scope.command_log) == 7 * 4


# --------------------------------------------------------------------------
# 個別セクション取得
# --------------------------------------------------------------------------


def test_get_channel_dict(driver: ScopeDriver) -> None:
    result = get_channel_dict(driver, "CH2")

    assert result["channel"] == "CH2"
    assert result["enabled"] is False
    assert json.dumps(result)


def test_get_channel_dict_rejects_unknown_channel(driver: ScopeDriver) -> None:
    with pytest.raises(ScopeError) as excinfo:
        get_channel_dict(driver, "CH9")

    assert excinfo.value.code == ErrorCode.INVALID_PARAMETER


def test_get_timebase_dict(driver: ScopeDriver) -> None:
    assert get_timebase_dict(driver)["scale_s_per_div"] == pytest.approx(2.0e-4)


def test_get_trigger_dict(driver: ScopeDriver) -> None:
    assert get_trigger_dict(driver)["source"] == "CH1"


def test_get_acquisition_dict(driver: ScopeDriver) -> None:
    assert get_acquisition_dict(driver) == {"trigger_status": "TD", "running": True}


def test_get_acquisition_dict_single_is_running(driver: ScopeDriver) -> None:
    driver.single()

    # WAIT(トリガ待ち)は停止ではない
    assert get_acquisition_dict(driver) == {"trigger_status": "WAIT", "running": True}


def test_get_state_matches_individual_getters(driver: ScopeDriver) -> None:
    state = get_state(driver)

    assert state["timebase"] == get_timebase_dict(driver)
    assert state["trigger"] == get_trigger_dict(driver)
    assert state["channels"]["CH1"] == get_channel_dict(driver, "CH1")


def test_get_state_exported_from_service_package() -> None:
    from rigol_oscilloscope_mcp import service

    assert service.get_state is get_state
