"""設定解決(Requirements.md 9章)。

優先順位: 環境変数(RIGOL_MCP_*) > TOML設定ファイル > 組み込みデフォルト。
Tool引数はさらに上位だが、それは各Tool側で本Configを上書きして扱う。
"""

from __future__ import annotations

import os
import tomllib
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .errors import ErrorCode, ScopeError

ENV_PREFIX = "RIGOL_MCP_"
CONFIG_ENV_VAR = ENV_PREFIX + "CONFIG"

DEFAULT_TIMEOUT_S = 5.0
DEFAULT_WAVEFORM_MAX_POINTS = 100000
DEFAULT_LOG_LEVEL = "info"

VALID_TRANSPORTS = ("lan", "usb")
VALID_LOG_LEVELS = ("error", "warn", "info", "debug")

_TRUE_VALUES = frozenset({"1", "true", "yes", "on"})
_FALSE_VALUES = frozenset({"0", "false", "no", "off"})


@dataclass(frozen=True)
class Config:
    """解決済み設定。生成後は不変。"""

    address: str | None = None
    transport: str | None = None  # "lan" | "usb" | None(addressから推定)
    port: int | None = None  # None ならプロファイル既定
    timeout_s: float = DEFAULT_TIMEOUT_S
    screenshot_dir: Path = field(default_factory=Path.cwd)
    allowed_dirs: tuple[Path, ...] = ()
    waveform_max_points: int = DEFAULT_WAVEFORM_MAX_POINTS
    raw_scpi: bool = False
    log_level: str = DEFAULT_LOG_LEVEL
    audit_log: Path | None = None  # None は「監査ログ無効」の意(load_configは既定で有効)


def _invalid(key: str, value: Any, reason: str) -> ScopeError:
    return ScopeError(
        ErrorCode.INVALID_PARAMETER,
        f"設定 {key} の値が不正です: {reason}",
        {"key": key, "value": repr(value)},
    )


def _read_toml(path: Path) -> dict:
    if not path.is_file():
        raise _invalid("config", str(path), "設定ファイルが見つかりません")
    try:
        with path.open("rb") as fp:
            data = tomllib.load(fp)
    except tomllib.TOMLDecodeError as exc:
        raise _invalid("config", str(path), f"TOMLの解析に失敗しました: {exc}") from exc
    if not isinstance(data, dict):  # pragma: no cover - tomllibは常にdictを返す
        raise _invalid("config", str(path), "トップレベルがテーブルではありません")
    return data


class _Source:
    """環境変数 > ファイル の順で生の値を引く。"""

    def __init__(self, env: Mapping[str, str], file_values: Mapping[str, Any]) -> None:
        self._env = env
        self._file = file_values

    def get(self, key: str) -> Any | None:
        raw = self._env.get(ENV_PREFIX + key.upper())
        if raw is not None and raw.strip() != "":
            return raw
        value = self._file.get(key)
        if isinstance(value, str) and value.strip() == "":
            return None
        return value


def _as_str(src: _Source, key: str) -> str | None:
    value = src.get(key)
    if value is None:
        return None
    if not isinstance(value, str):
        raise _invalid(key, value, "文字列である必要があります")
    return value.strip()


def _as_int(src: _Source, key: str) -> int | None:
    value = src.get(key)
    if value is None:
        return None
    if isinstance(value, bool):
        raise _invalid(key, value, "整数である必要があります")
    if isinstance(value, int):
        return value
    if isinstance(value, str):
        try:
            return int(value.strip())
        except ValueError as exc:
            raise _invalid(key, value, "整数として解釈できません") from exc
    raise _invalid(key, value, "整数である必要があります")


def _as_float(src: _Source, key: str) -> float | None:
    value = src.get(key)
    if value is None:
        return None
    if isinstance(value, bool):
        raise _invalid(key, value, "数値である必要があります")
    if isinstance(value, (int, float)):
        return float(value)
    if isinstance(value, str):
        try:
            return float(value.strip())
        except ValueError as exc:
            raise _invalid(key, value, "数値として解釈できません") from exc
    raise _invalid(key, value, "数値である必要があります")


def _as_bool(src: _Source, key: str) -> bool | None:
    value = src.get(key)
    if value is None:
        return None
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        lowered = value.strip().lower()
        if lowered in _TRUE_VALUES:
            return True
        if lowered in _FALSE_VALUES:
            return False
        raise _invalid(key, value, "真偽値として解釈できません")
    raise _invalid(key, value, "真偽値である必要があります")


def _as_path(src: _Source, key: str) -> Path | None:
    value = _as_str(src, key)
    if value is None:
        return None
    return _resolve_path(value)


def _resolve_path(value: str) -> Path:
    return Path(value).expanduser().resolve()


def _as_path_list(src: _Source, key: str) -> tuple[Path, ...]:
    value = src.get(key)
    if value is None:
        return ()
    if isinstance(value, str):
        parts = [part.strip() for part in value.split(os.pathsep) if part.strip()]
    elif isinstance(value, list):
        parts = []
        for item in value:
            if not isinstance(item, str):
                raise _invalid(key, item, "パス文字列である必要があります")
            if item.strip():
                parts.append(item.strip())
    else:
        raise _invalid(key, value, "パスのリストである必要があります")
    return tuple(_resolve_path(part) for part in parts)


def _choice(key: str, value: str | None, allowed: tuple[str, ...]) -> str | None:
    if value is None:
        return None
    lowered = value.lower()
    if lowered not in allowed:
        raise _invalid(key, value, f"{' / '.join(allowed)} のいずれかである必要があります")
    return lowered


def _audit_log(src: _Source, env: Mapping[str, str]) -> Path | None:
    """監査ログの出力先。既定は有効で、`off` 等の偽値でのみ無効化する(9章)。

    未設定・空文字は XDG state 配下の既定パス。`0/false/no/off` は None(無効)。
    それ以外はパスとして解釈する。
    """
    value = _as_str(src, "audit_log")
    if value is None:
        state_home = env.get("XDG_STATE_HOME") or "~/.local/state"
        return _resolve_path(state_home) / "rigol-oscilloscope-mcp" / "audit.jsonl"
    if value.lower() in _FALSE_VALUES:
        return None
    return _resolve_path(value)


def _pwd_dir(env: Mapping[str, str]) -> Path | None:
    """PWD環境変数が指す実行ディレクトリ(使えなければ None)。

    uv run --directory 等で cwd が書き換わっても、spawn元のシェル/ホストが
    継承させた PWD にはユーザーの実行ディレクトリが残る。os.chdir() は PWD を
    更新しないためこれが使える。GUIホストが PWD=/ で起動するようなケースは
    書き込み可否チェックで弾く。
    """
    raw = env.get("PWD")
    if not raw:
        return None
    path = Path(raw)
    if not path.is_absolute():
        return None
    path = path.resolve()
    if not path.is_dir() or not os.access(path, os.W_OK):
        return None
    return path


def load_config(
    env: Mapping[str, str] | None = None,
    config_file: Path | None = None,
) -> Config:
    """環境変数 > TOMLファイル > デフォルト の順に設定を解決する。

    env が None なら os.environ を使う。config_file が None のときは
    env の RIGOL_MCP_CONFIG を参照し、それも無ければファイルは読まない。
    不正値は ScopeError(INVALID_PARAMETER) を送出する。

    screenshot_dir だけは
    RIGOL_MCP_SCREENSHOT_DIR(env/TOML) > PWD環境変数 > Path.cwd()
    の順で解決する(9章)。allowed_dirs には明示指定に加えて screenshot_dir を
    必ず含める。プロセスのカレントディレクトリは自動追加しない(uv run
    --directory 起動ではサーバー自身のプロジェクトを指すため)。
    """
    env = os.environ if env is None else env

    if config_file is None:
        raw_config_path = env.get(CONFIG_ENV_VAR)
        if raw_config_path is not None and raw_config_path.strip():
            config_file = Path(raw_config_path.strip()).expanduser()

    file_values = _read_toml(config_file) if config_file is not None else {}
    src = _Source(env, file_values)

    address = _as_str(src, "address")
    transport = _choice("transport", _as_str(src, "transport"), VALID_TRANSPORTS)
    log_level = _choice("log_level", _as_str(src, "log_level"), VALID_LOG_LEVELS)

    port = _as_int(src, "port")
    if port is not None and not (1 <= port <= 65535):
        raise _invalid("port", port, "1〜65535 の範囲である必要があります")

    timeout_s = _as_float(src, "timeout_s")
    if timeout_s is not None and timeout_s <= 0:
        raise _invalid("timeout_s", timeout_s, "正の数である必要があります")

    waveform_max_points = _as_int(src, "waveform_max_points")
    if waveform_max_points is not None and waveform_max_points <= 0:
        raise _invalid(
            "waveform_max_points", waveform_max_points, "正の整数である必要があります"
        )

    raw_scpi = _as_bool(src, "raw_scpi")

    screenshot_dir = _as_path(src, "screenshot_dir") or _pwd_dir(env) or Path.cwd().resolve()
    # 許可ルートには保存先を必ず含める(Requirements 9章)。プロセスのカレントは
    # --directory 起動でサーバーのプロジェクトになりうるため自動追加しない。
    # dict は挿入順を保つため、順序を保った重複除去になる
    allowed_dirs = tuple(
        dict.fromkeys(_as_path_list(src, "allowed_dirs") + (screenshot_dir,))
    )

    return Config(
        address=address,
        transport=transport,
        port=port,
        timeout_s=DEFAULT_TIMEOUT_S if timeout_s is None else timeout_s,
        screenshot_dir=screenshot_dir,
        allowed_dirs=allowed_dirs,
        waveform_max_points=(
            DEFAULT_WAVEFORM_MAX_POINTS
            if waveform_max_points is None
            else waveform_max_points
        ),
        raw_scpi=False if raw_scpi is None else raw_scpi,
        log_level=DEFAULT_LOG_LEVEL if log_level is None else log_level,
        audit_log=_audit_log(src, env),
    )
