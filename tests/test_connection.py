"""service/connection.py のテスト。

接続ライフサイクル(Requirements.md 4.4): open → drain → *IDN? → プロファイル解決。
トランスポートはファクトリ注入で FakeTransport に差し替える。
"""

import json
import threading
from pathlib import Path

import pytest

from rigol_oscilloscope_mcp.config import Config
from rigol_oscilloscope_mcp.driver.scope import ScopeDriver
from rigol_oscilloscope_mcp.errors import ErrorCode, ScopeError
from rigol_oscilloscope_mcp.service.connection import (
    ConnectionManager,
    _default_transport_factory,
)
from rigol_oscilloscope_mcp.safety import AuditLogger
from rigol_oscilloscope_mcp.testing import FakeScope, FakeTransport
from rigol_oscilloscope_mcp.transport import UsbTransport


class RecordingFactory:
    """生成引数を記録するトランスポートファクトリ。"""

    def __init__(self, scope: FakeScope | None = None) -> None:
        self.scope = scope if scope is not None else FakeScope(stale_error_queue=True)
        self.calls: list[tuple[str, str, int, float]] = []
        self.transports: list[FakeTransport] = []

    def __call__(
        self, transport: str, address: str, port: int, timeout_s: float
    ) -> FakeTransport:
        self.calls.append((transport, address, port, timeout_s))
        made = FakeTransport(self.scope)
        self.transports.append(made)
        return made


@pytest.fixture
def factory() -> RecordingFactory:
    return RecordingFactory()


@pytest.fixture
def manager(factory: RecordingFactory) -> ConnectionManager:
    return ConnectionManager(Config(), transport_factory=factory)


# --------------------------------------------------------------------------
# connect
# --------------------------------------------------------------------------


def test_connect_returns_identified_status(manager: ConnectionManager) -> None:
    status = manager.connect(address="192.0.2.10")

    assert status.connected is True
    assert status.address == "192.0.2.10"
    assert status.transport == "lan"
    assert status.port == 5555
    assert status.idn is not None
    assert status.idn.model == "MHO98"
    assert status.profile_name == "mho98"
    assert status.profile_confidence == "verified"
    assert status.unsupported_vendor is False
    assert manager.generation == 1


def test_connect_drains_stale_error_queue(
    manager: ConnectionManager, factory: RecordingFactory
) -> None:
    assert factory.scope.error_queue  # 前提: 残留エラーがある

    manager.connect(address="192.0.2.10")

    assert not factory.scope.error_queue


def test_connect_sequence_is_drain_then_idn(
    manager: ConnectionManager, factory: RecordingFactory
) -> None:
    manager.connect(address="192.0.2.10")

    log = factory.scope.command_log
    assert log[0] == ":SYSTem:ERRor?"
    assert "*IDN?" in log
    assert log.index("*IDN?") > log.index(":SYSTem:ERRor?")


def test_connect_opens_transport(
    manager: ConnectionManager, factory: RecordingFactory
) -> None:
    manager.connect(address="192.0.2.10")

    assert factory.transports[0].is_open is True


# --- 接続先の解決 ----------------------------------------------------------


def test_connect_without_address_asks_the_user(manager: ConnectionManager) -> None:
    with pytest.raises(ScopeError) as excinfo:
        manager.connect()

    error = excinfo.value
    assert error.code == ErrorCode.INVALID_PARAMETER
    assert "ユーザー" in error.message
    assert "確認" in error.message
    assert error.detail["missing"] == "address"


def test_connect_falls_back_to_config_address(factory: RecordingFactory) -> None:
    manager = ConnectionManager(
        Config(address="10.0.0.5", port=5025), transport_factory=factory
    )

    status = manager.connect()

    assert status.address == "10.0.0.5"
    assert status.port == 5025
    assert factory.calls == [("lan", "10.0.0.5", 5025, 5.0)]


def test_argument_address_wins_over_config(factory: RecordingFactory) -> None:
    manager = ConnectionManager(Config(address="10.0.0.5"), transport_factory=factory)

    status = manager.connect(address="192.0.2.10", port=1234)

    assert status.address == "192.0.2.10"
    assert factory.calls == [("lan", "192.0.2.10", 1234, 5.0)]


def test_config_transport_is_used(factory: RecordingFactory) -> None:
    manager = ConnectionManager(
        Config(address="10.0.0.5", transport="usb"), transport_factory=factory
    )

    assert manager.connect().transport == "usb"
    assert factory.calls[0][0] == "usb"


def test_visa_resource_address_infers_usb(
    manager: ConnectionManager, factory: RecordingFactory
) -> None:
    status = manager.connect(address="USB0::0x1AB1::0x044C::MHO0001::INSTR")

    assert status.transport == "usb"
    assert factory.calls[0][0] == "usb"


def test_plain_address_infers_lan(
    manager: ConnectionManager, factory: RecordingFactory
) -> None:
    manager.connect(address="192.0.2.10")

    assert factory.calls[0][0] == "lan"


def test_config_timeout_is_passed_to_factory(factory: RecordingFactory) -> None:
    manager = ConnectionManager(Config(timeout_s=1.5), transport_factory=factory)

    manager.connect(address="192.0.2.10")

    assert factory.calls[0][3] == 1.5


# --- 再接続 ----------------------------------------------------------------


def test_reconnect_replaces_previous_connection(
    manager: ConnectionManager, factory: RecordingFactory
) -> None:
    manager.connect(address="192.0.2.10")
    first = factory.transports[0]

    status = manager.connect(address="192.0.2.11")

    assert first.is_open is False
    assert factory.transports[1].is_open is True
    assert status.address == "192.0.2.11"
    assert manager.generation == 2


# --- 失敗時に既存接続を保持する --------------------------------------------


class _ExplodingTransport(FakeTransport):
    """openは成功するが問い合わせに応答しないトランスポート。"""

    def query(self, command: str, timeout_s: float | None = None) -> str:
        raise ScopeError(ErrorCode.TIMEOUT, "機器が応答しませんでした", {"command": command})


def test_failed_connect_keeps_the_live_connection(
    manager: ConnectionManager, factory: RecordingFactory
) -> None:
    manager.connect(address="192.0.2.10")
    live = factory.transports[0]
    driver = manager.require_scope()

    def failing(*args: object) -> FakeTransport:
        raise ScopeError(ErrorCode.DEVICE_NOT_FOUND, "接続できません")

    manager._transport_factory = failing  # type: ignore[attr-defined]

    with pytest.raises(ScopeError) as excinfo:
        manager.connect(address="192.0.2.99")

    assert excinfo.value.code == ErrorCode.DEVICE_NOT_FOUND
    status = manager.status()
    assert status.connected is True
    assert status.address == "192.0.2.10"
    assert manager.generation == 1
    assert live.is_open is True  # 旧トランスポートはcloseされていない
    assert driver.identify().model == "MHO98"  # 既存driverで操作を継続できる


def test_failed_connect_closes_the_new_transport(
    manager: ConnectionManager, factory: RecordingFactory
) -> None:
    manager.connect(address="192.0.2.10")
    live = factory.transports[0]
    doomed = _ExplodingTransport(factory.scope)

    manager._transport_factory = lambda *args: doomed  # type: ignore[attr-defined]

    with pytest.raises(ScopeError) as excinfo:
        manager.connect(address="192.0.2.99")

    assert excinfo.value.code == ErrorCode.TIMEOUT
    assert doomed.is_open is False  # 開きかけた新トランスポートは後始末される
    assert live.is_open is True
    assert manager.status().address == "192.0.2.10"
    assert manager.generation == 1


def test_failed_connect_while_disconnected_still_errors(
    manager: ConnectionManager,
) -> None:
    def failing(*args: object) -> FakeTransport:
        raise ScopeError(ErrorCode.DEVICE_NOT_FOUND, "接続できません")

    manager._transport_factory = failing  # type: ignore[attr-defined]

    with pytest.raises(ScopeError) as excinfo:
        manager.connect(address="192.0.2.99")

    assert excinfo.value.code == ErrorCode.DEVICE_NOT_FOUND
    assert manager.status().connected is False
    assert manager.generation == 0


# --------------------------------------------------------------------------
# status / disconnect
# --------------------------------------------------------------------------


def test_status_when_never_connected(manager: ConnectionManager) -> None:
    status = manager.status()

    assert status.connected is False
    assert status.address is None
    assert status.idn is None
    assert status.profile_name is None
    assert status.unsupported_vendor is False
    assert manager.generation == 0


def test_status_after_connect(manager: ConnectionManager) -> None:
    manager.connect(address="192.0.2.10")

    assert manager.status().connected is True


def test_disconnect_is_idempotent(
    manager: ConnectionManager, factory: RecordingFactory
) -> None:
    manager.connect(address="192.0.2.10")

    manager.disconnect()
    manager.disconnect()

    assert factory.transports[0].is_open is False
    assert manager.status().connected is False


def test_disconnect_before_connect_is_safe(manager: ConnectionManager) -> None:
    manager.disconnect()

    assert manager.status().connected is False


# --------------------------------------------------------------------------
# require_scope
# --------------------------------------------------------------------------


def test_require_scope_when_disconnected(manager: ConnectionManager) -> None:
    with pytest.raises(ScopeError) as excinfo:
        manager.require_scope()

    error = excinfo.value
    assert error.code == ErrorCode.DEVICE_DISCONNECTED
    assert "未接続" in error.message
    assert "connect" in error.message


def test_require_scope_returns_driver(manager: ConnectionManager) -> None:
    manager.connect(address="192.0.2.10")

    assert isinstance(manager.require_scope(), ScopeDriver)


def test_require_scope_reconnects_a_dropped_link(
    manager: ConnectionManager, factory: RecordingFactory
) -> None:
    manager.connect(address="192.0.2.10")
    factory.transports[0].close()  # 機器側から切断された状況

    driver = manager.require_scope()

    assert isinstance(driver, ScopeDriver)
    assert factory.calls[1] == ("lan", "192.0.2.10", 5555, 5.0)
    assert factory.transports[1].is_open is True


def test_require_scope_reports_disconnected_when_reconnect_fails(
    factory: RecordingFactory,
) -> None:
    manager = ConnectionManager(Config(), transport_factory=factory)
    manager.connect(address="192.0.2.10")
    factory.transports[0].close()

    def failing(*args: object) -> FakeTransport:
        raise ScopeError(ErrorCode.DEVICE_NOT_FOUND, "接続できません")

    manager._transport_factory = failing  # type: ignore[attr-defined]

    with pytest.raises(ScopeError) as excinfo:
        manager.require_scope()

    assert excinfo.value.code == ErrorCode.DEVICE_DISCONNECTED


def test_require_scope_after_disconnect(manager: ConnectionManager) -> None:
    manager.connect(address="192.0.2.10")
    manager.disconnect()

    with pytest.raises(ScopeError) as excinfo:
        manager.require_scope()

    assert excinfo.value.code == ErrorCode.DEVICE_DISCONNECTED


# --------------------------------------------------------------------------
# 排他制御
# --------------------------------------------------------------------------


def test_lock_is_reentrant(manager: ConnectionManager) -> None:
    """server層が全Tool呼び出しを直列化するための公開ロック。"""
    assert isinstance(manager.lock, type(threading.RLock()))

    with manager.lock:
        with manager.lock:
            assert manager.status().connected is False


# --------------------------------------------------------------------------
# 監査ログ(Requirements.md 7.6)
# --------------------------------------------------------------------------


@pytest.fixture
def audit_path(tmp_path: Path) -> Path:
    return tmp_path / "audit.jsonl"


@pytest.fixture
def audited(factory: RecordingFactory, audit_path: Path) -> ConnectionManager:
    return ConnectionManager(
        Config(), transport_factory=factory, audit=AuditLogger(audit_path)
    )


def read_audit(path: Path) -> list[dict]:
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]


def test_connect_is_audited(audited: ConnectionManager, audit_path: Path) -> None:
    audited.connect(address="192.0.2.10")

    entries = read_audit(audit_path)
    assert len(entries) == 1
    entry = entries[0]
    assert entry["tool"] == "connect"
    assert entry["result"] == "success"
    assert entry["requested"] == {
        "address": "192.0.2.10",
        "transport": "lan",
        "port": 5555,
    }
    assert entry["after"]["profile_name"] == "mho98"
    assert entry["after"]["profile_confidence"] == "verified"
    assert entry["after"]["unsupported_vendor"] is False
    assert entry["after"]["idn"]["model"] == "MHO98"
    assert entry["detail"]["reconnect"] is False


def test_failed_connect_is_audited_as_error(
    audited: ConnectionManager, audit_path: Path
) -> None:
    audited.connect(address="192.0.2.10")

    def failing(*args: object) -> FakeTransport:
        raise ScopeError(ErrorCode.DEVICE_NOT_FOUND, "接続できません")

    audited._transport_factory = failing  # type: ignore[attr-defined]

    with pytest.raises(ScopeError):
        audited.connect(address="192.0.2.99")

    entries = read_audit(audit_path)
    assert len(entries) == 2
    assert entries[1]["tool"] == "connect"
    assert entries[1]["result"] == "error"
    assert entries[1]["detail"]["error"]["code"] == ErrorCode.DEVICE_NOT_FOUND
    # F3: 失敗しても既存接続は保持される
    assert audited.status().address == "192.0.2.10"


def test_auto_reconnect_is_audited_with_reconnect_flag(
    audited: ConnectionManager, factory: RecordingFactory, audit_path: Path
) -> None:
    audited.connect(address="192.0.2.10")
    factory.transports[0].close()

    audited.require_scope()

    entries = read_audit(audit_path)
    connects = [e for e in entries if e["tool"] == "connect"]
    assert len(connects) == 2
    assert connects[0]["detail"]["reconnect"] is False
    assert connects[1]["detail"]["reconnect"] is True


def test_disconnect_is_audited_only_when_a_link_is_closed(
    audited: ConnectionManager, audit_path: Path
) -> None:
    audited.connect(address="192.0.2.10")

    audited.disconnect()
    audited.disconnect()  # 冪等呼び出しは記録しない

    entries = [e for e in read_audit(audit_path) if e["tool"] == "disconnect"]
    assert len(entries) == 1
    assert entries[0]["result"] == "success"
    assert entries[0]["requested"]["address"] == "192.0.2.10"


def test_disconnect_before_connect_records_nothing(
    audited: ConnectionManager, audit_path: Path
) -> None:
    audited.disconnect()

    assert read_audit(audit_path) == []


def test_audit_defaults_to_disabled_logger(manager: ConnectionManager) -> None:
    """audit 未注入でも動作する(既定は no-op ロガー)。"""
    manager.connect(address="192.0.2.10")
    manager.disconnect()

    assert manager.status().connected is False


# --------------------------------------------------------------------------
# 既定ファクトリ
# --------------------------------------------------------------------------


def test_default_factory_builds_usb_transport() -> None:
    """USBは遅延importの `UsbTransport`(接続はopen時、ここでは生成のみ)。"""
    link = _default_transport_factory(
        "usb", "USB0::0x1AB1::0x044C::MHO0001::INSTR", 5555, 5.0
    )

    assert isinstance(link, UsbTransport)
    assert link.is_open is False


def test_default_factory_rejects_unknown_transport() -> None:
    with pytest.raises(ScopeError) as excinfo:
        _default_transport_factory("serial", "COM3", 5555, 5.0)

    assert excinfo.value.code == ErrorCode.INVALID_PARAMETER
