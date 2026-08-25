"""service/paths.py のテスト。

保存先パスの確定(ディレクトリ/ファイル判定・拡張子補完・`~` 展開・相対パスの
基準)と、許可ルート検証(Requirements.md 8.4 / 9章)を確認する。
書き込みは全て tmp_path 配下に閉じる(拒否されるパスは作成されない)。
"""

import os
import tempfile
from pathlib import Path

import pytest

from rigol_oscilloscope_mcp.config import Config
from rigol_oscilloscope_mcp.errors import ErrorCode, ScopeError
from rigol_oscilloscope_mcp.service.paths import allowed_roots, resolve_write_path

STEM = "scope_20240102_030405"

TEMP_ROOT = Path(tempfile.gettempdir()).resolve()


@pytest.fixture
def root(tmp_path: Path) -> Path:
    return tmp_path.resolve()


@pytest.fixture
def config(root: Path) -> Config:
    return Config(screenshot_dir=root, allowed_dirs=(root,))


@pytest.fixture
def outside_config() -> Config:
    """一時ディレクトリの外にある実在しない保存先(拒否確認用)。

    tmp_path は一時ディレクトリ配下=常時許可なので、脱出の拒否を確かめるには
    許可ルートが temp の外にある設定が要る。拒否されたパスは作成されないため
    ファイルシステムには何も残らない。
    """
    base = Path("/rigol-mcp-test-root/shots")
    return Config(screenshot_dir=base, allowed_dirs=(base,))


# --------------------------------------------------------------------------
# allowed_roots
# --------------------------------------------------------------------------


def test_allowed_roots_includes_screenshot_dir_and_temp(root: Path) -> None:
    extra = root / "extra"
    config = Config(screenshot_dir=root / "shots", allowed_dirs=(extra,))

    roots = allowed_roots(config)

    assert extra in roots
    assert (root / "shots") in roots
    assert TEMP_ROOT in roots


def test_allowed_roots_excludes_cwd(root: Path) -> None:
    config = Config(screenshot_dir=root, allowed_dirs=(root,))

    assert Path.cwd().resolve() not in allowed_roots(config)


@pytest.mark.skipif(os.name != "posix", reason="/tmp はPOSIX固有")
def test_allowed_roots_includes_conventional_tmp(config: Config) -> None:
    assert Path("/tmp").resolve() in allowed_roots(config)


def test_allowed_roots_deduplicates(root: Path) -> None:
    config = Config(screenshot_dir=root, allowed_dirs=(root, root))

    roots = allowed_roots(config)

    assert len(roots) == len(set(roots))
    assert roots.count(root) == 1


# --------------------------------------------------------------------------
# 保存先の確定
# --------------------------------------------------------------------------


def test_none_uses_screenshot_dir_with_default_name(config: Config, root: Path) -> None:
    assert resolve_write_path(None, config, STEM, "png") == root / f"{STEM}.png"


def test_none_creates_missing_screenshot_dir(root: Path) -> None:
    config = Config(screenshot_dir=root / "shots", allowed_dirs=(root,))

    resolved = resolve_write_path(None, config, STEM, "png")

    assert resolved == root / "shots" / f"{STEM}.png"
    assert resolved.parent.is_dir()


def test_existing_directory_gets_default_name(config: Config, root: Path) -> None:
    directory = root / "sub"
    directory.mkdir()

    resolved = resolve_write_path(str(directory), config, STEM, "png")

    assert resolved == directory / f"{STEM}.png"


def test_trailing_separator_is_treated_as_directory(config: Config, root: Path) -> None:
    argument = str(root / "new-dir") + os.sep

    resolved = resolve_write_path(argument, config, STEM, "png")

    assert resolved == root / "new-dir" / f"{STEM}.png"
    assert resolved.parent.is_dir()


def test_file_path_with_extension_is_kept(config: Config, root: Path) -> None:
    target = root / "capture.png"

    assert resolve_write_path(str(target), config, STEM, "png") == target


def test_file_path_without_extension_gets_extension(config: Config, root: Path) -> None:
    resolved = resolve_write_path(str(root / "capture"), config, STEM, "jpg")

    assert resolved == root / "capture.jpg"


def test_parent_directories_are_created(config: Config, root: Path) -> None:
    resolved = resolve_write_path(str(root / "a" / "b" / "c.png"), config, STEM, "png")

    assert resolved == root / "a" / "b" / "c.png"
    assert resolved.parent.is_dir()


def test_relative_file_name_is_resolved_against_screenshot_dir(
    config: Config, root: Path
) -> None:
    assert resolve_write_path("shot.png", config, STEM, "png") == root / "shot.png"


def test_relative_subdirectory_is_resolved_against_screenshot_dir(
    config: Config, root: Path
) -> None:
    resolved = resolve_write_path("images/ch1.png", config, STEM, "png")

    assert resolved == root / "images" / "ch1.png"
    assert resolved.parent.is_dir()


def test_relative_directory_argument_gets_default_name(
    config: Config, root: Path
) -> None:
    resolved = resolve_write_path("images/", config, STEM, "png")

    assert resolved == root / "images" / f"{STEM}.png"


def test_relative_existing_directory_gets_default_name(
    config: Config, root: Path
) -> None:
    (root / "images").mkdir()

    resolved = resolve_write_path("images", config, STEM, "png")

    assert resolved == root / "images" / f"{STEM}.png"


def test_tilde_is_expanded(root: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    home = root / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("USERPROFILE", str(home))
    config = Config(screenshot_dir=root, allowed_dirs=(home,))

    resolved = resolve_write_path("~/shots/capture.png", config, STEM, "png")

    assert resolved == home / "shots" / "capture.png"


# --------------------------------------------------------------------------
# 許可ルート検証
# --------------------------------------------------------------------------


def test_path_outside_allowed_roots_is_rejected(config: Config) -> None:
    with pytest.raises(ScopeError) as excinfo:
        resolve_write_path("/etc/rigol-capture.png", config, STEM, "png")

    error = excinfo.value
    assert error.code == ErrorCode.INVALID_PARAMETER
    # macOS では /etc が /private/etc のシンボリックリンクのため末尾で確認する
    assert error.detail["path"].endswith("/rigol-capture.png")
    assert str(config.screenshot_dir) in error.detail["allowed_roots"]


def test_parent_traversal_escape_is_rejected(outside_config: Config) -> None:
    argument = "/rigol-mcp-test-root/shots/../../escaped.png"

    with pytest.raises(ScopeError) as excinfo:
        resolve_write_path(argument, outside_config, STEM, "png")

    assert excinfo.value.code == ErrorCode.INVALID_PARAMETER


def test_relative_traversal_escape_is_rejected(outside_config: Config) -> None:
    with pytest.raises(ScopeError) as excinfo:
        resolve_write_path("../escape.png", outside_config, STEM, "png")

    error = excinfo.value
    assert error.code == ErrorCode.INVALID_PARAMETER
    assert error.detail["path"] == "/rigol-mcp-test-root/escape.png"
    assert "RIGOL_MCP_ALLOWED_DIRS" in error.message
    assert "RIGOL_MCP_ALLOWED_DIRS" in error.detail["hint"]


def test_path_under_cwd_is_rejected(config: Config) -> None:
    """cwd(= --directory で移動したサーバープロジェクト)は許可ルートではない。"""
    with pytest.raises(ScopeError) as excinfo:
        resolve_write_path(str(Path.cwd() / "leak.png"), config, STEM, "png")

    assert excinfo.value.code == ErrorCode.INVALID_PARAMETER
    assert not (Path.cwd() / "leak.png").exists()


def test_temp_directory_is_always_allowed(outside_config: Config, root: Path) -> None:
    """screenshot_dir/allowed_dirs の外でも、一時ディレクトリ配下は書ける。"""
    target = root / "temp-shot.png"

    assert resolve_write_path(str(target), outside_config, STEM, "png") == target


def test_rejected_path_is_not_created(config: Config) -> None:
    with pytest.raises(ScopeError):
        resolve_write_path("/etc/rigol-mcp-dir/capture.png", config, STEM, "png")

    assert not Path("/etc/rigol-mcp-dir").exists()
