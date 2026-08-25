"""server.py のテスト(Phase 1 Tool群 / tools.md 8章)。

MCP SDK の in-memory 接続で実際にTool呼び出しを往復させ、クライアントから
見える content(text JSON / image)を固定する。機器は FakeTransport 注入で
差し替えるため実接続は起きない(アドレスはダミーの TEST-NET-1)。

非同期テストランナー(pytest-asyncio)は導入せず、mcp SDK が同梱する anyio の
`anyio.run` を同期テストから呼ぶ。
"""

import json
from pathlib import Path

import anyio
import pytest
from mcp.shared.memory import create_connected_server_and_client_session
from mcp.types import CallToolResult, ImageContent, TextContent

from rigol_oscilloscope_mcp.config import Config
from rigol_oscilloscope_mcp.errors import ErrorCode
from rigol_oscilloscope_mcp.server import create_server
from rigol_oscilloscope_mcp.service import ConnectionManager
from rigol_oscilloscope_mcp.testing import FakeScope, FakeTransport

ADDRESS = "192.0.2.10"  # TEST-NET-1(実接続は FakeTransport が肩代わりする)
PNG_MAGIC = b"\x89PNG\r\n\x1a\n"

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


# --------------------------------------------------------------------------
# ハーネス
# --------------------------------------------------------------------------


@pytest.fixture
def config(tmp_path: Path) -> Config:
    root = tmp_path.resolve()
    return Config(screenshot_dir=root, allowed_dirs=(root,))


@pytest.fixture
def manager(config: Config) -> ConnectionManager:
    scope = FakeScope()

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


def connected(server) -> None:
    assert payload(call(server, "connect", {"address": ADDRESS}))["connected"] is True


# --------------------------------------------------------------------------
# Tool登録
# --------------------------------------------------------------------------


def test_list_tools_exposes_all_phase1_tools(server) -> None:
    """Phase 1 のToolが揃っていること(全体の顔ぶれは phase2 側で固定する)。"""

    async def main() -> list[str]:
        async with create_connected_server_and_client_session(server) as client:
            return [tool.name for tool in (await client.list_tools()).tools]

    names = anyio.run(main)

    assert PHASE1_TOOLS <= set(names)
    assert len(names) == len(set(names))


def test_tool_descriptions_guide_the_llm(server) -> None:
    async def main() -> dict[str, str]:
        async with create_connected_server_and_client_session(server) as client:
            return {t.name: t.description or "" for t in (await client.list_tools()).tools}

    descriptions = anyio.run(main)

    assert "sections" in descriptions["get_state"]
    assert "ユーザー" in descriptions["connect"]


# --------------------------------------------------------------------------
# 接続
# --------------------------------------------------------------------------


def test_scope_identify_when_disconnected_is_not_an_error(server) -> None:
    result = call(server, "scope_identify")

    assert result.isError is False
    data = payload(result)
    assert data["connected"] is False
    assert data.get("error") is None


def test_connect_returns_identification(server) -> None:
    data = payload(call(server, "connect", {"address": ADDRESS}))

    assert data["connected"] is True
    assert data["address"] == ADDRESS
    assert data["transport"] == "lan"
    assert data["idn"]["model"] == "MHO98"
    assert data["profile"] == {"name": "mho98", "confidence": "verified"}
    assert data["unsupported_vendor"] is False


def test_connect_without_address_asks_the_user(server) -> None:
    data = payload(call(server, "connect"))

    assert data["error"] is True
    assert data["code"] == ErrorCode.INVALID_PARAMETER
    assert "ユーザー" in data["message"]


def test_disconnect_is_idempotent(server) -> None:
    first, second, identify = run_calls(
        server, ("disconnect", {}), ("disconnect", {}), ("scope_identify", {})
    )

    assert first.isError is False
    assert second.isError is False
    assert payload(identify)["connected"] is False


def test_get_capabilities_reports_profile(server) -> None:
    connected(server)

    data = payload(call(server, "get_capabilities"))

    assert data["profile"] == {"name": "mho98", "confidence": "verified"}
    assert data["capabilities"]["analog_channels"] == 4
    assert data["unsupported_vendor"] is False


# --------------------------------------------------------------------------
# 状態取得
# --------------------------------------------------------------------------


def test_get_state_honours_sections(server) -> None:
    connected(server)

    data = payload(call(server, "get_state", {"sections": ["trigger"]}))

    assert list(data) == ["trigger"]
    assert data["trigger"]["source"] == "CH1"


def test_get_state_without_sections_returns_everything(server) -> None:
    connected(server)

    data = payload(call(server, "get_state"))

    assert set(data) == {"channels", "timebase", "trigger", "acquisition"}


def test_get_channel(server) -> None:
    connected(server)

    data = payload(call(server, "get_channel", {"channel": "CH1"}))

    assert data["channel"] == "CH1"
    assert "scale_v_per_div" in data
    assert "coupling" in data


def test_get_channel_rejects_unknown_channel(server) -> None:
    connected(server)

    result = call(server, "get_channel", {"channel": "CH9"})

    assert result.isError is False  # コードをLLMに読ませるため正常返却にする
    data = payload(result)
    assert data["error"] is True
    assert data["code"] == ErrorCode.INVALID_PARAMETER


def test_get_timebase_and_trigger_and_acquisition(server) -> None:
    connected(server)

    timebase, trigger, acquisition = (
        payload(result)
        for result in run_calls(
            server, ("get_timebase", {}), ("get_trigger", {}), ("get_acquisition_state", {})
        )
    )

    assert "scale_s_per_div" in timebase
    assert trigger["type"] == "edge"
    assert "running" in acquisition


# --------------------------------------------------------------------------
# 測定・データ取得
# --------------------------------------------------------------------------


def test_measure(server) -> None:
    connected(server)

    data = payload(
        call(server, "measure", {"channel": "CH1", "measurements": ["frequency", "vpp"]})
    )

    assert data["channel"] == "CH1"
    assert data["values"]["frequency_hz"] == pytest.approx(1000.1)
    assert "vpp_v" in data["values"]


def test_measure_while_disconnected(server) -> None:
    result = call(server, "measure", {"channel": "CH1", "measurements": ["frequency"]})

    assert result.isError is False
    data = payload(result)
    assert data["error"] is True
    assert data["code"] == ErrorCode.DEVICE_DISCONNECTED
    assert "connect" in data["message"]


def test_capture_waveform(server) -> None:
    connected(server)

    data = payload(call(server, "capture_waveform", {"channel": "CH1"}))

    assert data["points"] == 1000
    assert len(data["samples_v"]) == 1000
    assert data["sample_interval_s"] == pytest.approx(2e-6)


def test_capture_screenshot_returns_image_content(server, config: Config) -> None:
    connected(server)

    result = call(server, "capture_screenshot")

    assert len(result.content) == 2
    meta, image = result.content
    assert isinstance(meta, TextContent)
    assert isinstance(image, ImageContent)
    assert image.mimeType == "image/png"

    data = json.loads(meta.text)
    assert data["format"] == "png"
    assert data["mime"] == "image/png"
    saved = Path(data["saved_path"])
    assert saved.parent == config.screenshot_dir
    assert saved.read_bytes()[:8] == PNG_MAGIC
    assert data["size_bytes"] == saved.stat().st_size


def test_capture_screenshot_can_skip_the_image(server) -> None:
    connected(server)

    result = call(server, "capture_screenshot", {"return_image": False})

    assert [c.type for c in result.content] == ["text"]
    assert Path(payload(result)["saved_path"]).exists()


def test_capture_screenshot_rejects_path_outside_allowed_roots(server, tmp_path: Path) -> None:
    connected(server)

    outside = tmp_path.parent / "outside_root" / "shot.png"
    data = payload(call(server, "capture_screenshot", {"path": str(outside)}))

    assert data["error"] is True
    assert data["code"] == ErrorCode.INVALID_PARAMETER


# --------------------------------------------------------------------------
# ファクトリ
# --------------------------------------------------------------------------


def test_fake_mode_builds_a_usable_server(
    monkeypatch: pytest.MonkeyPatch, config: Config
) -> None:
    """RIGOL_MCP_FAKE=1 なら実機なしで connect まで通る(手動E2E用)。"""
    monkeypatch.setenv("RIGOL_MCP_FAKE", "1")

    server = create_server(config=config)

    assert payload(call(server, "connect", {"address": ADDRESS}))["idn"]["model"] == "MHO98"


def test_unexpected_exception_becomes_internal_error(
    server, manager: ConnectionManager
) -> None:
    def boom() -> None:
        raise RuntimeError("予期しない失敗")

    manager.require_scope = boom  # type: ignore[method-assign]

    data = payload(call(server, "get_timebase"))

    assert data["error"] is True
    assert data["code"] == "INTERNAL_ERROR"
    assert data["detail"]["type"] == "RuntimeError"
