"""保存先パスの確定と許可ルート検証(Requirements.md 8.4 / 9章)。

ファイルシステムへの書き込みは「許可ルート配下」に限定する。判定は
`expanduser` → `resolve(strict=False)` の順に正規化した**絶対パス**に対して
行うため、`../` を含む相対パスやシンボリックリンク経由の脱出も遮断される。

相対パスはプロセスのカレントディレクトリではなく**デフォルト保存先**
(`config.screenshot_dir` = 実行ディレクトリ)を基準に解決する。`uv run
--directory` 起動では cwd がサーバーのプロジェクトになるため、cwd 基準だと
ユーザーの意図しない場所へ書いてしまうためである。許可ルートも cwd を含めず、
デフォルト保存先・設定の allowed_dirs・一時ディレクトリのみとする
(Requirements.md 9章)。
"""

from __future__ import annotations

import os
import tempfile
from pathlib import Path

from ..config import Config
from ..errors import ErrorCode, ScopeError

_SEPARATORS = tuple(sep for sep in (os.sep, os.altsep) if sep)

_ALLOWED_DIRS_ENV = "RIGOL_MCP_ALLOWED_DIRS"
_HINT = f"To add an allowed root, set the {_ALLOWED_DIRS_ENV} environment variable"


def _temp_roots() -> tuple[Path, ...]:
    """常時許可する一時ディレクトリ(OS差は tempfile が吸収する)。"""
    roots = [Path(tempfile.gettempdir()).resolve()]
    if os.name == "posix":
        conventional = Path("/tmp")  # 許可判定に使うだけで、ここへは書かない
        if conventional.exists():
            roots.append(conventional.resolve())
    return tuple(roots)


def allowed_roots(config: Config) -> tuple[Path, ...]:
    """書き込みを許可するルートの一覧(順序を保った重複除去)。

    設定の allowed_dirs に加え、デフォルト保存先と一時ディレクトリを常に含める
    (Requirements.md 9章)。カレントディレクトリは含めない。
    """
    candidates = (*config.allowed_dirs, config.screenshot_dir, *_temp_roots())
    # dict は挿入順を保つため、順序を保った重複除去になる
    return tuple(
        dict.fromkeys(Path(c).expanduser().resolve() for c in candidates)
    )


def _is_directory_argument(raw: str, expanded: Path) -> bool:
    """ディレクトリ指定(既存ディレクトリ、または区切り文字終わり)か。"""
    return raw.endswith(_SEPARATORS) or expanded.is_dir()


def _check_allowed(resolved: Path, config: Config) -> None:
    roots = allowed_roots(config)
    if any(resolved.is_relative_to(root) for root in roots):
        return
    raise ScopeError(
        ErrorCode.INVALID_PARAMETER,
        f"Destination is outside the allowed roots: {resolved} ({_HINT})",
        {
            "path": str(resolved),
            "allowed_roots": [str(root) for root in roots],
            "hint": _HINT,
        },
    )


def resolve_write_path(
    path_arg: str | None,
    config: Config,
    default_stem: str,
    extension: str,
) -> Path:
    """保存先の絶対パスを確定する(許可ルート外なら INVALID_PARAMETER)。

    - `path_arg` が None ならデフォルト保存先 + `{default_stem}.{extension}`
    - 相対パスはデフォルト保存先(実行ディレクトリ)を基準に解決する
    - 既存ディレクトリ / 区切り文字終わりなら、そのディレクトリ配下の既定名
    - それ以外はファイルパス扱い(拡張子が無ければ `.{extension}` を付与)

    許可ルート内であることを確認したうえで、親ディレクトリが無ければ作成する。
    """
    default_name = f"{default_stem}.{extension}"

    if path_arg is None:
        target = Path(config.screenshot_dir).expanduser() / default_name
    else:
        expanded = Path(path_arg).expanduser()
        if not expanded.is_absolute():
            expanded = Path(config.screenshot_dir).expanduser() / expanded
        if _is_directory_argument(path_arg, expanded):
            target = expanded / default_name
        elif expanded.suffix:
            target = expanded
        else:
            target = expanded.with_name(f"{expanded.name}.{extension}")

    resolved = target.resolve()
    _check_allowed(resolved, config)
    resolved.parent.mkdir(parents=True, exist_ok=True)
    return resolved
