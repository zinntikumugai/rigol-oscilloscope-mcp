"""server.py の起動時チェック(操作クラス表との整合 / 監査ログの告知)。

Tool登録を薄いラッパーに通し、`safety/classes.py` の表を飾りではなく
起動時に効く不変条件にする(未登録Tool名・confirm_token欠落で起動失敗)。
"""

from pathlib import Path

import pytest
from mcp.server.fastmcp import FastMCP

from rigol_oscilloscope_mcp.config import Config
from rigol_oscilloscope_mcp.errors import ErrorCode, ScopeError
from rigol_oscilloscope_mcp.server import _checked_tool, create_server

EXPECTED_TOOL_COUNT = 19


@pytest.fixture
def config(tmp_path: Path) -> Config:
    root = tmp_path.resolve()
    return Config(
        screenshot_dir=root, allowed_dirs=(root,), audit_log=root / "audit.jsonl"
    )


def test_create_server_registers_every_tool(config: Config) -> None:
    server = create_server(config=config)

    assert len(server._tool_manager.list_tools()) == EXPECTED_TOOL_COUNT


def test_unknown_tool_name_fails_at_registration() -> None:
    register = _checked_tool(FastMCP("test"))

    def not_in_the_table() -> dict:
        return {}

    with pytest.raises(ScopeError) as excinfo:
        register(not_in_the_table)

    assert excinfo.value.code == ErrorCode.INVALID_PARAMETER


def test_restricted_tool_without_confirm_token_fails_at_registration() -> None:
    register = _checked_tool(FastMCP("test"))

    def autoset() -> dict:  # RESTRICTED_WRITE なのに confirm_token を受けない
        return {}

    with pytest.raises(ScopeError) as excinfo:
        register(autoset)

    assert excinfo.value.code == ErrorCode.SAFETY_POLICY_DENIED
    assert "confirm_token" in excinfo.value.message


def test_restricted_tool_with_confirm_token_registers() -> None:
    register = _checked_tool(FastMCP("test"))

    def autoset(confirm_token: str | None = None) -> dict:
        return {}

    assert register(autoset) is not None


def test_startup_announces_the_audit_log_path(
    config: Config, capsys: pytest.CaptureFixture[str]
) -> None:
    create_server(config=config)

    assert f"audit log: {config.audit_log}" in capsys.readouterr().err


def test_startup_announces_disabled_audit_log(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    create_server(config=Config(screenshot_dir=tmp_path, audit_log=None))

    assert "audit log: disabled" in capsys.readouterr().err
