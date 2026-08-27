"""server.py のテスト(カーソル・カウンタ・電圧計・ヒストグラム / Phase M2)。

MCP SDK の in-memory 接続で往復させ、クライアントから見えるJSONを固定する。
値変換そのものの退行ガードは tests/test_scope_driver.py と
tests/test_control_service.py が持ち、本ファイルは Tool 層までの疎通に絞る。

ハーネスは Phase 1 / 2 / 4 / M1 と重複するが、テスト同士の import 依存を作らない
ため自前に持つ(既存ファイルと同じ方針)。
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

M2_TOOLS = {
    "configure_cursor",
    "get_cursor_measurement",
    "configure_meter",
    "get_meter_value",
    "configure_histogram",
    "get_histogram_result",
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


def descriptions(server) -> dict[str, str]:
    async def main() -> dict[str, str]:
        async with create_connected_server_and_client_session(server) as client:
            return {t.name: t.description or "" for t in (await client.list_tools()).tools}

    return anyio.run(main)


# --------------------------------------------------------------------------
# Tool登録・説明文
# --------------------------------------------------------------------------


def test_list_tools_exposes_the_m2_tools(server) -> None:
    assert M2_TOOLS <= set(descriptions(server))


def test_configure_tools_document_the_unchanged_and_applied_policy(server) -> None:
    """未指定は変更しない / applied を信じる、はM2の3Toolでも同じ約束。"""
    for tool in ("configure_cursor", "configure_meter", "configure_histogram"):
        description = descriptions(server)[tool]

        assert "Omitted items are left unchanged." in description
        assert "applied" in description


def test_configure_cursor_description_documents_subtrees_and_units(server) -> None:
    description = descriptions(server)["configure_cursor"]

    for mode in ("manual", "track", "xy", "off"):
        assert mode in description
    assert "seconds" in description
    assert "volts" in description


def test_configure_meter_description_lists_the_modes_per_kind(server) -> None:
    """モードはセマンティック名でしか指定できず、種別ごとに値域が違う。"""
    description = descriptions(server)["configure_meter"]

    for mode in ("frequency", "period", "totalize", "ac_rms", "dc_rms"):
        assert mode in description
    assert "D0" in description  # カウンタはデジタルchも取る
    assert "analog" in description  # 電圧計はアナログchのみ
    assert "3-6" in description  # 分解能の値域
    assert "instrument" in description  # モードとの結合制約は機器側の判定


def test_configure_histogram_description_documents_the_range_constraint(server) -> None:
    description = descriptions(server)["configure_histogram"]

    assert "left_s" in description
    assert "right_s" in description
    assert "bottom_v" in description
    assert "top_v" in description
    assert "both" in description  # ホスト側検証は両端指定時のみ


def test_get_histogram_result_description_names_the_statistics(server) -> None:
    """M2実機検証済み: 機器自身がラベルを返すので、キーと単位を説明できる。"""
    description = descriptions(server)["get_histogram_result"]

    assert "raw" in description
    assert "stats" in description
    assert "pk_pk" in description
    assert "_unit" in description
    assert "disabled" in description  # 無効時に raw が空になる理由
    assert "not named" not in description  # 未確定という但し書きは解消済み


# --------------------------------------------------------------------------
# カーソル
# --------------------------------------------------------------------------


def test_configure_cursor_end_to_end(server) -> None:
    connected(server)

    result = data(
        server,
        "configure_cursor",
        {"mode": "manual", "type": "time", "source": "CH2", "ax": -1e-4, "bx": 1e-4},
    )

    assert result["mode"] == "manual"
    assert result["requested"]["source"] == "CH2"
    assert result["applied"] == {
        "mode": "manual",
        "type": "time",
        "source": "CH2",
        "ax": -1e-4,
        "bx": 1e-4,
    }
    assert result["changed"] is True


def test_get_cursor_measurement_end_to_end(server) -> None:
    connected(server)
    data(server, "configure_cursor", {"mode": "manual", "ax": 1e-4, "bx": 3e-4})

    result = data(server, "get_cursor_measurement")

    assert result["mode"] == "manual"
    assert result["xdelta_s"] == pytest.approx(2e-4)
    assert result["ixdelta_hz"] == pytest.approx(5e3)


def test_get_cursor_measurement_off_returns_the_mode_only(server) -> None:
    connected(server)

    assert data(server, "get_cursor_measurement") == {"mode": "off"}


def test_configure_cursor_without_any_item_returns_error_dict(server) -> None:
    connected(server)

    result = data(server, "configure_cursor")

    assert result["error"] is True
    assert result["code"] == ErrorCode.INVALID_PARAMETER


def test_configure_cursor_while_disconnected(server) -> None:
    result = data(server, "configure_cursor", {"mode": "manual"})

    assert result["error"] is True
    assert result["code"] == ErrorCode.DEVICE_DISCONNECTED


# --------------------------------------------------------------------------
# 周波数カウンタ・電圧計
# --------------------------------------------------------------------------


def test_configure_meter_counter_end_to_end(server) -> None:
    connected(server)

    result = data(
        server,
        "configure_meter",
        {"kind": "counter", "enabled": True, "source": "D3", "mode": "period"},
    )

    assert result["kind"] == "counter"
    assert result["applied"] == {
        "kind": "counter",
        "enabled": True,
        "source": "D3",
        "mode": "period",
    }
    assert result["changed"] is True


def test_configure_meter_clear_totalize_end_to_end(server, scope: FakeScope) -> None:
    connected(server)

    result = data(
        server,
        "configure_meter",
        {"kind": "counter", "mode": "totalize", "clear_totalize": True},
    )

    assert result["applied"]["clear_totalize"] is True
    assert scope.counter["total"] == 0.0


def test_get_meter_value_reports_the_unit_of_the_mode(server) -> None:
    connected(server)
    data(server, "configure_meter", {"kind": "counter", "enabled": True})
    data(server, "configure_meter", {"kind": "dvm", "enabled": True})

    counter = data(server, "get_meter_value", {"kind": "counter"})
    assert counter["mode"] == "frequency"
    assert counter["value"] == pytest.approx(1.0e3)
    assert counter["unit"] == "Hz"

    data(server, "configure_meter", {"kind": "counter", "mode": "period"})
    assert data(server, "get_meter_value", {"kind": "counter"})["unit"] == "s"

    dvm = data(server, "get_meter_value", {"kind": "dvm"})
    assert dvm["unit"] == "V"
    assert dvm["mode"] == "ac_rms"


def test_configure_meter_with_an_unknown_kind_returns_error_dict(server) -> None:
    connected(server)

    result = data(server, "configure_meter", {"kind": "voltmeter", "enabled": True})

    assert result["error"] is True
    assert result["code"] == ErrorCode.INVALID_PARAMETER


def test_get_meter_value_on_a_profile_without_the_meter_returns_error_dict(
    server, manager: ConnectionManager
) -> None:
    connected(server)
    manager.require_scope().profile = load_profile("rigol-generic")

    result = data(server, "get_meter_value", {"kind": "dvm"})

    assert result["error"] is True
    assert result["code"] == ErrorCode.UNSUPPORTED_FEATURE


# --------------------------------------------------------------------------
# ヒストグラム
# --------------------------------------------------------------------------


def test_configure_histogram_end_to_end(server) -> None:
    connected(server)

    result = data(
        server,
        "configure_histogram",
        {"enabled": True, "type": "vertical", "source": "CH2", "height": 4},
    )

    assert result["applied"] == {
        "enabled": True,
        "type": "vertical",
        "source": "CH2",
        "height": 4,
    }
    assert result["changed"] is True


def test_configure_histogram_reset_end_to_end(server, scope: FakeScope) -> None:
    connected(server)

    result = data(server, "configure_histogram", {"source": "CH2", "reset": True})

    assert result["applied"]["reset"] is True
    assert scope.histogram["hits"] == 0


def test_configure_histogram_rejects_reversed_ranges(server) -> None:
    connected(server)

    result = data(server, "configure_histogram", {"left_s": 1e-3, "right_s": -1e-3})

    assert result["error"] is True
    assert result["code"] == ErrorCode.INVALID_PARAMETER


def test_get_histogram_result_always_returns_the_raw_response(server) -> None:
    connected(server)
    data(server, "configure_histogram", {"enabled": True})

    result = data(server, "get_histogram_result")

    assert result["raw"].startswith("[Sum:374hits,")
    assert result["stats"]["pk_pk"] == pytest.approx(2.562)
    assert result["stats"]["pk_pk_unit"] == "V"


def test_get_histogram_result_while_disabled_explains_the_empty_raw(server) -> None:
    """無効時は統計クエリを送らない(エラーキューを汚さない)。理由は warnings。"""
    connected(server)

    result = data(server, "get_histogram_result")

    assert result["raw"] == ""
    assert "disabled" in result["warnings"][0]

    # 汚染が残っていれば、この直後の無関係な書き込みが SCPI_ERROR で落ちる
    assert data(server, "configure_histogram", {"height": 3})["applied"] == {
        "height": 3
    }


def test_get_meter_value_while_disabled_returns_null(server) -> None:
    connected(server)

    result = data(server, "get_meter_value", {"kind": "dvm"})

    assert result["enabled"] is False
    assert result["value"] is None


def test_histogram_tools_on_a_profile_without_it_return_error_dicts(
    server, manager: ConnectionManager
) -> None:
    connected(server)
    manager.require_scope().profile = load_profile("rigol-generic")

    for name, args in (
        ("configure_histogram", {"enabled": True}),
        ("get_histogram_result", {}),
        ("configure_cursor", {"mode": "manual"}),
        ("get_cursor_measurement", {}),
        ("configure_meter", {"kind": "counter", "enabled": True}),
    ):
        result = data(server, name, args)

        assert result["error"] is True, name
        assert result["code"] == ErrorCode.UNSUPPORTED_FEATURE, name
