"""状態の一括取得(tools.md 2章 / Requirements.md 8.1)。

server層がそのままToolレスポンスにできるよう、返却は**JSONプリミティブのみ**の
dict に揃える(dataclass や None 以外の非JSON値を混ぜない)。

全取得は約39クエリ・実測約1.3秒かかるため、`sections` による絞り込みを提供し、
**指定されていないセクションのクエリは1件も送らない**ことを保証する。
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import asdict

from ..driver.scope import ScopeDriver
from ..errors import ErrorCode, ScopeError

VALID_SECTIONS = ("channels", "timebase", "trigger", "acquisition")

# :TRIGger:STATus? の生値がこれなら停止中(TD / WAIT / AUTO 等は動作中)
STOPPED_STATUS = "STOP"


def get_channel_dict(driver: ScopeDriver, channel: str) -> dict:
    """1チャンネル分の状態。"""
    return asdict(driver.get_channel(channel))


def get_timebase_dict(driver: ScopeDriver) -> dict:
    """水平軸の状態。"""
    return asdict(driver.get_timebase())


def get_trigger_dict(driver: ScopeDriver) -> dict:
    """トリガ設定と状態。"""
    return asdict(driver.get_trigger())


def get_acquisition_dict(driver: ScopeDriver) -> dict:
    """収集状態。`running` はトリガ状態の生値から導く(専用クエリを増やさない)。"""
    status = driver.get_trigger_status()
    return {"trigger_status": status, "running": status != STOPPED_STATUS}


def _channels_dict(driver: ScopeDriver) -> dict:
    return {
        f"CH{n}": get_channel_dict(driver, f"CH{n}")
        for n in range(1, driver.analog_channels + 1)
    }


_SECTION_GETTERS: dict[str, Callable[[ScopeDriver], dict]] = {
    "channels": _channels_dict,
    "timebase": get_timebase_dict,
    "trigger": get_trigger_dict,
    "acquisition": get_acquisition_dict,
}


def _validate(sections: list[str]) -> None:
    """1件でも不正なら、クエリを送る前に失敗する。"""
    if not sections:
        raise ScopeError(
            ErrorCode.INVALID_PARAMETER,
            f"sections is empty (omit it for all sections; valid: {list(VALID_SECTIONS)})",
            {"invalid_sections": [], "valid": list(VALID_SECTIONS)},
        )
    invalid = [s for s in sections if s not in _SECTION_GETTERS]
    if invalid:
        raise ScopeError(
            ErrorCode.INVALID_PARAMETER,
            f"Unknown section: {invalid} (valid: {list(VALID_SECTIONS)})",
            {"invalid_sections": invalid, "valid": list(VALID_SECTIONS)},
        )


def get_state(driver: ScopeDriver, sections: list[str] | None = None) -> dict:
    """主要設定を一括取得する。`sections` 省略時は全セクション。

    返却は指定セクションのキーのみを持ち、キー順は VALID_SECTIONS に従う
    (引数の順序では変わらない)。
    """
    if sections is None:
        selected = list(VALID_SECTIONS)
    else:
        _validate(sections)
        selected = [s for s in VALID_SECTIONS if s in sections]
    return {section: _SECTION_GETTERS[section](driver) for section in selected}
