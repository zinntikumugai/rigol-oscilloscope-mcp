"""デコード結果(イベントテーブル)の取得(tools.md 6章 get_decode_result)。

ドライバが返す表をそのまま渡し、**件数の絞り込みだけをホスト側で行う**
(機器側に件数指定の手段が無く、長い表はコンテキストを食い潰すため)。
"""

from __future__ import annotations

from ..driver.scope import ScopeDriver
from ..errors import ErrorCode, ScopeError


def get_decode_result(
    driver: ScopeDriver, bus: int = 1, max_events: int | None = None
) -> dict:
    """デコードされたイベントテーブルを読む(read-only)。"""
    if max_events is not None and (
        isinstance(max_events, bool) or not isinstance(max_events, int) or max_events < 1
    ):
        raise ScopeError(
            ErrorCode.INVALID_PARAMETER,
            f"max_events must be 1 or greater: {max_events!r}",
            {"max_events": max_events},
        )

    result = driver.get_decode_events(bus)
    events = result["events"]
    total = len(events)
    truncated = max_events is not None and total > max_events
    warnings = list(result["warnings"])
    if truncated:
        events = events[:max_events]
        warnings.append(
            f"only the first {max_events} of {total} events are returned "
            "(raise max_events to see more)"
        )

    return {
        "bus": result["bus"],
        "protocol": result["protocol"],
        "columns": result["columns"],
        "events": events,
        # 打ち切り前の総件数(`len(events)` と異なるなら打ち切られている)
        "event_count": total,
        "truncated": truncated,
        "warnings": warnings,
    }
