"""server.py のテスト(トリガ種別の開放 / Phase M5)。

MCP SDK の in-memory 接続で往復させ、クライアントから見えるJSONを固定する。
値変換そのものの退行ガードは tests/test_scope_driver.py と
tests/test_control_service.py が持ち、本ファイルは Tool 層までの疎通に絞る。

ハーネスは他Phaseと重複するが、テスト同士の import 依存を作らないため自前に
持つ(既存ファイルと同じ方針)。
"""

import json
from pathlib import Path

import anyio
import pytest
from mcp.shared.memory import create_connected_server_and_client_session
from mcp.types import CallToolResult, TextContent

from rigol_oscilloscope_mcp.config import Config
from rigol_oscilloscope_mcp.errors import ErrorCode
from rigol_oscilloscope_mcp.profiles import load_profile
from rigol_oscilloscope_mcp.server import create_server
from rigol_oscilloscope_mcp.service import ConnectionManager
from rigol_oscilloscope_mcp.testing import FakeScope, FakeTransport

ADDRESS = "192.0.2.10"  # TEST-NET-1(実接続は FakeTransport が肩代わりする)

TRIGGER_TYPES = (
    "edge",
    "pulse",
    "slope",
    "pattern",
    "duration",
    "timeout",
    "runt",
    "window",
    "delay",
    "setup_hold",
    "nth_edge",
)


# --------------------------------------------------------------------------
# ハーネス
# --------------------------------------------------------------------------


@pytest.fixture
def config(tmp_path: Path) -> Config:
    root = tmp_path.resolve()
    return Config(
        screenshot_dir=root, allowed_dirs=(root,), audit_log=tmp_path / "audit.jsonl"
    )


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
    async def main() -> list[CallToolResult]:
        async with create_connected_server_and_client_session(server) as client:
            return [await client.call_tool(name, args) for name, args in calls]

    return anyio.run(main)


def payload(result: CallToolResult) -> dict:
    first = result.content[0]
    assert isinstance(first, TextContent), first
    return json.loads(first.text)


def data(server, name: str, args: dict | None = None) -> dict:
    return payload(run_calls(server, (name, args or {}))[0])


def connected(server) -> None:
    assert data(server, "connect", {"address": ADDRESS})["connected"] is True


def descriptions(server) -> dict[str, str]:
    async def main() -> dict[str, str]:
        async with create_connected_server_and_client_session(server) as client:
            return {t.name: t.description or "" for t in (await client.list_tools()).tools}

    return anyio.run(main)


# --------------------------------------------------------------------------
# 説明文(LLMが種別を選ぶ唯一の経路)
# --------------------------------------------------------------------------


def test_configure_trigger_lists_every_supported_type(server) -> None:
    description = descriptions(server)["configure_trigger"]
    missing = [t for t in TRIGGER_TYPES if t not in description]

    assert not missing, f"configure_trigger の description に無いトリガ種別: {missing}"


def test_configure_trigger_documents_the_implicit_type(server) -> None:
    """`type` 省略時に「今のトリガへ書く」ことを明示する(挙動が変わったため)。"""
    description = descriptions(server)["configure_trigger"]

    assert "Omit type" in description


# --------------------------------------------------------------------------
# 往復
# --------------------------------------------------------------------------


def test_configure_trigger_switches_type_and_reports_settings(server) -> None:
    connected(server)

    result = data(
        server,
        "configure_trigger",
        {"type": "pulse", "settings": {"when": "less", "upper_width_s": 1e-7}},
    )

    assert result["applied"]["type"] == "pulse"
    assert result["applied"]["when"] == "less"
    assert result["trigger"]["type"] == "pulse"
    assert result["trigger"]["settings"]["upper_width_s"] == pytest.approx(1e-7)


def test_configure_trigger_rejects_settings_from_another_type(server) -> None:
    connected(server)

    result = data(
        server,
        "configure_trigger",
        {"type": "edge", "settings": {"upper_width_s": 1e-7}},
    )

    assert result["error"] is True
    assert result["code"] == ErrorCode.INVALID_PARAMETER


def test_configure_trigger_without_items_is_rejected(server) -> None:
    connected(server)

    result = data(server, "configure_trigger", {})

    assert result["error"] is True
    assert result["code"] == ErrorCode.INVALID_PARAMETER


def test_get_trigger_reports_the_active_subtree(server) -> None:
    connected(server)
    data(server, "configure_trigger", {"type": "timeout", "settings": {"time_s": 3e-6}})

    state = data(server, "get_trigger")

    assert state["type"] == "timeout"
    assert state["settings"]["time_s"] == pytest.approx(3e-6)


def test_legacy_edge_call_still_works(server) -> None:
    """既存の呼び出し(source / level_v / slope)は種別を跨がずそのまま通る。"""
    connected(server)

    result = data(
        server,
        "configure_trigger",
        {"source": "CH2", "level_v": 0.5, "slope": "falling"},
    )

    assert result["applied"] == {
        "source": "CH2",
        "level_v": pytest.approx(0.5),
        "slope": "falling",
    }
