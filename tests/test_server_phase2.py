"""server.py のテスト(Phase 2: 書き込み系Tool群 / tools.md 3章・4章)。

Phase 1 と同じく MCP SDK の in-memory 接続で往復させ、クライアントから見える
JSON を固定する。本ファイルの主眼は Tool 層まで通した**確認フロー**
(Requirements.md 6.2)と**監査ログ**(7.6)の疎通で、値変換そのものの退行ガードは
tests/test_control_service.py が持つ。

ハーネスは Phase 1 と重複するが、テスト同士の import 依存を作らないため自前に持つ。
"""

import json
from pathlib import Path

import anyio
import pytest
from mcp.shared.memory import create_connected_server_and_client_session
from mcp.types import CallToolResult, TextContent

from rigol_oscilloscope_mcp.config import Config
from rigol_oscilloscope_mcp.errors import ErrorCode
from rigol_oscilloscope_mcp.server import create_server
from rigol_oscilloscope_mcp.service import ConnectionManager
from rigol_oscilloscope_mcp.testing import FakeScope, FakeTransport

ADDRESS = "192.0.2.10"  # TEST-NET-1(実接続は FakeTransport が肩代わりする)

PHASE1_TOOLS = {
    "connect",
    "disconnect",
    "scope_identify",
    "get_capabilities",
    "get_state",
    "get_channel",
    "get_timebase",
    "get_trigger",
    "get_acquisition_state",
    "measure",
    "capture_waveform",
    "capture_screenshot",
}

PHASE2_TOOLS = {
    "configure_channel",
    "configure_timebase",
    "configure_trigger",
    "run",
    "stop",
    "single",
    "autoset",
}

PHASE4_TOOLS = {
    "analyze_waveform",
    "clear_measurements",
}


# --------------------------------------------------------------------------
# ハーネス
# --------------------------------------------------------------------------


@pytest.fixture
def audit_path(tmp_path: Path) -> Path:
    return tmp_path / "audit.jsonl"


@pytest.fixture
def config(tmp_path: Path, audit_path: Path) -> Config:
    root = tmp_path.resolve()
    return Config(screenshot_dir=root, allowed_dirs=(root,), audit_log=audit_path)


@pytest.fixture
def scope() -> FakeScope:
    return FakeScope()


@pytest.fixture
def manager(config: Config, scope: FakeScope) -> ConnectionManager:
    def factory(transport: str, address: str, port: int, timeout_s: float) -> FakeTransport:
        return FakeTransport(scope)

    return ConnectionManager(config, transport_factory=factory)


@pytest.fixture
def server(config: Config, manager: ConnectionManager):
    return create_server(config=config, connection_manager=manager)


def run_calls(server, *calls: tuple[str, dict]) -> list[CallToolResult]:
    """`(tool名, 引数)` の並びを1セッションで順に呼び、結果を返す。"""

    async def main() -> list[CallToolResult]:
        async with create_connected_server_and_client_session(server) as client:
            return [await client.call_tool(name, args) for name, args in calls]

    return anyio.run(main)


def call(server, name: str, args: dict | None = None) -> CallToolResult:
    return run_calls(server, (name, args or {}))[0]


def payload(result: CallToolResult) -> dict:
    """最初の text content をJSONとして読む(LLMが見るのと同じ形)。"""
    first = result.content[0]
    assert isinstance(first, TextContent), first
    return json.loads(first.text)


def data(server, name: str, args: dict | None = None) -> dict:
    return payload(call(server, name, args))


def connected(server) -> None:
    assert data(server, "connect", {"address": ADDRESS})["connected"] is True


def audit_lines(path: Path) -> list[dict]:
    if not path.exists():
        return []
    return [
        json.loads(line)
        for line in path.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]


# --------------------------------------------------------------------------
# Tool登録
# --------------------------------------------------------------------------


def test_list_tools_exposes_every_phase(server) -> None:
    async def main() -> list[str]:
        async with create_connected_server_and_client_session(server) as client:
            return [tool.name for tool in (await client.list_tools()).tools]

    names = anyio.run(main)

    # 総数は tests/test_server_startup.py が持つ(フェーズ追加のたびに触らない)
    assert PHASE1_TOOLS | PHASE2_TOOLS | PHASE4_TOOLS <= set(names)


def test_write_tool_descriptions_warn_about_confirmation(server) -> None:
    async def main() -> dict[str, str]:
        async with create_connected_server_and_client_session(server) as client:
            return {t.name: t.description or "" for t in (await client.list_tools()).tools}

    descriptions = anyio.run(main)

    assert "50" in descriptions["configure_channel"]
    assert "confirm" in descriptions["configure_channel"]
    assert "confirm" in descriptions["autoset"]


# --------------------------------------------------------------------------
# 設定変更
# --------------------------------------------------------------------------


def test_configure_channel_returns_requested_and_applied(server) -> None:
    connected(server)

    result = data(server, "configure_channel", {"channel": "CH1", "scale_v_per_div": 2.0})

    assert result["channel"] == "CH1"
    assert result["requested"] == {"scale_v_per_div": 2.0}
    assert result["applied"]["scale_v_per_div"] == pytest.approx(2.0)


def test_configure_channel_without_any_setting_is_invalid(server) -> None:
    connected(server)

    result = data(server, "configure_channel", {"channel": "CH1"})

    assert result["error"] is True
    assert result["code"] == ErrorCode.INVALID_PARAMETER


def test_configure_channel_while_disconnected(server) -> None:
    result = data(server, "configure_channel", {"channel": "CH1", "enabled": True})

    assert result["error"] is True
    assert result["code"] == ErrorCode.DEVICE_DISCONNECTED


def test_configure_timebase(server) -> None:
    connected(server)

    result = data(server, "configure_timebase", {"scale_s_per_div": 1e-3})

    assert result["requested"] == {"scale_s_per_div": 1e-3}
    assert result["applied"]["scale_s_per_div"] == pytest.approx(1e-3)


def test_configure_timebase_without_any_setting_is_invalid(server) -> None:
    connected(server)

    result = data(server, "configure_timebase")

    assert result["error"] is True
    assert result["code"] == ErrorCode.INVALID_PARAMETER


def test_configure_trigger(server) -> None:
    connected(server)

    result = data(server, "configure_trigger", {"level_v": 1.5, "slope": "rising"})

    assert result["applied"]["level_v"] == pytest.approx(1.5)
    assert result["trigger"]["level_v"] == pytest.approx(1.5)
    assert result["trigger"]["slope"] == "rising"


# --------------------------------------------------------------------------
# 50Ω(RESTRICTED_WRITE)の確認フロー
# --------------------------------------------------------------------------


def test_50ohm_requires_confirmation_then_succeeds(server) -> None:
    connected(server)

    first = data(server, "configure_channel", {"channel": "CH1", "impedance": "50"})

    assert first["error"] is True
    assert first["code"] == ErrorCode.USER_CONFIRMATION_REQUIRED
    token = first["detail"]["confirm_token"]
    assert token
    assert "human" in first["detail"]["instruction"]
    assert first["detail"]["expires_in_s"] > 0
    # 承認前に機器へ書き込んでいないこと
    assert data(server, "get_channel", {"channel": "CH1"})["impedance"] == "1M"

    second = data(
        server,
        "configure_channel",
        {"channel": "CH1", "impedance": "50", "confirm_token": token},
    )

    assert second.get("error") is None
    assert second["applied"]["impedance"] == "50"
    assert data(server, "get_channel", {"channel": "CH1"})["impedance"] == "50"


def test_confirm_token_is_bound_to_the_arguments(server) -> None:
    connected(server)

    issued = data(server, "configure_channel", {"channel": "CH1", "impedance": "50"})
    token = issued["detail"]["confirm_token"]

    reused = data(
        server,
        "configure_channel",
        {
            "channel": "CH1",
            "impedance": "50",
            "scale_v_per_div": 0.5,  # 発行時と引数が異なる
            "confirm_token": token,
        },
    )

    assert reused["error"] is True
    assert reused["code"] == ErrorCode.USER_CONFIRMATION_REQUIRED
    assert reused["detail"]["reason"] == "args_mismatch"


# --------------------------------------------------------------------------
# Acquisition
# --------------------------------------------------------------------------


def test_run_stop_single(server) -> None:
    connected(server)

    started, stopped, state, shot = run_calls(
        server, ("run", {}), ("stop", {}), ("get_acquisition_state", {}), ("single", {})
    )

    assert payload(started)["result"] == "ok"
    assert payload(stopped)["result"] == "ok"
    assert payload(state)["running"] is False
    assert payload(shot)["result"] == "ok"


def test_clear_measurements_roundtrip(server, scope) -> None:
    """measureで蓄積したResultビュー項目をclear_measurementsで消す(issue #16)。"""
    connected(server)

    measured, cleared = run_calls(
        server,
        ("measure", {"channel": "CH1", "measurements": ["vpp"]}),
        ("clear_measurements", {}),
    )

    assert "vpp_v" in payload(measured)["values"]
    assert payload(cleared) == {"result": "ok"}
    assert scope.measurement_items == []


def test_clear_measurements_requires_connection(server) -> None:
    result = data(server, "clear_measurements")

    assert result["error"] is True


def test_autoset_requires_confirmation_then_returns_state(server) -> None:
    connected(server)

    first = data(server, "autoset")

    assert first["error"] is True
    assert first["code"] == ErrorCode.USER_CONFIRMATION_REQUIRED
    token = first["detail"]["confirm_token"]
    assert "human" in first["detail"]["instruction"]

    second = data(server, "autoset", {"confirm_token": token})

    assert second["result"] == "ok"
    assert "Auto Setup" in second["note"]
    assert set(second["state"]) == {"channels", "timebase", "trigger"}


# --------------------------------------------------------------------------
# 監査ログ(Requirements.md 7.6)
# --------------------------------------------------------------------------


def test_write_operations_are_audited(server, audit_path: Path) -> None:
    connected(server)

    before = len(audit_lines(audit_path))
    data(server, "configure_timebase", {"scale_s_per_div": 1e-3})
    after_first = audit_lines(audit_path)
    data(server, "stop")
    after_second = audit_lines(audit_path)

    assert audit_path.exists()
    assert len(after_first) > before
    assert len(after_second) > len(after_first)
    assert [row["tool"] for row in after_second[-2:]] == ["configure_timebase", "stop"]
    assert after_second[-1]["result"] == "success"
