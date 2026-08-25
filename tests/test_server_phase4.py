"""server.py のテスト(Phase 4: シリアルデコード / 信号発生 / tools.md 6章・7章)。

MCP SDK の in-memory 接続で往復させ、クライアントから見えるJSONを固定する。
値変換そのものの退行ガードは tests/test_scope_driver.py と
tests/test_control_service.py が持ち、本ファイルは Tool 層までの疎通に絞る。

ハーネスは Phase 1 / 2 と重複するが、テスト同士の import 依存を作らないため
自前に持つ(既存2ファイルと同じ方針)。
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

PHASE4_TOOLS = {
    "configure_decode",
    "get_decode_result",
    "configure_afg",
    "get_afg_state",
}


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


# --------------------------------------------------------------------------
# Tool登録
# --------------------------------------------------------------------------


def test_list_tools_exposes_phase4(server) -> None:
    async def main() -> list[str]:
        async with create_connected_server_and_client_session(server) as client:
            return [tool.name for tool in (await client.list_tools()).tools]

    names = anyio.run(main)

    assert PHASE4_TOOLS <= set(names)


def test_configure_decode_description_lists_protocols(server) -> None:
    async def main() -> dict[str, str]:
        async with create_connected_server_and_client_session(server) as client:
            return {t.name: t.description or "" for t in (await client.list_tools()).tools}

    description = anyio.run(main)["configure_decode"]

    for protocol in ("uart", "i2c", "spi", "can", "lin", "parallel"):
        assert protocol in description
    assert "event_table" in description


# --------------------------------------------------------------------------
# configure_decode
# --------------------------------------------------------------------------


def test_configure_decode_end_to_end(server) -> None:
    connected(server)

    result = data(
        server,
        "configure_decode",
        {
            "protocol": "uart",
            "bus": 1,
            "enabled": True,
            "settings": {"tx_source": "CH1", "baud_bps": 115200, "parity": "none"},
        },
    )

    assert result["bus"] == 1
    assert result["applied"]["protocol"] == "uart"
    assert result["applied"]["enabled"] is True
    assert result["applied"]["settings"] == {
        "tx_source": "CH1",
        "baud_bps": 115200,
        "parity": "none",
    }
    assert result["changed"] is True


def test_configure_decode_unknown_protocol_returns_error_dict(server) -> None:
    connected(server)

    result = data(server, "configure_decode", {"protocol": "i2s"})

    assert result["error"] is True
    assert result["code"] == ErrorCode.UNSUPPORTED_FEATURE


def test_configure_decode_while_disconnected(server) -> None:
    result = data(server, "configure_decode", {"protocol": "uart"})

    assert result["error"] is True
    assert result["code"] == ErrorCode.DEVICE_DISCONNECTED


# --------------------------------------------------------------------------
# get_decode_result
# --------------------------------------------------------------------------


def test_get_decode_result_end_to_end(server) -> None:
    connected(server)
    data(
        server,
        "configure_decode",
        {"protocol": "uart", "bus": 1, "enabled": True, "event_table": True},
    )
    data(server, "stop")

    result = data(server, "get_decode_result", {"bus": 1})

    assert result["bus"] == 1
    assert result["protocol"] == "uart"
    # 列構成はプロトコル依存(実機実測のRS232ヘッダ)
    assert result["columns"] == ["time_s", "tx_rx", "data", "error"]
    assert result["event_count"] == len(result["events"])
    assert result["truncated"] is False
    assert result["warnings"] == []
    assert isinstance(result["events"][0]["time_s"], float)


def test_get_decode_result_warns_while_the_bus_is_off(server) -> None:
    connected(server)
    data(server, "configure_decode", {"protocol": "uart", "bus": 2})

    result = data(server, "get_decode_result", {"bus": 2})

    assert result["events"] == []
    assert result["event_count"] == 0
    assert any("configure_decode(bus=2, enabled=true)" in w for w in result["warnings"])


def test_get_decode_result_truncates_with_max_events(server) -> None:
    connected(server)
    data(
        server,
        "configure_decode",
        {"protocol": "uart", "bus": 1, "enabled": True, "event_table": True},
    )

    result = data(server, "get_decode_result", {"bus": 1, "max_events": 1})

    assert len(result["events"]) == 1
    assert result["event_count"] == 2
    assert result["truncated"] is True
    assert any("max_events" in w for w in result["warnings"])


def test_get_decode_result_rejects_max_events_zero(server) -> None:
    connected(server)

    result = data(server, "get_decode_result", {"max_events": 0})

    assert result["error"] is True
    assert result["code"] == ErrorCode.INVALID_PARAMETER


def test_get_decode_result_while_disconnected(server) -> None:
    result = data(server, "get_decode_result", {})

    assert result["error"] is True
    assert result["code"] == ErrorCode.DEVICE_DISCONNECTED


# --------------------------------------------------------------------------
# 信号発生(configure_afg / get_afg_state)
# --------------------------------------------------------------------------


def descriptions(server) -> dict[str, str]:
    async def main() -> dict[str, str]:
        async with create_connected_server_and_client_session(server) as client:
            return {t.name: t.description or "" for t in (await client.list_tools()).tools}

    return anyio.run(main)


def test_configure_afg_description_states_the_output_is_untouched(server) -> None:
    """LLMが「設定=出力ON」と誤解しないことが最重要(出力制御は別Tool)。"""
    description = descriptions(server)["configure_afg"].lower()

    assert "output" in description
    assert "peak-to-peak" in description
    for waveform in ("sine", "square", "ramp", "noise", "dc"):
        assert waveform in description


def test_configure_afg_end_to_end(server) -> None:
    connected(server)

    result = data(
        server,
        "configure_afg",
        {
            "channel": 1,
            "waveform": "square",
            "frequency_hz": 2000.0,
            "amplitude_vpp": 1.0,
            "duty_percent": 60.0,
        },
    )

    assert result["channel"] == 1
    assert result["applied"] == {
        "channel": 1,
        "waveform": "square",
        "frequency_hz": 2000.0,
        "amplitude_vpp": 1.0,
        "duty_percent": 60.0,
    }
    assert result["changed"] is True


def test_configure_afg_leaves_the_output_off(server, scope: FakeScope) -> None:
    connected(server)

    data(server, "configure_afg", {"waveform": "square", "amplitude_vpp": 1.0})

    assert data(server, "get_afg_state", {"channel": 1})["output"] is False
    assert [c for c in scope.command_log if "OUTP" in c.upper() and "?" not in c] == []


def test_get_afg_state_single_channel(server) -> None:
    connected(server)

    state = data(server, "get_afg_state", {"channel": 2})

    assert state["channel"] == 2
    assert state["output"] is False
    assert state["waveform"] == "sine"


def test_get_afg_state_all_channels(server) -> None:
    connected(server)

    state = data(server, "get_afg_state")

    # チャンネル番号は文字列キー(JSONオブジェクトのキーは文字列)
    assert set(state["channels"]) == {"1", "2"}
    assert state["channels"]["2"]["channel"] == 2
    assert state["channels"]["1"]["waveform"] == "sine"


def test_configure_afg_unknown_waveform_returns_error_dict(server) -> None:
    connected(server)

    result = data(server, "configure_afg", {"waveform": "pulse"})

    assert result["error"] is True
    assert result["code"] == ErrorCode.INVALID_PARAMETER


def test_configure_afg_while_disconnected(server) -> None:
    result = data(server, "configure_afg", {"waveform": "sine"})

    assert result["error"] is True
    assert result["code"] == ErrorCode.DEVICE_DISCONNECTED


def test_get_afg_state_while_disconnected(server) -> None:
    result = data(server, "get_afg_state", {})

    assert result["error"] is True
    assert result["code"] == ErrorCode.DEVICE_DISCONNECTED
