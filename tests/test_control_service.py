"""service/control.py のテスト(tools.md 3章・4章)。

control service は「Safety Layer(confirmトークン・監査ログ)とドライバの結合点」で
あるため、退行ガードは次の3点に集中する:

1. **requested / applied の分離**(Requirements.md 7.3): 機器がスナップしても
   要求値を書き換えず、両方を返すこと
2. **confirmトークンの門番**(Requirements.md 6.2): 承認が必要な操作は
   トークン無しでは**機器へ1コマンドも送らない**こと
3. **監査ログ**(Requirements.md 7.6): 成功・失敗・トークン発行/消費/拒否が
   JSONLに残ること
"""

import json
from pathlib import Path

import pytest

from rigol_oscilloscope_mcp.driver.scope import ScopeDriver
from rigol_oscilloscope_mcp.driver.session import ScpiSession
from rigol_oscilloscope_mcp.errors import ErrorCode, ScopeError
from rigol_oscilloscope_mcp.profiles import load_profile
from rigol_oscilloscope_mcp.safety import AuditLogger, ConfirmTokenStore, token_digest
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


@pytest.fixture
def audit_path(tmp_path: Path) -> Path:
    return tmp_path / "audit.jsonl"


@pytest.fixture
def store() -> ConfirmTokenStore:
    return ConfirmTokenStore()


@pytest.fixture
def service(store: ConfirmTokenStore, audit_path: Path) -> ControlService:
    return ControlService(store, AuditLogger(audit_path))


# --------------------------------------------------------------------------
# ヘルパ
# --------------------------------------------------------------------------


def rows(path: Path) -> list[dict]:
    """監査JSONLを1行1dictで読む(未作成なら空)。"""
    if not path.exists():
        return []
    return [
        json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()
    ]


def operations(path: Path) -> list[dict]:
    """操作記録(record)の行のみ。"""
    return [row for row in rows(path) if "result" in row]


def confirms(path: Path) -> list[dict]:
    """confirmトークンの記録(record_confirm)の行のみ。"""
    return [row for row in rows(path) if "event" in row]


def sent(scope: FakeScope, needle: str) -> list[str]:
    return [c for c in scope.command_log if needle in c.upper()]


def writes(scope: FakeScope, needle: str) -> list[str]:
    """問い合わせ(`?`)を除いた送信コマンド。"""
    return [c for c in sent(scope, needle) if not c.strip().endswith("?")]


# ==========================================================================
# configure_channel — 正常系
# ==========================================================================


def test_configure_channel_returns_requested_and_applied(
    service: ControlService, driver: ScopeDriver
) -> None:
    result = service.configure_channel(
        driver, 0, "CH1", scale_v_per_div=3.0, coupling="AC"
    )

    assert result["requested"] == {"scale_v_per_div": 3.0, "coupling": "AC"}
    assert result["applied"] == {"scale_v_per_div": 3.0, "coupling": "AC"}


def test_configure_channel_returns_normalized_channel(
    service: ControlService, driver: ScopeDriver
) -> None:
    result = service.configure_channel(driver, 0, "chan1", scale_v_per_div=1.0)

    assert result["channel"] == "CH1"


def test_configure_channel_reports_changed(
    service: ControlService, driver: ScopeDriver
) -> None:
    result = service.configure_channel(
        driver, 0, "CH1", scale_v_per_div=3.0, coupling="AC"
    )

    assert result["changed"] is True


def test_configure_channel_no_op_is_not_changed(
    service: ControlService, driver: ScopeDriver
) -> None:
    # FakeScopeの既定は coupling=DC。同値の設定は状態を変えない
    result = service.configure_channel(driver, 0, "CH1", coupling="DC")

    assert result["changed"] is False


def test_configure_channel_applies_to_device(
    service: ControlService, driver: ScopeDriver, scope: FakeScope
) -> None:
    service.configure_channel(driver, 0, "CH1", scale_v_per_div=3.0, coupling="AC")

    assert scope.channels[1]["scale"] == pytest.approx(3.0)
    assert scope.channels[1]["coupling"] == "AC"


def test_configure_channel_result_is_json_serializable(
    service: ControlService, driver: ScopeDriver
) -> None:
    result = service.configure_channel(driver, 0, "CH1", enabled=False)

    assert json.loads(json.dumps(result))["applied"] == {"enabled": False}


@pytest.mark.parametrize(
    ("kwargs", "key", "expected"),
    [
        ({"enabled": False}, "enabled", False),
        ({"offset_v": -1.5}, "offset_v", -1.5),
        ({"probe_ratio": 1.0}, "probe_ratio", 1.0),
        ({"bandwidth_limit": True}, "bandwidth_limit", True),
        ({"impedance": "1M"}, "impedance", "1M"),
    ],
)
def test_configure_channel_each_field_round_trips(
    service: ControlService, driver: ScopeDriver, kwargs: dict, key: str, expected: object
) -> None:
    result = service.configure_channel(driver, 0, "CH1", **kwargs)

    assert result["applied"] == {key: expected}


def test_configure_channel_unspecified_fields_are_untouched(
    service: ControlService, driver: ScopeDriver, scope: FakeScope
) -> None:
    service.configure_channel(driver, 0, "CH1", coupling="AC")

    assert scope.channels[1]["scale"] == pytest.approx(10.0)
    assert scope.channels[1]["probe"] == pytest.approx(10.0)
    assert writes(scope, ":CHAN") == [":CHANnel1:COUPling AC"]


# --- requested ≠ applied(7.3 の核心)---------------------------------------


def test_configure_channel_applied_differs_when_device_snaps(store: ConfirmTokenStore, audit_path: Path) -> None:
    """機器がスナップしても requested は要求値のまま(LLMは applied を信頼する)。"""
    scope = FakeScope(snap_to_125=True)
    service = ControlService(store, AuditLogger(audit_path))

    result = service.configure_channel(make_driver(scope), 0, "CH1", scale_v_per_div=3.0)

    assert result["requested"]["scale_v_per_div"] == 3.0
    assert result["applied"]["scale_v_per_div"] == pytest.approx(2.0)


def test_configure_channel_snapped_value_is_recorded_as_changed(
    store: ConfirmTokenStore, audit_path: Path
) -> None:
    scope = FakeScope(snap_to_125=True)
    service = ControlService(store, AuditLogger(audit_path))

    result = service.configure_channel(make_driver(scope), 0, "CH1", scale_v_per_div=3.0)

    assert result["changed"] is True


# --- 引数検証 ---------------------------------------------------------------


def test_configure_channel_rejects_all_none(
    service: ControlService, driver: ScopeDriver
) -> None:
    with pytest.raises(ScopeError) as excinfo:
        service.configure_channel(driver, 0, "CH1")

    assert excinfo.value.code == ErrorCode.INVALID_PARAMETER


def test_configure_channel_all_none_sends_nothing(
    service: ControlService, driver: ScopeDriver, scope: FakeScope
) -> None:
    scope.command_log.clear()

    with pytest.raises(ScopeError):
        service.configure_channel(driver, 0, "CH1")

    assert scope.command_log == []


# --- 監査ログ ---------------------------------------------------------------


def test_configure_channel_writes_single_audit_row(
    service: ControlService, driver: ScopeDriver, audit_path: Path
) -> None:
    service.configure_channel(driver, 0, "CH1", scale_v_per_div=3.0, coupling="AC")

    assert len(operations(audit_path)) == 1


def test_configure_channel_audit_row_has_before_after_result(
    service: ControlService, driver: ScopeDriver, audit_path: Path
) -> None:
    service.configure_channel(driver, 0, "CH1", scale_v_per_div=3.0, coupling="AC")
    row = operations(audit_path)[0]

    assert row["tool"] == "configure_channel"
    assert row["result"] == "success"
    assert row["before"]["scale_v_per_div"] == pytest.approx(10.0)
    assert row["before"]["coupling"] == "DC"
    assert row["after"]["scale_v_per_div"] == pytest.approx(3.0)
    assert row["after"]["coupling"] == "AC"


def test_configure_channel_audit_requested_includes_channel(
    service: ControlService, driver: ScopeDriver, audit_path: Path
) -> None:
    """Requirements.md 7.6 の記録例は requested に channel を含む。"""
    service.configure_channel(driver, 0, "CH1", scale_v_per_div=3.0)

    assert operations(audit_path)[0]["requested"] == {
        "channel": "CH1",
        "scale_v_per_div": 3.0,
    }


def test_configure_channel_audit_has_timestamp(
    service: ControlService, driver: ScopeDriver, audit_path: Path
) -> None:
    service.configure_channel(driver, 0, "CH1", scale_v_per_div=3.0)

    assert operations(audit_path)[0]["timestamp"].endswith("Z")


# --- エラー経路 -------------------------------------------------------------


def test_configure_channel_unknown_channel_raises(
    service: ControlService, driver: ScopeDriver
) -> None:
    with pytest.raises(ScopeError) as excinfo:
        service.configure_channel(driver, 0, "CH9", scale_v_per_div=1.0)

    assert excinfo.value.code == ErrorCode.INVALID_PARAMETER


def test_configure_channel_error_is_audited(
    service: ControlService, driver: ScopeDriver, audit_path: Path
) -> None:
    with pytest.raises(ScopeError):
        service.configure_channel(driver, 0, "CH9", scale_v_per_div=1.0)

    row = operations(audit_path)[0]
    assert row["result"] == "error"
    assert row["detail"]["error"]["code"] == ErrorCode.INVALID_PARAMETER


def test_configure_channel_invalid_coupling_is_audited(
    service: ControlService, driver: ScopeDriver, audit_path: Path
) -> None:
    with pytest.raises(ScopeError) as excinfo:
        service.configure_channel(driver, 0, "CH1", coupling="XX")

    assert excinfo.value.code == ErrorCode.INVALID_PARAMETER
    assert operations(audit_path)[0]["result"] == "error"


# ==========================================================================
# configure_channel — 50Ω(RESTRICTED_WRITE)
# ==========================================================================


def request_50ohm(service: ControlService, driver: ScopeDriver, **kwargs) -> ScopeError:
    """1回目の呼び出し(トークン無し)。送出された ScopeError を返す。"""
    with pytest.raises(ScopeError) as excinfo:
        service.configure_channel(driver, 0, "CH1", impedance="50", **kwargs)
    return excinfo.value


def test_50ohm_requires_confirmation(
    service: ControlService, driver: ScopeDriver
) -> None:
    error = request_50ohm(service, driver)

    assert error.code == ErrorCode.USER_CONFIRMATION_REQUIRED


def test_50ohm_confirmation_detail_carries_token_and_instruction(
    service: ControlService, driver: ScopeDriver
) -> None:
    detail = request_50ohm(service, driver).detail

    assert isinstance(detail["confirm_token"], str) and detail["confirm_token"]
    assert "human" in detail["instruction"]
    assert detail["description"]
    assert detail["risk"]
    assert detail["expires_in_s"] > 0


def test_50ohm_message_explains_damage_risk(
    service: ControlService, driver: ScopeDriver
) -> None:
    """文言退行ガード: 過大入力による機器破損リスクを伝えること。"""
    error = request_50ohm(service, driver)

    assert "50" in error.message
    assert "damage" in error.message


def test_50ohm_without_token_sends_no_impedance_command(
    service: ControlService, driver: ScopeDriver, scope: FakeScope
) -> None:
    scope.command_log.clear()

    request_50ohm(service, driver)

    assert sent(scope, "IMP") == []


def test_50ohm_without_token_sends_nothing_at_all(
    service: ControlService, driver: ScopeDriver, scope: FakeScope
) -> None:
    scope.command_log.clear()

    request_50ohm(service, driver)

    assert scope.command_log == []


def test_50ohm_issue_is_audited(
    service: ControlService, driver: ScopeDriver, audit_path: Path
) -> None:
    error = request_50ohm(service, driver)
    row = confirms(audit_path)[0]

    assert row["event"] == "issued"
    assert row["tool"] == "configure_channel"
    assert row["token_digest"] == token_digest(error.detail["confirm_token"])


def test_50ohm_audit_never_contains_raw_token(
    service: ControlService, driver: ScopeDriver, audit_path: Path
) -> None:
    token = request_50ohm(service, driver).detail["confirm_token"]

    assert token not in audit_path.read_text(encoding="utf-8")


def test_50ohm_with_token_succeeds(
    service: ControlService, driver: ScopeDriver
) -> None:
    token = request_50ohm(service, driver).detail["confirm_token"]

    result = service.configure_channel(
        driver, 0, "CH1", impedance="50", confirm_token=token
    )

    assert result["applied"] == {"impedance": "50"}


def test_50ohm_with_token_reaches_device(
    service: ControlService, driver: ScopeDriver, scope: FakeScope
) -> None:
    token = request_50ohm(service, driver).detail["confirm_token"]

    service.configure_channel(driver, 0, "CH1", impedance="50", confirm_token=token)

    assert scope.channels[1]["impedance"] == "FIFT"


def test_50ohm_consumption_is_audited(
    service: ControlService, driver: ScopeDriver, audit_path: Path
) -> None:
    token = request_50ohm(service, driver).detail["confirm_token"]

    service.configure_channel(driver, 0, "CH1", impedance="50", confirm_token=token)

    assert [row["event"] for row in confirms(audit_path)] == ["issued", "consumed"]


def test_50ohm_token_is_single_use(
    service: ControlService, driver: ScopeDriver
) -> None:
    token = request_50ohm(service, driver).detail["confirm_token"]
    service.configure_channel(driver, 0, "CH1", impedance="50", confirm_token=token)

    with pytest.raises(ScopeError) as excinfo:
        service.configure_channel(driver, 0, "CH1", impedance="50", confirm_token=token)

    assert excinfo.value.detail["reason"] == "unknown_token"


def test_50ohm_rejects_other_generation(
    service: ControlService, driver: ScopeDriver
) -> None:
    """接続が切り替わった後のトークンは無効(6.2)。"""
    token = request_50ohm(service, driver).detail["confirm_token"]

    with pytest.raises(ScopeError) as excinfo:
        service.configure_channel(
            driver, 1, "CH1", impedance="50", confirm_token=token
        )

    assert excinfo.value.detail["reason"] == "generation_mismatch"


def test_50ohm_generation_mismatch_is_audited_as_rejected(
    service: ControlService, driver: ScopeDriver, audit_path: Path
) -> None:
    token = request_50ohm(service, driver).detail["confirm_token"]

    with pytest.raises(ScopeError):
        service.configure_channel(driver, 1, "CH1", impedance="50", confirm_token=token)

    assert [row["event"] for row in confirms(audit_path)] == ["issued", "rejected"]


def test_50ohm_generation_mismatch_sends_nothing(
    service: ControlService, driver: ScopeDriver, scope: FakeScope
) -> None:
    token = request_50ohm(service, driver).detail["confirm_token"]
    scope.command_log.clear()

    with pytest.raises(ScopeError):
        service.configure_channel(driver, 1, "CH1", impedance="50", confirm_token=token)

    assert scope.command_log == []


def test_50ohm_token_rejects_changed_channel(
    service: ControlService, driver: ScopeDriver
) -> None:
    """トークンは操作内容にバインドされる(引数を変えた再利用は無効)。"""
    token = request_50ohm(service, driver).detail["confirm_token"]

    with pytest.raises(ScopeError) as excinfo:
        service.configure_channel(driver, 0, "CH2", impedance="50", confirm_token=token)

    assert excinfo.value.detail["reason"] == "args_mismatch"


def test_50ohm_token_rejects_added_field(
    service: ControlService, driver: ScopeDriver
) -> None:
    token = request_50ohm(service, driver).detail["confirm_token"]

    with pytest.raises(ScopeError) as excinfo:
        service.configure_channel(
            driver, 0, "CH1", impedance="50", scale_v_per_div=1.0, confirm_token=token
        )

    assert excinfo.value.detail["reason"] == "args_mismatch"


def test_50ohm_token_binds_other_fields(
    service: ControlService, driver: ScopeDriver
) -> None:
    """付随項目まで含めて同一なら実行できる。"""
    error = request_50ohm(service, driver, coupling="DC")
    token = error.detail["confirm_token"]

    result = service.configure_channel(
        driver, 0, "CH1", impedance="50", coupling="DC", confirm_token=token
    )

    assert result["applied"] == {"coupling": "DC", "impedance": "50"}


def test_1m_impedance_needs_no_confirmation(
    service: ControlService, driver: ScopeDriver
) -> None:
    result = service.configure_channel(driver, 0, "CH1", impedance="1M")

    assert result["applied"] == {"impedance": "1M"}


def test_1m_impedance_writes_no_confirm_audit(
    service: ControlService, driver: ScopeDriver, audit_path: Path
) -> None:
    service.configure_channel(driver, 0, "CH1", impedance="1M")

    assert confirms(audit_path) == []


def test_stray_confirm_token_on_safe_write_is_ignored(
    service: ControlService, driver: ScopeDriver
) -> None:
    """承認不要な操作は、トークンが付いていても素通しする(消費もしない)。"""
    result = service.configure_channel(
        driver, 0, "CH1", scale_v_per_div=1.0, confirm_token="bogus"
    )

    assert result["applied"] == {"scale_v_per_div": 1.0}


# ==========================================================================
# configure_timebase
# ==========================================================================


def test_configure_timebase_returns_requested_and_applied(
    service: ControlService, driver: ScopeDriver
) -> None:
    result = service.configure_timebase(driver, scale_s_per_div=1.0e-3)

    assert result["requested"] == {"scale_s_per_div": 1.0e-3}
    assert result["applied"]["scale_s_per_div"] == pytest.approx(1.0e-3)


def test_configure_timebase_sets_both_fields(
    service: ControlService, driver: ScopeDriver, scope: FakeScope
) -> None:
    result = service.configure_timebase(
        driver, scale_s_per_div=1.0e-3, position_s=5.0e-4
    )

    assert result["applied"]["position_s"] == pytest.approx(5.0e-4)
    assert scope.timebase["scale"] == pytest.approx(1.0e-3)
    assert scope.timebase["offset"] == pytest.approx(5.0e-4)


def test_configure_timebase_reports_changed(
    service: ControlService, driver: ScopeDriver
) -> None:
    assert service.configure_timebase(driver, scale_s_per_div=1.0e-3)["changed"] is True


def test_configure_timebase_no_op_is_not_changed(
    service: ControlService, driver: ScopeDriver
) -> None:
    # FakeScopeの既定は 2.0e-4 s/div
    assert service.configure_timebase(driver, scale_s_per_div=2.0e-4)["changed"] is False


def test_configure_timebase_rejects_all_none(
    service: ControlService, driver: ScopeDriver
) -> None:
    with pytest.raises(ScopeError) as excinfo:
        service.configure_timebase(driver)

    assert excinfo.value.code == ErrorCode.INVALID_PARAMETER


def test_configure_timebase_all_none_sends_nothing(
    service: ControlService, driver: ScopeDriver, scope: FakeScope
) -> None:
    scope.command_log.clear()

    with pytest.raises(ScopeError):
        service.configure_timebase(driver)

    assert scope.command_log == []


def test_configure_timebase_is_audited(
    service: ControlService, driver: ScopeDriver, audit_path: Path
) -> None:
    service.configure_timebase(driver, scale_s_per_div=1.0e-3)
    row = operations(audit_path)[0]

    assert row["tool"] == "configure_timebase"
    assert row["result"] == "success"
    assert row["before"]["scale_s_per_div"] == pytest.approx(2.0e-4)
    assert row["after"]["scale_s_per_div"] == pytest.approx(1.0e-3)


def test_configure_timebase_needs_no_confirmation(
    service: ControlService, driver: ScopeDriver, audit_path: Path
) -> None:
    service.configure_timebase(driver, position_s=1.0e-4)

    assert confirms(audit_path) == []


# ==========================================================================
# configure_trigger
# ==========================================================================


def test_configure_trigger_returns_requested_and_applied(
    service: ControlService, driver: ScopeDriver
) -> None:
    result = service.configure_trigger(driver, level_v=1.5, slope="falling")

    assert result["requested"] == {"level_v": 1.5, "slope": "falling"}
    assert result["applied"] == {"level_v": pytest.approx(1.5), "slope": "falling"}


def test_configure_trigger_returns_full_trigger_state(
    service: ControlService, driver: ScopeDriver
) -> None:
    trigger = service.configure_trigger(driver, level_v=1.5, slope="falling")["trigger"]

    assert trigger["type"] == "edge"
    assert trigger["source"] == "CH1"
    assert trigger["level_v"] == pytest.approx(1.5)
    assert trigger["slope"] == "falling"
    assert trigger["sweep_mode"] == "auto"
    assert trigger["status"] == "TD"


def test_configure_trigger_sets_source_and_sweep(
    service: ControlService, driver: ScopeDriver, scope: FakeScope
) -> None:
    result = service.configure_trigger(driver, source="CH2", sweep_mode="normal")

    assert result["applied"] == {"source": "CH2", "sweep_mode": "normal"}
    assert scope.trigger["source"] == "CHAN2"
    assert scope.trigger["sweep"] == "NORM"


def test_configure_trigger_reports_changed(
    service: ControlService, driver: ScopeDriver
) -> None:
    assert service.configure_trigger(driver, level_v=1.5)["changed"] is True


def test_configure_trigger_no_op_is_not_changed(
    service: ControlService, driver: ScopeDriver
) -> None:
    assert service.configure_trigger(driver, slope="rising")["changed"] is False


def test_configure_trigger_rejects_all_none(
    service: ControlService, driver: ScopeDriver
) -> None:
    with pytest.raises(ScopeError) as excinfo:
        service.configure_trigger(driver)

    assert excinfo.value.code == ErrorCode.INVALID_PARAMETER


def test_configure_trigger_all_none_sends_nothing(
    service: ControlService, driver: ScopeDriver, scope: FakeScope
) -> None:
    scope.command_log.clear()

    with pytest.raises(ScopeError):
        service.configure_trigger(driver)

    assert scope.command_log == []


def test_configure_trigger_rejects_unknown_slope(
    service: ControlService, driver: ScopeDriver
) -> None:
    with pytest.raises(ScopeError) as excinfo:
        service.configure_trigger(driver, slope="sideways")

    assert excinfo.value.code == ErrorCode.INVALID_PARAMETER


def test_configure_trigger_error_is_audited(
    service: ControlService, driver: ScopeDriver, audit_path: Path
) -> None:
    with pytest.raises(ScopeError):
        service.configure_trigger(driver, slope="sideways")

    assert operations(audit_path)[0]["result"] == "error"


def test_configure_trigger_is_audited(
    service: ControlService, driver: ScopeDriver, audit_path: Path
) -> None:
    service.configure_trigger(driver, level_v=1.5, slope="falling")
    row = operations(audit_path)[0]

    assert row["tool"] == "configure_trigger"
    assert row["result"] == "success"
    assert row["before"]["level_v"] == 0.0
    assert row["after"]["level_v"] == pytest.approx(1.5)


# ==========================================================================
# run / stop / single
# ==========================================================================


def test_run_returns_ok_and_status(
    service: ControlService, driver: ScopeDriver
) -> None:
    driver.stop()

    assert service.run(driver) == {"result": "ok", "trigger_status": "TD"}


def test_stop_returns_ok_and_status(
    service: ControlService, driver: ScopeDriver
) -> None:
    assert service.stop(driver) == {"result": "ok", "trigger_status": "STOP"}


def test_single_returns_ok_and_status(
    service: ControlService, driver: ScopeDriver
) -> None:
    assert service.single(driver) == {"result": "ok", "trigger_status": "WAIT"}


@pytest.mark.parametrize(
    ("method", "state"),
    [("run", "RUN"), ("stop", "STOP"), ("single", "SINGLE")],
)
def test_acquisition_changes_device_state(
    service: ControlService, driver: ScopeDriver, scope: FakeScope, method: str, state: str
) -> None:
    getattr(service, method)(driver)

    assert scope.acquisition == state


@pytest.mark.parametrize("method", ["run", "stop", "single"])
def test_acquisition_is_audited(
    service: ControlService, driver: ScopeDriver, audit_path: Path, method: str
) -> None:
    getattr(service, method)(driver)
    row = operations(audit_path)[0]

    assert row["tool"] == method
    assert row["result"] == "success"


def test_stop_audit_records_before_and_after_status(
    service: ControlService, driver: ScopeDriver, audit_path: Path
) -> None:
    row_before = operations(audit_path)
    assert row_before == []

    service.stop(driver)
    row = operations(audit_path)[0]

    assert row["before"] == {"trigger_status": "TD"}
    assert row["after"] == {"trigger_status": "STOP"}


@pytest.mark.parametrize("method", ["run", "stop", "single"])
def test_acquisition_needs_no_confirmation(
    service: ControlService, driver: ScopeDriver, audit_path: Path, method: str
) -> None:
    getattr(service, method)(driver)

    assert confirms(audit_path) == []


# ==========================================================================
# autoset(RESTRICTED_WRITE)
# ==========================================================================


def request_autoset(service: ControlService, driver: ScopeDriver, generation: int = 0) -> ScopeError:
    with pytest.raises(ScopeError) as excinfo:
        service.autoset(driver, generation)
    return excinfo.value


def test_autoset_requires_confirmation(
    service: ControlService, driver: ScopeDriver
) -> None:
    assert request_autoset(service, driver).code == ErrorCode.USER_CONFIRMATION_REQUIRED


def test_autoset_confirmation_detail_carries_token_and_instruction(
    service: ControlService, driver: ScopeDriver
) -> None:
    detail = request_autoset(service, driver).detail

    assert isinstance(detail["confirm_token"], str) and detail["confirm_token"]
    assert "human" in detail["instruction"]
    assert detail["expires_in_s"] > 0


def test_autoset_risk_mentions_setting_change(
    service: ControlService, driver: ScopeDriver
) -> None:
    detail = request_autoset(service, driver).detail

    assert "settings" in detail["risk"]
    assert "changed" in detail["risk"]


def test_autoset_without_token_sends_nothing(
    service: ControlService, driver: ScopeDriver, scope: FakeScope
) -> None:
    scope.command_log.clear()

    request_autoset(service, driver)

    assert scope.command_log == []


def test_autoset_issue_is_audited(
    service: ControlService, driver: ScopeDriver, audit_path: Path
) -> None:
    error = request_autoset(service, driver)
    row = confirms(audit_path)[0]

    assert row["event"] == "issued"
    assert row["tool"] == "autoset"
    assert row["token_digest"] == token_digest(error.detail["confirm_token"])


def test_autoset_with_token_executes(
    service: ControlService, driver: ScopeDriver, scope: FakeScope
) -> None:
    token = request_autoset(service, driver).detail["confirm_token"]
    scope.command_log.clear()

    result = service.autoset(driver, 0, confirm_token=token)

    assert result["result"] == "ok"
    assert [c for c in scope.command_log if "AUTO" in c.upper()] == [":AUToscale"]


def test_autoset_note_states_settings_changed(
    service: ControlService, driver: ScopeDriver
) -> None:
    token = request_autoset(service, driver).detail["confirm_token"]

    note = service.autoset(driver, 0, confirm_token=token)["note"]

    assert "Auto Setup" in note
    assert "settings" in note


def test_autoset_returns_state_sections(
    service: ControlService, driver: ScopeDriver
) -> None:
    token = request_autoset(service, driver).detail["confirm_token"]

    state = service.autoset(driver, 0, confirm_token=token)["state"]

    assert set(state) == {"channels", "timebase", "trigger"}
    assert state["channels"]["CH1"]["coupling"] == "DC"


def test_autoset_result_is_json_serializable(
    service: ControlService, driver: ScopeDriver
) -> None:
    token = request_autoset(service, driver).detail["confirm_token"]

    result = service.autoset(driver, 0, confirm_token=token)

    assert json.loads(json.dumps(result))["result"] == "ok"


def test_autoset_consumption_is_audited(
    service: ControlService, driver: ScopeDriver, audit_path: Path
) -> None:
    token = request_autoset(service, driver).detail["confirm_token"]

    service.autoset(driver, 0, confirm_token=token)

    assert [row["event"] for row in confirms(audit_path)] == ["issued", "consumed"]


def test_autoset_audit_records_timebase_and_trigger(
    service: ControlService, driver: ScopeDriver, audit_path: Path
) -> None:
    token = request_autoset(service, driver).detail["confirm_token"]

    service.autoset(driver, 0, confirm_token=token)
    row = operations(audit_path)[0]

    assert row["tool"] == "autoset"
    assert row["result"] == "success"
    assert set(row["before"]) == {"timebase", "trigger"}
    assert set(row["after"]) == {"timebase", "trigger"}


def test_autoset_does_not_query_all_channels_before(
    service: ControlService, driver: ScopeDriver, scope: FakeScope
) -> None:
    """クエリ数節約: 実行前にチャンネル4本分の状態は取らない(7.6のbeforeは絞る)。"""
    token = request_autoset(service, driver).detail["confirm_token"]
    scope.command_log.clear()

    service.autoset(driver, 0, confirm_token=token)

    before_autoset = scope.command_log[: scope.command_log.index(":AUToscale")]
    assert sent_in(before_autoset, ":CHAN") == []


def sent_in(commands: list[str], needle: str) -> list[str]:
    return [c for c in commands if needle in c.upper()]


def test_autoset_token_is_single_use(
    service: ControlService, driver: ScopeDriver
) -> None:
    token = request_autoset(service, driver).detail["confirm_token"]
    service.autoset(driver, 0, confirm_token=token)

    with pytest.raises(ScopeError) as excinfo:
        service.autoset(driver, 0, confirm_token=token)

    assert excinfo.value.detail["reason"] == "unknown_token"


def test_autoset_rejects_other_generation(
    service: ControlService, driver: ScopeDriver, audit_path: Path
) -> None:
    token = request_autoset(service, driver).detail["confirm_token"]

    with pytest.raises(ScopeError) as excinfo:
        service.autoset(driver, 3, confirm_token=token)

    assert excinfo.value.detail["reason"] == "generation_mismatch"
    assert [row["event"] for row in confirms(audit_path)] == ["issued", "rejected"]


def test_autoset_token_does_not_authorize_50ohm(
    service: ControlService, driver: ScopeDriver
) -> None:
    """トークンはTool名にバインドされる(別操作へ流用できない)。"""
    token = request_autoset(service, driver).detail["confirm_token"]

    with pytest.raises(ScopeError) as excinfo:
        service.configure_channel(driver, 0, "CH1", impedance="50", confirm_token=token)

    assert excinfo.value.detail["reason"] == "tool_mismatch"


# ==========================================================================
# 監査ログ無効時
# ==========================================================================


def test_works_without_audit_log(store: ConfirmTokenStore, driver: ScopeDriver) -> None:
    """path=None の AuditLogger でも操作は成立する。"""
    service = ControlService(store, AuditLogger(None))

    assert service.configure_channel(driver, 0, "CH1", coupling="AC")["changed"] is True


def test_confirm_flow_works_without_audit_log(
    store: ConfirmTokenStore, driver: ScopeDriver
) -> None:
    service = ControlService(store, AuditLogger(None))
    with pytest.raises(ScopeError) as excinfo:
        service.autoset(driver, 0)

    service.autoset(driver, 0, confirm_token=excinfo.value.detail["confirm_token"])


def test_run_skips_the_before_query_without_audit_log(
    store: ConfirmTokenStore, driver: ScopeDriver, scope: FakeScope
) -> None:
    """記録専用の事前クエリは監査無効時に発行しない(実機1クエリ≒30msの節約)。"""
    service = ControlService(store, AuditLogger(None))
    scope.command_log.clear()

    service.run(driver)

    # 返却用の1回(:RUN の後)だけ
    assert sent(scope, ":TRIGGER:STATUS?") == [":TRIGger:STATus?"]


def test_run_keeps_the_before_query_with_audit_log(
    service: ControlService, driver: ScopeDriver, scope: FakeScope, audit_path: Path
) -> None:
    """監査有効時は従来どおり before / after の2回とも問い合わせる。"""
    scope.command_log.clear()

    service.run(driver)

    assert sent(scope, ":TRIGGER:STATUS?") == [":TRIGger:STATus?"] * 2
    assert set(rows(audit_path)[-1]["before"]) == {"trigger_status"}


def test_autoset_skips_the_before_state_without_audit_log(
    store: ConfirmTokenStore, driver: ScopeDriver, scope: FakeScope
) -> None:
    service = ControlService(store, AuditLogger(None))
    with pytest.raises(ScopeError) as excinfo:
        service.autoset(driver, 0)
    scope.command_log.clear()

    service.autoset(driver, 0, confirm_token=excinfo.value.detail["confirm_token"])

    before_autoset = scope.command_log[: scope.command_log.index(":AUToscale")]
    assert before_autoset == []


# ==========================================================================
# configure_decode(tools.md 6章 / Phase 4)
# ==========================================================================


def test_configure_decode_returns_requested_and_applied(
    service: ControlService, driver: ScopeDriver
) -> None:
    result = service.configure_decode(
        driver,
        1,
        "uart",
        enabled=True,
        settings={"baud_bps": 115200, "tx_source": "CH2"},
    )

    assert result["bus"] == 1
    assert result["requested"] == {
        "enabled": True,
        "settings": {"baud_bps": 115200, "tx_source": "CH2"},
    }
    assert result["applied"] == {
        "bus": 1,
        "protocol": "uart",
        "enabled": True,
        "settings": {"baud_bps": 115200, "tx_source": "CH2"},
    }


def test_configure_decode_reports_changed(
    service: ControlService, driver: ScopeDriver
) -> None:
    assert service.configure_decode(driver, 1, "uart")["changed"] is True
    # 同じ設定の再適用は変化なし
    assert service.configure_decode(driver, 1, "uart")["changed"] is False


def test_configure_decode_is_audited(
    service: ControlService, driver: ScopeDriver, audit_path: Path
) -> None:
    service.configure_decode(driver, 1, "uart", settings={"baud_bps": 115200})
    row = operations(audit_path)[0]

    assert row["tool"] == "configure_decode"
    assert row["result"] == "success"
    assert row["requested"]["protocol"] == "uart"
    assert row["before"]["protocol"] == "parallel"  # FakeScopeの既定
    assert row["after"]["protocol"] == "uart"
    assert row["after"]["settings"]["baud_bps"] == 115200


def test_configure_decode_needs_no_confirmation(
    service: ControlService, driver: ScopeDriver, audit_path: Path
) -> None:
    """表示・解析層のみの変更で取り込み設定も出力も変えない(SAFE_WRITE)。"""
    service.configure_decode(driver, 1, "uart")

    assert confirms(audit_path) == []


def test_configure_decode_rejects_unknown_protocol(
    service: ControlService, driver: ScopeDriver
) -> None:
    with pytest.raises(ScopeError) as excinfo:
        service.configure_decode(driver, 1, "i2s")

    assert excinfo.value.code == ErrorCode.UNSUPPORTED_FEATURE


def test_configure_decode_error_is_audited(
    service: ControlService, driver: ScopeDriver, audit_path: Path
) -> None:
    with pytest.raises(ScopeError):
        service.configure_decode(driver, 1, "uart", settings={"baud_bps": 0})

    row = operations(audit_path)[0]
    assert row["result"] == "error"
    assert row["detail"]["error"]["code"] == ErrorCode.INVALID_PARAMETER


def test_configure_decode_invalid_setting_writes_nothing(
    service: ControlService, driver: ScopeDriver, scope: FakeScope
) -> None:
    """検証で弾かれた場合、バスへの書き込みは1件も出ない(読みは行う)。"""
    with pytest.raises(ScopeError):
        service.configure_decode(driver, 1, "uart", settings={"baud_bps": 0})

    assert [c for c in sent(scope, ":BUS") if "?" not in c] == []


# ==========================================================================
# configure_afg(tools.md 7章 / Phase 4)
# ==========================================================================


def test_configure_afg_returns_requested_and_applied(
    service: ControlService, driver: ScopeDriver
) -> None:
    result = service.configure_afg(
        driver, 1, waveform="square", frequency_hz=2000.0, duty_percent=60.0
    )

    assert result["channel"] == 1
    assert result["requested"] == {
        "waveform": "square",
        "frequency_hz": 2000.0,
        "duty_percent": 60.0,
    }
    assert result["applied"] == {
        "channel": 1,
        "waveform": "square",
        "frequency_hz": 2000.0,
        "duty_percent": 60.0,
    }
    assert result["changed"] is True


def test_configure_afg_reports_changed(
    service: ControlService, driver: ScopeDriver
) -> None:
    assert service.configure_afg(driver, 1, waveform="square")["changed"] is True
    # 同じ設定の再適用は変化なし
    assert service.configure_afg(driver, 1, waveform="square")["changed"] is False


def test_configure_afg_is_audited(
    service: ControlService, driver: ScopeDriver, audit_path: Path
) -> None:
    service.configure_afg(driver, 2, amplitude_vpp=1.0)
    row = operations(audit_path)[0]

    assert row["tool"] == "configure_afg"
    assert row["result"] == "success"
    assert row["requested"]["channel"] == 2
    assert row["before"]["amplitude_vpp"] == 5.0  # FakeScopeの既定(実機のガイド既定値)
    assert row["after"]["amplitude_vpp"] == 1.0


def test_configure_afg_needs_no_confirmation(
    service: ControlService, driver: ScopeDriver, audit_path: Path
) -> None:
    """出力状態に触れない設定変更なので承認は要求しない(SAFE_WRITE)。"""
    service.configure_afg(driver, 1, waveform="square")

    assert confirms(audit_path) == []


def test_configure_afg_never_touches_the_output(
    service: ControlService, driver: ScopeDriver, scope: FakeScope
) -> None:
    """信号は外へ出ない。出力のON/OFFは別Tool(承認フロー付き)の責務。"""
    service.configure_afg(driver, 1, waveform="square", amplitude_vpp=1.0)

    assert writes(scope, ":OUTP") == []
    assert scope.afg[1]["output"] is False


def test_configure_afg_without_items_sends_nothing(
    service: ControlService, driver: ScopeDriver, scope: FakeScope
) -> None:
    with pytest.raises(ScopeError) as excinfo:
        service.configure_afg(driver, 1)

    assert excinfo.value.code == ErrorCode.INVALID_PARAMETER
    assert scope.command_log == []


def test_configure_afg_error_is_audited(
    service: ControlService, driver: ScopeDriver, audit_path: Path
) -> None:
    with pytest.raises(ScopeError):
        service.configure_afg(driver, 1, waveform="pulse")

    row = operations(audit_path)[0]
    assert row["result"] == "error"
    assert row["detail"]["error"]["code"] == ErrorCode.INVALID_PARAMETER


# ==========================================================================
# enable_afg(DANGEROUS_WRITE)/ disable_afg(SAFE_WRITE)
# ==========================================================================


def request_enable_afg(
    service: ControlService,
    driver: ScopeDriver,
    channel: int = 1,
    generation: int = 0,
) -> ScopeError:
    """承認要求(トークン未指定)を1回起こし、その ScopeError を返す。"""
    with pytest.raises(ScopeError) as excinfo:
        service.enable_afg(driver, generation, channel)
    return excinfo.value


def test_enable_afg_requires_confirmation(
    service: ControlService, driver: ScopeDriver
) -> None:
    error = request_enable_afg(service, driver)

    assert error.code == ErrorCode.USER_CONFIRMATION_REQUIRED
    assert isinstance(error.detail["confirm_token"], str)
    assert "human" in error.detail["instruction"]


def test_enable_afg_risk_asks_about_the_physical_setup(
    service: ControlService, driver: ScopeDriver
) -> None:
    """リスク文言の要点: 実信号が出ること・接続先の物理確認・設定値の提示。"""
    detail = request_enable_afg(service, driver, channel=2).detail

    assert "channel 2" in detail["description"]
    risk = detail["risk"]
    assert "connected" in risk
    assert "confirm" in risk
    assert "get_afg_state" in risk


def test_enable_afg_without_token_sends_nothing(
    service: ControlService, driver: ScopeDriver, scope: FakeScope
) -> None:
    """承認前に機器へは1コマンドも送らない(出力もOFFのまま)。"""
    scope.command_log.clear()

    request_enable_afg(service, driver)

    assert scope.command_log == []
    assert scope.afg[1]["output"] is False


def test_enable_afg_issue_is_audited(
    service: ControlService, driver: ScopeDriver, audit_path: Path
) -> None:
    error = request_enable_afg(service, driver)
    row = confirms(audit_path)[0]

    assert row["event"] == "issued"
    assert row["tool"] == "enable_afg"
    assert row["token_digest"] == token_digest(error.detail["confirm_token"])


def test_enable_afg_with_token_turns_the_output_on(
    service: ControlService, driver: ScopeDriver, scope: FakeScope
) -> None:
    token = request_enable_afg(service, driver).detail["confirm_token"]

    result = service.enable_afg(driver, 0, 1, confirm_token=token)

    assert result["result"] == "ok"
    assert result["channel"] == 1
    assert result["state"]["output"] is True
    assert result["state"]["waveform"] == "sine"
    assert scope.afg[1]["output"] is True
    assert json.loads(json.dumps(result))["result"] == "ok"


def test_enable_afg_is_audited(
    service: ControlService, driver: ScopeDriver, audit_path: Path
) -> None:
    token = request_enable_afg(service, driver).detail["confirm_token"]

    service.enable_afg(driver, 0, 1, confirm_token=token)
    row = operations(audit_path)[0]

    assert row["tool"] == "enable_afg"
    assert row["result"] == "success"
    assert row["requested"] == {"channel": 1}
    assert row["before"]["output"] is False
    assert row["after"]["output"] is True
    assert [r["event"] for r in confirms(audit_path)] == ["issued", "consumed"]


def test_enable_afg_token_is_single_use(
    service: ControlService, driver: ScopeDriver
) -> None:
    token = request_enable_afg(service, driver).detail["confirm_token"]
    service.enable_afg(driver, 0, 1, confirm_token=token)

    with pytest.raises(ScopeError) as excinfo:
        service.enable_afg(driver, 0, 1, confirm_token=token)

    assert excinfo.value.detail["reason"] == "unknown_token"


def test_enable_afg_token_is_bound_to_the_channel(
    service: ControlService, driver: ScopeDriver, scope: FakeScope
) -> None:
    """ch1の承認でch2を出力ONにはできない(承認はチャンネル単位)。"""
    token = request_enable_afg(service, driver, channel=1).detail["confirm_token"]

    with pytest.raises(ScopeError) as excinfo:
        service.enable_afg(driver, 0, 2, confirm_token=token)

    assert excinfo.value.detail["reason"] == "args_mismatch"
    assert scope.afg[2]["output"] is False


def test_enable_afg_rejects_other_generation(
    service: ControlService, driver: ScopeDriver, scope: FakeScope
) -> None:
    """再接続後(世代交代後)のトークンは無効。"""
    token = request_enable_afg(service, driver).detail["confirm_token"]

    with pytest.raises(ScopeError) as excinfo:
        service.enable_afg(driver, 3, 1, confirm_token=token)

    assert excinfo.value.detail["reason"] == "generation_mismatch"
    assert scope.afg[1]["output"] is False


def test_disable_afg_needs_no_confirmation(
    service: ControlService,
    driver: ScopeDriver,
    scope: FakeScope,
    audit_path: Path,
) -> None:
    """緊急OFFを承認でブロックしない(出力停止は常に1呼び出しで通す)。"""
    scope.afg[1]["output"] = True

    result = service.disable_afg(driver, 1)

    assert result["result"] == "ok"
    assert result["channel"] == 1
    assert result["state"]["output"] is False
    assert scope.afg[1]["output"] is False
    assert confirms(audit_path) == []


def test_disable_afg_is_audited(
    service: ControlService,
    driver: ScopeDriver,
    scope: FakeScope,
    audit_path: Path,
) -> None:
    scope.afg[2]["output"] = True

    service.disable_afg(driver, 2)
    row = operations(audit_path)[0]

    assert row["tool"] == "disable_afg"
    assert row["result"] == "success"
    assert row["requested"] == {"channel": 2}
    assert row["before"]["output"] is True
    assert row["after"]["output"] is False


def test_disable_afg_of_an_already_off_channel_is_not_an_error(
    service: ControlService, driver: ScopeDriver
) -> None:
    assert service.disable_afg(driver, 1)["state"]["output"] is False


def test_afg_output_error_is_audited(
    service: ControlService, driver: ScopeDriver, audit_path: Path
) -> None:
    with pytest.raises(ScopeError):
        service.disable_afg(driver, 3)  # :SOURce3 は存在しない

    row = operations(audit_path)[0]
    assert row["result"] == "error"
    assert row["detail"]["error"]["code"] == ErrorCode.INVALID_PARAMETER


# ==========================================================================
# パッケージ公開
# ==========================================================================


def test_control_service_exported_from_service_package() -> None:
    from rigol_oscilloscope_mcp import service as service_pkg

    assert service_pkg.ControlService is ControlService
