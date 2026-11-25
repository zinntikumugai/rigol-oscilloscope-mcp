"""server.py のテスト(イベント検索 / Phase M7・issue #56)。

MCP SDK の in-memory 接続で往復させ、クライアントから見えるJSONを固定する。
値変換そのものの退行ガードは tests/test_scope_driver.py と
tests/test_control_service.py が持ち、本ファイルは Tool 層までの疎通に絞る。

ハーネスは 既存の test_server_*.py と重複するが、テスト同士の import
依存を作らないため自前に持つ(既存ファイルと同じ方針)。
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

M7_TOOLS = {"configure_search", "get_search_results"}


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
# Tool登録・説明文
# --------------------------------------------------------------------------


def test_list_tools_exposes_the_search_tools(server) -> None:
    assert M7_TOOLS <= set(descriptions(server))


def test_get_search_results_tells_to_stop_first(server) -> None:
    description = descriptions(server)["get_search_results"]

    assert "stop" in description
    assert "seconds" in description


# --------------------------------------------------------------------------
# 往復
# --------------------------------------------------------------------------


def test_search_round_trip(server, scope: FakeScope) -> None:
    connected(server)
    scope.search_events = [-0.001, 0.0, 0.001]

    configured = data(
        server,
        "configure_search",
        {"enabled": True, "mode": "edge", "settings": {"source": "CH1", "threshold_v": 0.2}},
    )
    data(server, "stop")
    results = data(server, "get_search_results", {"max_events": 2})

    assert configured["applied"]["enabled"] is True
    assert configured["search"]["mode"] == "edge"
    assert results["count"] == 3
    assert results["events_s"] == [-0.001, 0.0]
    assert results["truncated"] is True
    assert results["search"]["enabled"] is True


def test_get_search_results_while_running_is_an_error(server, scope: FakeScope) -> None:
    connected(server)
    scope.search_events = [0.0]
    data(server, "configure_search", {"enabled": True})

    result = data(server, "get_search_results")

    assert result["error"] is True
    assert result["code"] == ErrorCode.SAFETY_POLICY_DENIED
