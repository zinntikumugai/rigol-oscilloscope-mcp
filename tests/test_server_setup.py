"""server.py のテスト(設定の一括保存・復元 / issue #72)。"""

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
def server(config: Config, scope: FakeScope):
    def factory(transport: str, address: str, port: int, timeout_s: float) -> FakeTransport:
        return FakeTransport(scope)

    manager = ConnectionManager(config, transport_factory=factory)
    return create_server(config=config, connection_manager=manager)


def run_calls(server, *calls: tuple[str, dict]) -> list[CallToolResult]:
    async def main() -> list[CallToolResult]:
        async with create_connected_server_and_client_session(server) as client:
            return [await client.call_tool(name, args) for name, args in calls]

    return anyio.run(main)


def data(server, name: str, args: dict | None = None) -> dict:
    first = run_calls(server, (name, args or {}))[0].content[0]
    assert isinstance(first, TextContent), first
    return json.loads(first.text)


def connected(server) -> None:
    assert data(server, "connect", {"address": ADDRESS})["connected"] is True


def test_save_then_restore_with_confirmation(server, scope: FakeScope, tmp_path: Path) -> None:
    connected(server)
    saved = data(server, "save_setup", {"path": "bench"})
    assert saved["saved_path"].endswith("bench.stp")
    assert saved["size_bytes"] > 4
    data(server, "configure_timebase", {"scale_s_per_div": 1e-5})

    asked = data(server, "restore_setup", {"path": "bench.stp"})
    assert asked["code"] == ErrorCode.USER_CONFIRMATION_REQUIRED
    assert not [c for c in scope.command_log if c.startswith(":SYSTem:SETup #")]

    token = asked["detail"]["confirm_token"]
    restored = data(server, "restore_setup", {"path": "bench.stp", "confirm_token": token})

    assert restored["result"] == "ok"
    assert restored["timebase"]["scale_s_per_div"] == pytest.approx(2e-4)


def test_restore_setup_outside_the_allowed_roots_is_rejected(server) -> None:
    connected(server)

    result = data(server, "restore_setup", {"path": "/etc/passwd"})

    assert result["error"] is True
    assert result["code"] == ErrorCode.INVALID_PARAMETER


def test_restore_token_is_bound_to_the_file_contents(server, scope: FakeScope) -> None:
    """承認後にファイルが差し替わったら、その承認では復元しない(Copilotレビュー)。"""
    connected(server)
    saved = data(server, "save_setup", {"path": "bench"})
    asked = data(server, "restore_setup", {"path": "bench.stp"})
    token = asked["detail"]["confirm_token"]

    # 承認を求めた後で、別の(正しい形の)設定ファイルに差し替える
    data(server, "configure_timebase", {"scale_s_per_div": 1e-3})
    data(server, "save_setup", {"path": "bench"})

    result = data(server, "restore_setup", {"path": "bench.stp", "confirm_token": token})

    assert result["error"] is True
    assert not [c for c in scope.command_log if c.startswith(":SYSTem:SETup #")]
    assert saved["saved_path"].endswith("bench.stp")


def test_default_setup_names_do_not_collide_within_a_second(server) -> None:
    connected(server)

    first = data(server, "save_setup")["saved_path"]
    second = data(server, "save_setup")["saved_path"]

    assert first != second
