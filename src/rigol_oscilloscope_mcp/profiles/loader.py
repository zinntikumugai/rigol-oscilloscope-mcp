"""機種プロファイルYAMLの読み込みと3層解決(docs/device-profiles.md 1章)。

- 読み込み: パッケージ同梱の `data/<name>.yaml` を `inherits` チェーンで深くマージ
- 解決: `*IDN?` のモデル文字列に対し verified → family → 汎用 の順で決定
"""

from __future__ import annotations

import re
from importlib import resources
from typing import Any

import yaml

from ..errors import ErrorCode, ScopeError
from ..models import IdnInfo
from .profile import Profile, ResolvedProfile

DATA_PACKAGE = "rigol_oscilloscope_mcp.profiles"
DATA_DIRNAME = "data"
SUFFIX = ".yaml"

GENERIC_PROFILE = "rigol-generic"
VENDOR_KEYWORD = "RIGOL"

# 解決の優先順位(小さいほど優先)
_CONFIDENCE_ORDER = {"verified": 0, "family": 1, "generic": 2}
_BLOCKS = ("capabilities", "dialect", "limits")


def _data_dir() -> Any:
    """同梱プロファイルのディレクトリ(zip配布でも読める Traversable)。"""
    return resources.files(DATA_PACKAGE).joinpath(DATA_DIRNAME)


def _invalid(message: str, **detail: Any) -> ScopeError:
    return ScopeError(ErrorCode.INVALID_PARAMETER, message, detail or None)


def _read_raw(directory: Any, name: str) -> dict:
    """`<name>.yaml` を1枚だけ読む(継承は解決しない)。"""
    entry = directory.joinpath(f"{name}{SUFFIX}")
    if not entry.is_file():
        raise _invalid(
            f"プロファイル '{name}' が見つかりません",
            name=name,
            available=_available_profiles_from(directory),
        )
    try:
        data = yaml.safe_load(entry.read_text(encoding="utf-8"))
    except yaml.YAMLError as exc:
        raise _invalid(f"プロファイル '{name}' のYAMLが不正です: {exc}", name=name)
    if data is None:
        data = {}
    if not isinstance(data, dict):
        raise _invalid(
            f"プロファイル '{name}' のトップレベルはマッピングである必要があります",
            name=name,
        )
    return data


def _deep_merge(base: dict, override: dict) -> dict:
    """子が優先の深いマージ。dictは再帰、list/スカラは置換。"""
    merged = dict(base)
    for key, value in override.items():
        current = merged.get(key)
        if isinstance(current, dict) and isinstance(value, dict):
            merged[key] = _deep_merge(current, value)
        else:
            merged[key] = value
    return merged


def _merged_raw(directory: Any, name: str, chain: tuple[str, ...] = ()) -> dict:
    """`inherits` チェーンを親から順に適用したマージ済みマッピングを返す。"""
    if name in chain:
        cycle = " -> ".join([*chain, name])
        raise _invalid(
            f"プロファイルの継承が循環しています: {cycle}", name=name, cycle=cycle
        )

    raw = _read_raw(directory, name)
    parent = raw.get("inherits")
    if parent is None:
        return raw
    if not isinstance(parent, str):
        raise _invalid(
            f"プロファイル '{name}' の inherits はプロファイル名(文字列)である必要があります",
            name=name,
        )
    base = _merged_raw(directory, parent, (*chain, name))
    return _deep_merge(base, raw)


def _to_profile(name: str, raw: dict) -> Profile:
    blocks: dict[str, dict] = {}
    for block in _BLOCKS:
        value = raw.get(block)
        if value is None:
            value = {}
        if not isinstance(value, dict):
            raise _invalid(
                f"プロファイル '{name}' の {block} はマッピングである必要があります",
                name=name,
            )
        blocks[block] = value

    confidence = raw.get("confidence", "generic")
    if not isinstance(confidence, str):
        raise _invalid(
            f"プロファイル '{name}' の confidence は文字列である必要があります",
            name=name,
        )
    return Profile(name=name, confidence=confidence, **blocks)


def _load_profile_from(directory: Any, name: str) -> Profile:
    """ディレクトリを注入できる `load_profile`(テスト・将来のユーザー拡張用)。"""
    return _to_profile(name, _merged_raw(directory, name))


def _available_profiles_from(directory: Any) -> list[str]:
    return sorted(
        entry.name[: -len(SUFFIX)]
        for entry in directory.iterdir()
        if entry.name.endswith(SUFFIX)
    )


def _matches(profile_name: str, pattern: Any, model: str) -> bool:
    if pattern is None:
        return False  # match を持たないプロファイルはフォールバック専用
    if not isinstance(pattern, str):
        raise _invalid(
            f"プロファイル '{profile_name}' の match は正規表現文字列である必要があります",
            name=profile_name,
        )
    try:
        return re.search(pattern, model) is not None
    except re.error as exc:
        raise _invalid(
            f"プロファイル '{profile_name}' の match が不正な正規表現です: {exc}",
            name=profile_name,
        )


def _resolve_profile_from(directory: Any, idn: IdnInfo) -> ResolvedProfile:
    """ディレクトリを注入できる `resolve_profile`。"""
    model = idn.model.strip()
    candidates: list[tuple[int, str, Profile]] = []

    for name in _available_profiles_from(directory):
        raw = _merged_raw(directory, name)
        if not _matches(name, raw.get("match"), model):
            continue
        profile = _to_profile(name, raw)
        rank = _CONFIDENCE_ORDER.get(profile.confidence, len(_CONFIDENCE_ORDER))
        candidates.append((rank, name, profile))

    if candidates:
        profile = min(candidates, key=lambda c: (c[0], c[1]))[2]
    else:
        profile = _load_profile_from(directory, GENERIC_PROFILE)

    unsupported_vendor = VENDOR_KEYWORD not in idn.manufacturer.upper()
    return ResolvedProfile(profile=profile, unsupported_vendor=unsupported_vendor)


def load_profile(name: str) -> Profile:
    """同梱プロファイルを継承解決して返す。未知の名前は INVALID_PARAMETER。"""
    return _load_profile_from(_data_dir(), name)


def resolve_profile(idn: IdnInfo) -> ResolvedProfile:
    """`*IDN?` の解析結果から、モデル完全一致 → ファミリ → 汎用の順に解決する。"""
    return _resolve_profile_from(_data_dir(), idn)


def available_profiles() -> list[str]:
    """同梱プロファイル名の一覧(拡張子なし・ソート済み)。"""
    return _available_profiles_from(_data_dir())
