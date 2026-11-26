"""ロジックアナライザ(:LA。ガイド3.13。issue #86)のテスト。

FakeScope は実機で確かめた挙動をなぞる(docs/verification/mho98-m6.md §18):
ポッドの表示を切り替えるとそのポッドの8chが揃って切り替わる、LA を無効にすると
ポッド・chは全て 0 と読める、16ch 有効のまま `:LA:SIZE LARGe` は `-222`。
"""

import pytest

from rigol_oscilloscope_mcp.driver.scope import ScopeDriver
from rigol_oscilloscope_mcp.driver.session import ScpiSession
from rigol_oscilloscope_mcp.errors import ErrorCode, ScopeError
from rigol_oscilloscope_mcp.profiles import load_profile
from rigol_oscilloscope_mcp.safety import AuditLogger, ConfirmTokenStore
from rigol_oscilloscope_mcp.service.control import ControlService
from rigol_oscilloscope_mcp.testing import FakeScope, FakeTransport


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


def writes(scope: FakeScope) -> list[str]:
    return [c for c in scope.command_log if "?" not in c]


def test_get_logic_analyzer_reads_everything(driver: ScopeDriver) -> None:
    state = driver.get_logic_analyzer()

    assert state["enabled"] is False
    assert state["active"] == "D0"
    assert state["autosort"] == "d15_d0"
    assert state["size"] == "medium"
    assert state["pods"] == {
        "1": {"enabled": False, "threshold_v": 0.0},
        "2": {"enabled": False, "threshold_v": 0.0},
    }
    assert list(state["channels"]) == [f"D{n}" for n in range(16)]
    assert state["channels"]["D3"] == {"enabled": False, "label": "D3"}


def test_configure_logic_analyzer_sets_and_reads_back(
    driver: ScopeDriver, scope: FakeScope
) -> None:
    applied = driver.configure_logic_analyzer(
        enabled=True,
        pods={"1": {"enabled": True, "threshold_v": 1.65}, 2: {"threshold_v": 1.65}},
        channels={"D3": {"enabled": False, "label": "SDA"}},
        active="D1",
        autosort="d0_d15",
    )

    assert applied["enabled"] is True
    assert applied["pods"] == {
        "1": {"enabled": True, "threshold_v": 1.65},
        "2": {"threshold_v": 1.65},
    }
    assert applied["channels"] == {"D3": {"enabled": False, "label": "SDA"}}
    assert applied["active"] == "D1"
    assert applied["autosort"] == "d0_d15"
    assert ":LA:POD1:THReshold 1.65" in scope.command_log
    assert ":LA:DIGital:ENABle D3,OFF" in scope.command_log
    assert ":LA:DIGital:LABel D3,SDA" in scope.command_log


def test_pods_are_sent_before_channels(driver: ScopeDriver, scope: FakeScope) -> None:
    """**実機実測**: ポッドの表示を ON にするとその8chが全て ON になる。chの指定が
    上書きされないよう、ポッドを先に送る。"""
    driver.configure_logic_analyzer(
        enabled=True, pods={"2": {"enabled": True}}, channels={"D9": {"enabled": False}}
    )

    state = driver.get_logic_analyzer()
    assert state["channels"]["D9"]["enabled"] is False
    assert state["channels"]["D8"]["enabled"] is True
    sent = writes(scope)
    assert sent.index(":LA:POD2:DISPlay ON") < sent.index(":LA:DIGital:ENABle D9,OFF")


def test_channel_names_are_case_insensitive(driver: ScopeDriver, scope: FakeScope) -> None:
    applied = driver.configure_logic_analyzer(channels={"d5": {"label": "MOSI"}}, active="d5")

    assert applied["channels"] == {"D5": {"label": "MOSI"}}
    assert applied["active"] == "D5"


@pytest.mark.parametrize(
    "kwargs",
    [
        {"channels": {"D16": {"enabled": True}}},
        {"channels": {"CH1": {"enabled": True}}},
        {"channels": {"D1": {"colour": "red"}}},
        {"channels": {"D1": True}},
        {"channels": {"D1": {"label": "has space"}}},
        {"channels": {"D1": {"label": "x;*RST"}}},
        {"pods": {"3": {"enabled": True}}},
        {"pods": {"1": {"threshold_v": 20}}},
        {"pods": {"1": {"bias": 1}}},
        {"active": "D16"},
        {"autosort": "up"},
        {"size": "huge"},
    ],
)
def test_invalid_settings_send_nothing(driver: ScopeDriver, scope: FakeScope, kwargs) -> None:
    scope.command_log.clear()

    with pytest.raises(ScopeError) as excinfo:
        driver.configure_logic_analyzer(enabled=True, **kwargs)

    assert excinfo.value.code == ErrorCode.INVALID_PARAMETER
    assert scope.command_log == []


def test_large_size_with_too_many_channels_reports_the_device_error(
    driver: ScopeDriver,
) -> None:
    """**実機実測**: 16ch 有効のまま LARGe は `-222`(ガイド: 8ch 以下のときだけ)。"""
    driver.configure_logic_analyzer(enabled=True, pods={"1": {"enabled": True}, "2": {"enabled": True}})

    with pytest.raises(ScopeError) as excinfo:
        driver.configure_logic_analyzer(size="large")

    assert excinfo.value.code == ErrorCode.SCPI_ERROR


def test_large_size_with_eight_channels(driver: ScopeDriver) -> None:
    applied = driver.configure_logic_analyzer(
        enabled=True, pods={"1": {"enabled": True}, "2": {"enabled": False}}, size="large"
    )

    assert applied["size"] == "large"


def test_disabled_logic_analyzer_reads_pods_and_channels_as_off(driver: ScopeDriver) -> None:
    """**実機実測**: LA を無効にするとポッド・chは 0 と読め、有効に戻すと元に戻る。"""
    driver.configure_logic_analyzer(enabled=True, pods={"1": {"enabled": True}})
    driver.configure_logic_analyzer(enabled=False)

    assert driver.get_logic_analyzer()["pods"]["1"]["enabled"] is False

    driver.configure_logic_analyzer(enabled=True)
    assert driver.get_logic_analyzer()["pods"]["1"]["enabled"] is True


@pytest.mark.parametrize("call", ["get", "configure"])
def test_unsupported_profile_sends_nothing(scope: FakeScope, call: str) -> None:
    driver = make_driver(scope, "rigol-generic")

    with pytest.raises(ScopeError) as excinfo:
        if call == "get":
            driver.get_logic_analyzer()
        else:
            driver.configure_logic_analyzer(enabled=True)

    assert excinfo.value.code == ErrorCode.UNSUPPORTED_FEATURE
    assert scope.command_log == []


def test_control_service_reports_requested_applied_and_change(
    driver: ScopeDriver, scope: FakeScope, tmp_path
) -> None:
    control = ControlService(ConfirmTokenStore(), AuditLogger(tmp_path / "audit.jsonl"))

    result = control.configure_logic_analyzer(
        driver, enabled=True, pods={"1": {"threshold_v": 1.65}}
    )

    assert result["requested"] == {"enabled": True, "pods": {"1": {"threshold_v": 1.65}}}
    assert result["applied"]["pods"]["1"]["threshold_v"] == pytest.approx(1.65)
    assert result["changed"] is True


def test_control_service_requires_something_to_change(driver: ScopeDriver, tmp_path) -> None:
    control = ControlService(ConfirmTokenStore(), AuditLogger(tmp_path / "audit.jsonl"))

    with pytest.raises(ScopeError) as excinfo:
        control.configure_logic_analyzer(driver)

    assert excinfo.value.code == ErrorCode.INVALID_PARAMETER
