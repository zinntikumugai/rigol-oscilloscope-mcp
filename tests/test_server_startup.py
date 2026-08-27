"""server.py の起動時チェック(操作クラス表との整合 / 監査ログの告知)。

Tool登録を薄いラッパーに通し、`safety/classes.py` の表を飾りではなく
起動時に効く不変条件にする(未登録Tool名・confirm_token欠落で起動失敗)。
"""

import logging
import sys
import threading
from collections.abc import Iterator
from dataclasses import replace
from pathlib import Path

import pytest
from mcp.server.fastmcp import FastMCP

from rigol_oscilloscope_mcp.config import Config
from rigol_oscilloscope_mcp.errors import ErrorCode, ScopeError
from rigol_oscilloscope_mcp.server import PACKAGE_LOGGER, _checked_tool, create_server

#: 登録Tool総数(Phase 1: 12 + Phase 2: 7 + Phase 4: 9 + Phase M1: 2 + Phase M2: 6
#: + Phase M3: 2)。
#: Tool一覧の総数はここが唯一の規範とし、フェーズ別テストは自分の集合だけを見る。
EXPECTED_TOOL_COUNT = 38


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
    register = _checked_tool(FastMCP("test"), threading.RLock())

    def not_in_the_table() -> dict:
        return {}

    with pytest.raises(ScopeError) as excinfo:
        register(not_in_the_table)

    assert excinfo.value.code == ErrorCode.INVALID_PARAMETER


def test_restricted_tool_without_confirm_token_fails_at_registration() -> None:
    register = _checked_tool(FastMCP("test"), threading.RLock())

    def autoset() -> dict:  # RESTRICTED_WRITE なのに confirm_token を受けない
        return {}

    with pytest.raises(ScopeError) as excinfo:
        register(autoset)

    assert excinfo.value.code == ErrorCode.SAFETY_POLICY_DENIED
    assert "confirm_token" in excinfo.value.message


def test_restricted_tool_with_confirm_token_registers() -> None:
    register = _checked_tool(FastMCP("test"), threading.RLock())

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


# --------------------------------------------------------------------------
# ログ設定(Requirements.md 8.3 / RIGOL_MCP_LOG_LEVEL)
# --------------------------------------------------------------------------


@pytest.fixture
def package_logger() -> Iterator[logging.Logger]:
    """パッケージロガーを白紙で貸し出し、テスト後に既定へ戻す。

    プロセス全体で共有される状態なので、他テストへ設定を持ち越さない。
    caplog は非伝播ロガーへ自前のハンドラを差し込むため、伝播も既定へ戻して
    おく(これらのテストはログ内容ではなく設定そのものを見る)。
    """
    logger = logging.getLogger(PACKAGE_LOGGER)
    logger.handlers.clear()
    logger.propagate = True
    try:
        yield logger
    finally:
        logger.handlers.clear()
        logger.setLevel(logging.NOTSET)
        logger.propagate = True


def test_create_server_applies_log_level(
    config: Config, package_logger: logging.Logger
) -> None:
    create_server(config=replace(config, log_level="debug"))

    assert package_logger.level == logging.DEBUG
    # MCPはstdoutをプロトコルに使う。ログはstderrへ出し、rootへは伝播させない
    assert package_logger.propagate is False
    [handler] = package_logger.handlers
    assert handler.stream is sys.stderr


def test_create_server_maps_warn_to_warning(
    config: Config, package_logger: logging.Logger
) -> None:
    create_server(config=replace(config, log_level="warn"))

    assert package_logger.level == logging.WARNING


def test_create_server_does_not_stack_handlers(
    config: Config, package_logger: logging.Logger
) -> None:
    create_server(config=config)
    create_server(config=config)

    assert len(package_logger.handlers) == 1
