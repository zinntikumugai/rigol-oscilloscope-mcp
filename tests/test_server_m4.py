"""server.py のテスト(測定の前提設定と統計 / Phase M4)。

MCP SDK の in-memory 接続で往復させ、クライアントから見えるJSONを固定する。
値変換そのものの退行ガードは tests/test_scope_driver.py と
tests/test_control_service.py が持ち、本ファイルは Tool 層までの疎通に絞る。

ハーネスは Phase 1 / 2 / 4 / M1 / M2 / M3 と重複するが、テスト同士の import
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

M4_TOOLS = {"configure_measurement", "get_measurement_statistics"}


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


def test_list_tools_exposes_the_m4_tools(server) -> None:
    assert M4_TOOLS <= set(descriptions(server))


def test_configure_measurement_documents_the_unchanged_policy(server) -> None:
    description = descriptions(server)["configure_measurement"]

    assert "Omitted items are left unchanged." in description
    # 取り込み条件に触れないことを明記する(SAFE_WRITE の根拠)
    assert "does not change acquisition" in description


def test_configure_measurement_warns_about_zoom_prerequisite(server) -> None:
    """ZOOM は遅延掃引を先に有効化しないと機器が拒否する(ガイド3.17.19)。"""
    description = descriptions(server)["configure_measurement"]

    assert "delayed sweep" in description


def test_get_measurement_statistics_points_at_the_enable_step(server) -> None:
    """統計は項目ごとの有効化が前提。説明文で必ず案内する。"""
    description = descriptions(server)["get_measurement_statistics"]

    assert "statistics_items" in description


# --------------------------------------------------------------------------
# 往復
# --------------------------------------------------------------------------


def test_configure_measurement_returns_requested_applied_changed(server) -> None:
    connected(server)

    result = data(server, "configure_measurement", {"area": "cursor", "region_ax_s": -1e-5})

    assert result["requested"] == {"area": "cursor", "region_ax_s": -1e-5}
    assert result["applied"]["area"] == "cursor"
    assert result["changed"] is True


def test_configure_measurement_without_items_is_rejected(server) -> None:
    connected(server)

    result = data(server, "configure_measurement", {})

    assert result["error"] is True
    assert result["code"] == ErrorCode.INVALID_PARAMETER


def test_get_measurement_statistics_round_trip(server) -> None:
    connected(server)

    result = data(
        server,
        "get_measurement_statistics",
        {"channel": "CH1", "measurements": ["vpp"], "types": ["maximum", "deviation"]},
    )

    assert result["channel"] == "CH1"
    assert set(result["statistics"]["vpp"]) == {"maximum", "deviation"}


def test_get_measurement_statistics_needs_channel_b_for_delay(server) -> None:
    connected(server)

    result = data(
        server,
        "get_measurement_statistics",
        {"channel": "CH1", "measurements": ["delay_rise_rise"]},
    )

    assert result["error"] is True
    assert result["code"] == ErrorCode.INVALID_PARAMETER


def test_configure_measurement_is_recorded_in_the_audit_log(
    server, config: Config
) -> None:
    connected(server)
    data(server, "configure_measurement", {"statistics_count": 500})

    entries = [
        json.loads(line)
        for line in config.audit_log.read_text(encoding="utf-8").splitlines()
    ]
    tools = [entry["tool"] for entry in entries]

    assert "configure_measurement" in tools


# 方言未宣言の機種で送信ゼロの UNSUPPORTED_FEATURE になることは
# tests/test_scope_driver.py::test_configure_measurement_unsupported_on_generic が
# 担保する(FakeScope の `*IDN?` はモジュール定数で機種を差し替えられないため、
# ここでは重複させない)。
