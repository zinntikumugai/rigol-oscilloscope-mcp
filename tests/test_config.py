"""config.py のテスト(環境変数 > TOMLファイル > デフォルト)。"""

import os
from pathlib import Path

import pytest

from rigol_oscilloscope_mcp.config import Config, load_config
from rigol_oscilloscope_mcp.errors import ErrorCode, ScopeError


def write_toml(tmp_path: Path, body: str) -> Path:
    path = tmp_path / "config.toml"
    path.write_text(body, encoding="utf-8")
    return path


# --- デフォルト -------------------------------------------------------------


def test_defaults_with_empty_env(tmp_path: Path) -> None:
    cfg = load_config(env={})
    assert cfg.address is None
    assert cfg.transport is None
    assert cfg.port is None
    assert cfg.timeout_s == 5.0
    assert cfg.waveform_max_points == 100000
    assert cfg.raw_scpi is False
    assert cfg.log_level == "info"
    assert cfg.audit_log is None


def test_config_is_frozen() -> None:
    cfg = load_config(env={})
    with pytest.raises(Exception):
        cfg.address = "192.0.2.1"  # type: ignore[misc]


def test_default_screenshot_dir_is_cwd() -> None:
    cfg = load_config(env={})
    assert cfg.screenshot_dir == Path.cwd().resolve()


def test_default_allowed_dirs_contains_cwd() -> None:
    cfg = load_config(env={})
    assert Path.cwd().resolve() in cfg.allowed_dirs


def test_config_dataclass_constructible_without_args() -> None:
    cfg = Config()
    assert cfg.timeout_s == 5.0
    assert cfg.allowed_dirs == ()


# --- 環境変数 ---------------------------------------------------------------


def test_env_address_and_transport() -> None:
    cfg = load_config(env={"RIGOL_MCP_ADDRESS": "192.0.2.10", "RIGOL_MCP_TRANSPORT": "lan"})
    assert cfg.address == "192.0.2.10"
    assert cfg.transport == "lan"


def test_env_transport_is_case_insensitive() -> None:
    cfg = load_config(env={"RIGOL_MCP_TRANSPORT": "USB"})
    assert cfg.transport == "usb"


def test_env_numeric_values() -> None:
    cfg = load_config(
        env={
            "RIGOL_MCP_PORT": "5555",
            "RIGOL_MCP_TIMEOUT_S": "2.5",
            "RIGOL_MCP_WAVEFORM_MAX_POINTS": "1200",
        }
    )
    assert cfg.port == 5555
    assert cfg.timeout_s == 2.5
    assert cfg.waveform_max_points == 1200


@pytest.mark.parametrize("raw", ["1", "true", "TRUE", "yes", "on"])
def test_env_raw_scpi_true_values(raw: str) -> None:
    assert load_config(env={"RIGOL_MCP_RAW_SCPI": raw}).raw_scpi is True


@pytest.mark.parametrize("raw", ["0", "false", "False", "no", "off"])
def test_env_raw_scpi_false_values(raw: str) -> None:
    assert load_config(env={"RIGOL_MCP_RAW_SCPI": raw}).raw_scpi is False


def test_env_log_level_normalized() -> None:
    assert load_config(env={"RIGOL_MCP_LOG_LEVEL": "DEBUG"}).log_level == "debug"


def test_env_paths_are_expanded_and_resolved(tmp_path: Path) -> None:
    cfg = load_config(
        env={
            "RIGOL_MCP_SCREENSHOT_DIR": str(tmp_path / "shots"),
            "RIGOL_MCP_AUDIT_LOG": str(tmp_path / "audit.jsonl"),
        }
    )
    assert cfg.screenshot_dir == (tmp_path / "shots").resolve()
    assert cfg.audit_log == (tmp_path / "audit.jsonl").resolve()


def test_env_screenshot_dir_expands_user(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setenv("HOME", str(tmp_path))
    cfg = load_config(env={"RIGOL_MCP_SCREENSHOT_DIR": "~/captures"})
    assert cfg.screenshot_dir == (tmp_path / "captures").resolve()


def test_env_allowed_dirs_pathsep_separated(tmp_path: Path) -> None:
    a = tmp_path / "a"
    b = tmp_path / "b"
    cfg = load_config(
        env={"RIGOL_MCP_ALLOWED_DIRS": os.pathsep.join([str(a), str(b)])}
    )
    assert a.resolve() in cfg.allowed_dirs
    assert b.resolve() in cfg.allowed_dirs


def test_allowed_dirs_always_include_screenshot_dir_and_cwd(tmp_path: Path) -> None:
    shots = tmp_path / "shots"
    other = tmp_path / "other"
    cfg = load_config(
        env={
            "RIGOL_MCP_SCREENSHOT_DIR": str(shots),
            "RIGOL_MCP_ALLOWED_DIRS": str(other),
        }
    )
    assert shots.resolve() in cfg.allowed_dirs
    assert Path.cwd().resolve() in cfg.allowed_dirs
    assert other.resolve() in cfg.allowed_dirs


def test_allowed_dirs_deduplicated(tmp_path: Path) -> None:
    shots = tmp_path / "shots"
    cfg = load_config(
        env={
            "RIGOL_MCP_SCREENSHOT_DIR": str(shots),
            "RIGOL_MCP_ALLOWED_DIRS": os.pathsep.join([str(shots), str(shots)]),
        }
    )
    assert cfg.allowed_dirs.count(shots.resolve()) == 1


def test_allowed_dirs_is_tuple_of_paths() -> None:
    cfg = load_config(env={})
    assert isinstance(cfg.allowed_dirs, tuple)
    assert all(isinstance(p, Path) for p in cfg.allowed_dirs)


def test_env_empty_string_is_ignored() -> None:
    """空文字の環境変数は未設定として扱う。"""
    cfg = load_config(env={"RIGOL_MCP_ADDRESS": "", "RIGOL_MCP_PORT": ""})
    assert cfg.address is None
    assert cfg.port is None


def test_env_none_uses_os_environ(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("RIGOL_MCP_ADDRESS", "198.51.100.7")
    monkeypatch.delenv("RIGOL_MCP_CONFIG", raising=False)
    assert load_config().address == "198.51.100.7"


# --- TOMLファイル -----------------------------------------------------------


def test_toml_file_values(tmp_path: Path) -> None:
    path = write_toml(
        tmp_path,
        'address = "198.51.100.20"\ntransport = "lan"\nport = 5556\n'
        "timeout_s = 9.5\nwaveform_max_points = 500\nraw_scpi = true\n"
        'log_level = "debug"\n',
    )
    cfg = load_config(env={}, config_file=path)
    assert cfg.address == "198.51.100.20"
    assert cfg.transport == "lan"
    assert cfg.port == 5556
    assert cfg.timeout_s == 9.5
    assert cfg.waveform_max_points == 500
    assert cfg.raw_scpi is True
    assert cfg.log_level == "debug"


def test_toml_paths(tmp_path: Path) -> None:
    shots = tmp_path / "shots"
    audit = tmp_path / "audit.jsonl"
    path = write_toml(
        tmp_path,
        f'screenshot_dir = "{shots}"\naudit_log = "{audit}"\n',
    )
    cfg = load_config(env={}, config_file=path)
    assert cfg.screenshot_dir == shots.resolve()
    assert cfg.audit_log == audit.resolve()


def test_toml_allowed_dirs_as_list(tmp_path: Path) -> None:
    a = tmp_path / "a"
    b = tmp_path / "b"
    path = write_toml(tmp_path, f'allowed_dirs = ["{a}", "{b}"]\n')
    cfg = load_config(env={}, config_file=path)
    assert a.resolve() in cfg.allowed_dirs
    assert b.resolve() in cfg.allowed_dirs


def test_config_file_from_env_var(tmp_path: Path) -> None:
    path = write_toml(tmp_path, 'address = "203.0.113.5"\n')
    cfg = load_config(env={"RIGOL_MCP_CONFIG": str(path)})
    assert cfg.address == "203.0.113.5"


def test_env_overrides_toml(tmp_path: Path) -> None:
    path = write_toml(tmp_path, 'address = "198.51.100.20"\ntimeout_s = 9.5\n')
    cfg = load_config(
        env={"RIGOL_MCP_ADDRESS": "192.0.2.99", "RIGOL_MCP_CONFIG": str(path)}
    )
    assert cfg.address == "192.0.2.99"
    # 環境変数が無いキーはファイル値が残る
    assert cfg.timeout_s == 9.5


def test_toml_overrides_defaults_only_for_present_keys(tmp_path: Path) -> None:
    path = write_toml(tmp_path, "timeout_s = 1.0\n")
    cfg = load_config(env={}, config_file=path)
    assert cfg.timeout_s == 1.0
    assert cfg.waveform_max_points == 100000


def test_unknown_toml_keys_are_ignored(tmp_path: Path) -> None:
    path = write_toml(tmp_path, 'address = "192.0.2.1"\nfuture_option = "x"\n')
    cfg = load_config(env={}, config_file=path)
    assert cfg.address == "192.0.2.1"


# --- 異常系 -----------------------------------------------------------------


def _assert_invalid_parameter(exc_info: pytest.ExceptionInfo[ScopeError]) -> None:
    assert exc_info.value.code == ErrorCode.INVALID_PARAMETER


def test_invalid_port_raises() -> None:
    with pytest.raises(ScopeError) as exc:
        load_config(env={"RIGOL_MCP_PORT": "abc"})
    _assert_invalid_parameter(exc)


def test_invalid_timeout_raises() -> None:
    with pytest.raises(ScopeError) as exc:
        load_config(env={"RIGOL_MCP_TIMEOUT_S": "fast"})
    _assert_invalid_parameter(exc)


def test_non_positive_timeout_raises() -> None:
    with pytest.raises(ScopeError) as exc:
        load_config(env={"RIGOL_MCP_TIMEOUT_S": "0"})
    _assert_invalid_parameter(exc)


def test_invalid_waveform_max_points_raises() -> None:
    with pytest.raises(ScopeError) as exc:
        load_config(env={"RIGOL_MCP_WAVEFORM_MAX_POINTS": "-1"})
    _assert_invalid_parameter(exc)


def test_invalid_bool_raises() -> None:
    with pytest.raises(ScopeError) as exc:
        load_config(env={"RIGOL_MCP_RAW_SCPI": "maybe"})
    _assert_invalid_parameter(exc)


def test_invalid_transport_raises() -> None:
    with pytest.raises(ScopeError) as exc:
        load_config(env={"RIGOL_MCP_TRANSPORT": "wifi"})
    _assert_invalid_parameter(exc)


def test_invalid_log_level_raises() -> None:
    with pytest.raises(ScopeError) as exc:
        load_config(env={"RIGOL_MCP_LOG_LEVEL": "verbose"})
    _assert_invalid_parameter(exc)


def test_invalid_port_range_raises() -> None:
    with pytest.raises(ScopeError) as exc:
        load_config(env={"RIGOL_MCP_PORT": "0"})
    _assert_invalid_parameter(exc)


def test_missing_config_file_raises(tmp_path: Path) -> None:
    with pytest.raises(ScopeError) as exc:
        load_config(env={}, config_file=tmp_path / "nope.toml")
    _assert_invalid_parameter(exc)


def test_broken_toml_raises(tmp_path: Path) -> None:
    path = write_toml(tmp_path, "this is not toml =\n")
    with pytest.raises(ScopeError) as exc:
        load_config(env={}, config_file=path)
    _assert_invalid_parameter(exc)


def test_toml_wrong_type_raises(tmp_path: Path) -> None:
    path = write_toml(tmp_path, 'timeout_s = "soon"\n')
    with pytest.raises(ScopeError) as exc:
        load_config(env={}, config_file=path)
    _assert_invalid_parameter(exc)


def test_error_detail_names_the_offending_key() -> None:
    with pytest.raises(ScopeError) as exc:
        load_config(env={"RIGOL_MCP_PORT": "abc"})
    assert "port" in str(exc.value.detail) or "port" in exc.value.message


# --- PWD(実行ディレクトリ)フォールバック -----------------------------------


def test_pwd_becomes_default_screenshot_dir(tmp_path: Path) -> None:
    cfg = load_config(env={"PWD": str(tmp_path)})
    assert cfg.screenshot_dir == tmp_path.resolve()


def test_screenshot_dir_env_wins_over_pwd(tmp_path: Path) -> None:
    shots = tmp_path / "shots"
    shots.mkdir()
    cfg = load_config(
        env={"RIGOL_MCP_SCREENSHOT_DIR": str(shots), "PWD": str(tmp_path)}
    )
    assert cfg.screenshot_dir == shots.resolve()


def test_missing_pwd_falls_back_to_cwd(tmp_path: Path) -> None:
    cfg = load_config(env={"PWD": str(tmp_path / "nope")})
    assert cfg.screenshot_dir == Path.cwd().resolve()


def test_relative_pwd_falls_back_to_cwd() -> None:
    cfg = load_config(env={"PWD": "relative/dir"})
    assert cfg.screenshot_dir == Path.cwd().resolve()


def test_unwritable_pwd_falls_back_to_cwd(tmp_path: Path) -> None:
    locked = tmp_path / "locked"
    locked.mkdir()
    locked.chmod(0o555)
    try:
        cfg = load_config(env={"PWD": str(locked)})
    finally:
        locked.chmod(0o755)
    assert cfg.screenshot_dir == Path.cwd().resolve()


def test_no_pwd_key_keeps_cwd_default() -> None:
    cfg = load_config(env={})
    assert cfg.screenshot_dir == Path.cwd().resolve()


def test_pwd_dir_is_in_allowed_dirs(tmp_path: Path) -> None:
    cfg = load_config(env={"PWD": str(tmp_path)})
    assert tmp_path.resolve() in cfg.allowed_dirs
