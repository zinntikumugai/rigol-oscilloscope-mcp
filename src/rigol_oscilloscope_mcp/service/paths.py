"""保存先パスの確定と許可ルート検証(Requirements.md 8.4 / 9章)。

ファイルシステムへの書き込みは「許可ルート配下」に限定する。判定は
`expanduser` → `resolve(strict=False)` の順に正規化した**絶対パス**に対して
行うため、`../` を含む相対パスやシンボリックリンク経由の脱出も遮断される。
"""

from __future__ import annotations

import os
from pathlib import Path

from ..config import Config
from ..errors import ErrorCode, ScopeError

_SEPARATORS = tuple(sep for sep in (os.sep, os.altsep) if sep)


def allowed_roots(config: Config) -> tuple[Path, ...]:
    """書き込みを許可するルートの一覧(順序を保った重複除去)。

    設定の allowed_dirs に加え、デフォルト保存先とカレントディレクトリを
    常に含める(Requirements.md 9章)。
    """
    candidates = (*config.allowed_dirs, config.screenshot_dir, Path.cwd())
    roots: dict[Path, None] = {}
    for candidate in candidates:
        roots.setdefault(Path(candidate).expanduser().resolve(), None)
    return tuple(roots)


def _is_directory_argument(raw: str, expanded: Path) -> bool:
    """ディレクトリ指定(既存ディレクトリ、または区切り文字終わり)か。"""
    return raw.endswith(_SEPARATORS) or expanded.is_dir()


def _check_allowed(resolved: Path, config: Config) -> None:
    roots = allowed_roots(config)
    if any(resolved.is_relative_to(root) for root in roots):
        return
    raise ScopeError(
        ErrorCode.INVALID_PARAMETER,
        f"保存先が許可ルートの外です: {resolved}",
        {"path": str(resolved), "allowed_roots": [str(root) for root in roots]},
    )


def resolve_write_path(
    path_arg: str | None,
    config: Config,
    default_stem: str,
    extension: str,
) -> Path:
    """保存先の絶対パスを確定する(許可ルート外なら INVALID_PARAMETER)。

    - `path_arg` が None ならデフォルト保存先 + `{default_stem}.{extension}`
    - 既存ディレクトリ / 区切り文字終わりなら、そのディレクトリ配下の既定名
    - それ以外はファイルパス扱い(拡張子が無ければ `.{extension}` を付与)

    許可ルート内であることを確認したうえで、親ディレクトリが無ければ作成する。
    """
    default_name = f"{default_stem}.{extension}"

    if path_arg is None:
        target = Path(config.screenshot_dir).expanduser() / default_name
    else:
        expanded = Path(path_arg).expanduser()
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
