"""server.py のテスト(MATH演算 / Phase M1)。

MCP SDK の in-memory 接続で往復させ、クライアントから見えるJSONを固定する。
値変換そのものの退行ガードは tests/test_scope_driver.py と
tests/test_control_service.py が持ち、本ファイルは Tool 層までの疎通に絞る。

ハーネスは Phase 1 / 2 / 4 と重複するが、テスト同士の import 依存を作らないため
自前に持つ(既存ファイルと同じ方針)。
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

MATH_TOOLS = {"configure_math", "get_math_state"}


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
# Tool登録
# --------------------------------------------------------------------------


def test_list_tools_exposes_the_math_tools(server) -> None:
    assert MATH_TOOLS <= set(descriptions(server))


def test_configure_math_description_lists_operators(server) -> None:
    """演算子はセマンティック名でしか指定できないため、説明に列挙する。"""
    description = descriptions(server)["configure_math"]

    for operator in (
        "add", "subtract", "multiply", "divide", "and", "or", "xor", "not",
        "fft", "integrate", "differentiate", "sqrt", "log10", "ln", "exp",
        "abs", "lowpass", "highpass", "bandpass", "bandstop", "axb",
    ):
        assert operator in description


def test_configure_math_description_documents_the_cascade_rule(server) -> None:
    description = descriptions(server)["configure_math"]

    assert "MATH" in description
    assert "applied" in description


# --------------------------------------------------------------------------
# configure_math
# --------------------------------------------------------------------------


def test_configure_math_end_to_end(server) -> None:
    connected(server)

    result = data(
        server,
        "configure_math",
        {
            "channel": 1,
            "display": True,
            "operator": "subtract",
            "source1": "CH1",
            "source2": "CH2",
        },
    )

    assert result["channel"] == 1
    assert result["requested"]["operator"] == "subtract"
    assert result["applied"] == {
        "channel": 1,
        "display": True,
        "operator": "subtract",
        "source1": "CH1",
        "source2": "CH2",
    }
    assert result["changed"] is True


def test_configure_math_fft_subtree_end_to_end(server) -> None:
    connected(server)

    result = data(
        server,
        "configure_math",
        {"channel": 2, "operator": "fft", "fft": {"source": "CH1", "unit": "vrms"}},
    )

    assert result["applied"]["fft"] == {"source": "CH1", "unit": "vrms"}


def test_configure_math_without_any_item_returns_error_dict(server) -> None:
    connected(server)

    result = data(server, "configure_math", {"channel": 1})

    assert result["error"] is True
    assert result["code"] == ErrorCode.INVALID_PARAMETER


def test_configure_math_while_disconnected(server) -> None:
    result = data(server, "configure_math", {"operator": "add"})

    assert result["error"] is True
    assert result["code"] == ErrorCode.DEVICE_DISCONNECTED


# --------------------------------------------------------------------------
# get_math_state
# --------------------------------------------------------------------------


def test_get_math_state_single_channel_is_flat(server) -> None:
    connected(server)

    result = data(server, "get_math_state", {"channel": 1})

    assert result["channel"] == 1
    assert result["display"] is False
    assert result["operator"] == "add"
    assert "channels" not in result


def test_get_math_state_aggregates_every_channel(server) -> None:
    connected(server)

    result = data(server, "get_math_state")

    assert sorted(result["channels"]) == ["1", "2", "3", "4"]
    assert result["channels"]["3"]["channel"] == 3


def test_get_math_state_on_a_profile_without_math_returns_error_dict(
    server, manager: ConnectionManager
) -> None:
    """宣言の無い機種では空dictを「正常」に見せず UNSUPPORTED_FEATURE を返す。"""
    connected(server)
    manager.require_scope().profile = load_profile("rigol-generic")

    result = data(server, "get_math_state")

    assert result["error"] is True
    assert result["code"] == ErrorCode.UNSUPPORTED_FEATURE


def test_configure_math_on_a_profile_without_math_returns_error_dict(
    server, manager: ConnectionManager
) -> None:
    connected(server)
    manager.require_scope().profile = load_profile("rigol-generic")

    result = data(server, "configure_math", {"operator": "add"})

    assert result["error"] is True
    assert result["code"] == ErrorCode.UNSUPPORTED_FEATURE


# --------------------------------------------------------------------------
# capture_waveform / analyze_waveform のMATH対応
# --------------------------------------------------------------------------


def test_capture_waveform_accepts_a_math_source(server) -> None:
    connected(server)

    result = data(server, "capture_waveform", {"channel": "MATH1"})

    assert result["channel"] == "MATH1"
    assert result["points"] > 0
    assert "effective_sample_rate_sa_per_s" in result


def test_capture_waveform_of_an_fft_trace_reports_a_frequency_axis(
    server, scope: FakeScope
) -> None:
    connected(server)
    data(server, "configure_math", {"channel": 1, "operator": "fft"})

    result = data(server, "capture_waveform", {"channel": "MATH1"})

    assert result["x_unit"] == "Hz"
    assert "effective_sample_rate_sa_per_s" not in result


def test_analyze_waveform_rejects_an_fft_trace(server) -> None:
    connected(server)
    data(server, "configure_math", {"channel": 1, "operator": "fft"})

    result = data(server, "analyze_waveform", {"channel": "MATH1"})

    assert result["error"] is True
    assert result["code"] == ErrorCode.INVALID_PARAMETER


def test_capture_waveform_description_mentions_math(server) -> None:
    description = descriptions(server)["capture_waveform"]

    assert "MATH1" in description
