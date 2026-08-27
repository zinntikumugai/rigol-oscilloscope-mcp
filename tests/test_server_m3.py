"""server.py のテスト(リファレンス波形 / Phase M3)。

MCP SDK の in-memory 接続で往復させ、クライアントから見えるJSONを固定する。
値変換そのものの退行ガードは tests/test_scope_driver.py と
tests/test_control_service.py が持ち、本ファイルは Tool 層までの疎通に絞る。

ハーネスは Phase 1 / 2 / 4 / M1 / M2 と重複するが、テスト同士の import 依存を
作らないため自前に持つ(既存ファイルと同じ方針)。
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

M3_TOOLS = {"configure_reference", "get_reference_state"}


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


def test_list_tools_exposes_the_m3_tools(server) -> None:
    assert M3_TOOLS <= set(descriptions(server))


def test_configure_reference_documents_the_unchanged_and_applied_policy(server) -> None:
    description = descriptions(server)["configure_reference"]

    assert "Omitted items are left unchanged." in description
    assert "applied" in description


def test_configure_reference_warns_that_save_is_irreversible(server) -> None:
    """保存は不可逆で、枠の元の内容は戻せない。説明文で必ず警告する。"""
    description = descriptions(server)["configure_reference"]

    assert "save" in description
    assert "irreversib" in description.lower()
    assert "lost" in description.lower()
    # 「保存済みか」を問い合わせる手段が無いことも伝える
    assert "no way to check" in description.lower()


def test_configure_reference_documents_the_sources_and_colors(server) -> None:
    description = descriptions(server)["configure_reference"]

    for token in ("CH1", "MATH1", "D0", "gray", "green", "blue", "red", "orange"):
        assert token in description, token
    # ラベル表示は全枠共通のスイッチ
    assert "label_display" in description
    assert "every reference" in description.lower()
    # 有効なチャンネルしか選べないのは機器側の判定
    assert "instrument" in description


def test_get_reference_state_documents_both_shapes(server) -> None:
    description = descriptions(server)["get_reference_state"]

    assert "channels" in description
    assert "read-only" in description.lower()


# --------------------------------------------------------------------------
# configure_reference
# --------------------------------------------------------------------------


def test_configure_reference_end_to_end(server) -> None:
    connected(server)

    result = data(
        server,
        "configure_reference",
        {
            "ref": 2,
            "source": "CH2",
            "scale": 0.5,
            "offset_v": -1.0,
            "color": "blue",
            "label": "before_fix",
        },
    )

    assert result["ref"] == 2
    assert result["requested"]["source"] == "CH2"
    assert result["applied"] == {
        "ref": 2,
        "source": "CH2",
        "scale": 0.5,
        "offset_v": -1.0,
        "color": "blue",
        "label": "before_fix",
    }
    assert result["changed"] is True


def test_configure_reference_save_end_to_end(server, scope: FakeScope) -> None:
    connected(server)

    result = data(
        server, "configure_reference", {"ref": 3, "source": "CH2", "save": True}
    )

    assert result["applied"]["save"] is True
    assert scope.reference[3]["saved"] is True
    assert scope.reference[1]["saved"] is False


def test_configure_reference_reset_end_to_end(server, scope: FakeScope) -> None:
    connected(server)
    data(server, "configure_reference", {"ref": 4, "scale": 0.25})

    result = data(server, "configure_reference", {"ref": 4, "reset": True})

    assert result["applied"]["reset"] is True
    assert scope.reference[4]["vscale"] == 0.05


def test_configure_reference_label_display_switches_every_slot(
    server, scope: FakeScope
) -> None:
    connected(server)

    result = data(server, "configure_reference", {"ref": 1, "label_display": True})

    assert result["applied"]["label_display"] is True
    assert scope.reference_global["label_display"] is True


def test_configure_reference_rejects_an_unknown_slot(server) -> None:
    connected(server)

    result = data(server, "configure_reference", {"ref": 11, "scale": 1.0})

    assert result["error"] is True
    assert result["code"] == ErrorCode.INVALID_PARAMETER


def test_configure_reference_rejects_a_reference_source(server) -> None:
    """リファレンスはリファレンスをソースにできない(値域は CH / MATH / D)。"""
    connected(server)

    result = data(server, "configure_reference", {"ref": 1, "source": "REF2"})

    assert result["error"] is True
    assert result["code"] == ErrorCode.INVALID_PARAMETER


def test_configure_reference_without_any_item_returns_error_dict(server) -> None:
    connected(server)

    result = data(server, "configure_reference", {"ref": 1})

    assert result["error"] is True
    assert result["code"] == ErrorCode.INVALID_PARAMETER


def test_configure_reference_while_disconnected(server) -> None:
    result = data(server, "configure_reference", {"ref": 1, "scale": 1.0})

    assert result["error"] is True
    assert result["code"] == ErrorCode.DEVICE_DISCONNECTED


# --------------------------------------------------------------------------
# get_reference_state
# --------------------------------------------------------------------------


def test_get_reference_state_for_one_slot_is_flat(server) -> None:
    connected(server)
    data(server, "configure_reference", {"ref": 5, "source": "MATH2", "color": "red"})

    result = data(server, "get_reference_state", {"ref": 5})

    assert result == {
        "ref": 5,
        "source": "MATH2",
        "scale": 0.05,
        "offset_v": 0.0,
        "color": "red",
        "label": "REF5",
        "label_display": False,
    }


def test_get_reference_state_returns_every_slot(server) -> None:
    connected(server)

    result = data(server, "get_reference_state")

    assert set(result) == {"channels"}
    assert set(result["channels"]) == {str(n) for n in range(1, 11)}
    assert result["channels"]["10"]["ref"] == 10
    assert result["channels"]["1"]["source"] == "CH1"


def test_reference_tools_on_a_profile_without_them_return_error_dicts(
    server, manager: ConnectionManager
) -> None:
    connected(server)
    manager.require_scope().profile = load_profile("rigol-generic")

    for name, args in (
        ("configure_reference", {"ref": 1, "scale": 1.0}),
        ("get_reference_state", {"ref": 1}),
        ("get_reference_state", {}),
    ):
        result = data(server, name, args)

        assert result["error"] is True, name
        assert result["code"] == ErrorCode.UNSUPPORTED_FEATURE, name
