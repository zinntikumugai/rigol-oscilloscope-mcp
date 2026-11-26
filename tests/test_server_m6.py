"""server.py のテスト(取得モード / Phase M6・issue #52)。

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

M6_TOOLS = {"configure_acquisition"}


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


def test_list_tools_exposes_configure_acquisition(server) -> None:
    assert M6_TOOLS <= set(descriptions(server))


def test_get_acquisition_state_documents_the_returned_settings(server) -> None:
    description = descriptions(server)["get_acquisition_state"]

    for key in ("mode", "averages", "adc_bits", "memory_depth"):
        assert key in description


def test_configure_acquisition_documents_modes_and_constraints(server) -> None:
    description = descriptions(server)["configure_acquisition"]

    assert "Omitted items are left unchanged." in description
    for mode in ("normal", "peak", "average", "high_resolution"):
        assert mode in description
    # 実機実測の制約2点(平均回数は2のべき乗のみ / BITSはHRESモードのみ)
    assert "power of 2" in description
    assert 'mode="high_resolution"' in description


# --------------------------------------------------------------------------
# 往復
# --------------------------------------------------------------------------


def test_configure_acquisition_returns_requested_applied_changed(server) -> None:
    connected(server)

    result = data(server, "configure_acquisition", {"mode": "average", "averages": 128})

    assert result["requested"] == {"mode": "average", "averages": 128}
    assert result["applied"] == {"mode": "average", "averages": 128}
    assert result["changed"] is True


def test_configure_acquisition_without_items_is_rejected(server) -> None:
    connected(server)

    result = data(server, "configure_acquisition", {})

    assert result["error"] is True
    assert result["code"] == ErrorCode.INVALID_PARAMETER


def test_get_acquisition_state_includes_the_mode(server) -> None:
    connected(server)
    data(server, "configure_acquisition", {"mode": "peak"})

    result = data(server, "get_acquisition_state")

    assert result["mode"] == "peak"
    assert result["averages"] == 2
    assert result["adc_bits"] == 14
    assert result["memory_depth"] == 10_000
    assert "running" in result


def test_get_acquisition_state_on_generic_profile_keeps_the_mvp_shape(
    server, manager: ConnectionManager, scope: FakeScope
) -> None:
    """方言未宣言の機種では :ACQuire の設定クエリを1件も送らない。"""
    connected(server)
    manager.require_scope().profile = load_profile("rigol-generic")
    scope.command_log.clear()

    result = data(server, "get_acquisition_state")

    assert set(result) == {"trigger_status", "running"}
    assert not [c for c in scope.command_log if c.upper().startswith(":ACQ")]
