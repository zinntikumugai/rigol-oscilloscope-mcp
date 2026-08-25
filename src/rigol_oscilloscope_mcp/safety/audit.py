"""監査ログ(Requirements.md 7.6)。

書き込み操作の Before / Action / After と confirmトークンの発行・消費を
JSONL(1操作1行)で追記する。監査ログの失敗が操作自体を止めてはならないため、
書き込み例外は伝播させず stderr へ警告する。
"""

from __future__ import annotations

import hashlib
import json
import sys
import threading
from datetime import datetime, timezone
from pathlib import Path

_TOKEN_DIGEST_LEN = 16


def token_digest(token: str) -> str:
    """confirmトークンの監査用ダイジェスト(SHA-256 先頭16hex)。

    監査ログにトークン本体を残さないための一方向表現。
    """
    return hashlib.sha256(token.encode("utf-8")).hexdigest()[:_TOKEN_DIGEST_LEN]


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


class AuditLogger:
    """JSONL監査ログの追記器。

    path=None のときは記録を行わない(全メソッドが no-op)。
    """

    def __init__(self, path: Path | None) -> None:
        self._path = path
        self._lock = threading.Lock()

    @property
    def enabled(self) -> bool:
        """監査ログが有効(出力先が設定済み)かどうか。"""
        return self._path is not None

    @property
    def path(self) -> Path | None:
        return self._path

    def record(
        self,
        tool: str,
        requested: dict,
        before: dict | None,
        after: dict | None,
        result: str,
        detail: dict | None = None,
    ) -> None:
        """1操作を1行のJSONとして追記する(Requirements.md 7.6)。"""
        self._write(
            {
                "timestamp": _utc_now_iso(),
                "tool": tool,
                "requested": requested,
                "before": before,
                "after": after,
                "result": result,
                "detail": detail,
            }
        )

    def record_confirm(self, event: str, tool: str, token_digest: str) -> None:
        """confirmトークンの発行/消費/拒否を記録する(Requirements.md 6.2)。

        event: "issued" | "consumed" | "rejected"。
        トークン本体は記録せず、ダイジェストのみを残す。
        """
        self._write(
            {
                "timestamp": _utc_now_iso(),
                "event": event,
                "tool": tool,
                "token_digest": token_digest,
            }
        )

    def _write(self, entry: dict) -> None:
        path = self._path
        if path is None:
            return
        try:
            line = json.dumps(entry, ensure_ascii=False, default=str) + "\n"
            with self._lock:
                path.parent.mkdir(parents=True, exist_ok=True)
                with path.open("a", encoding="utf-8") as fp:
                    fp.write(line)
        except Exception as exc:  # 監査ログ障害で操作を止めない
            print(
                f"[warn] audit log write failed ({path}): {exc!r}",
                file=sys.stderr,
            )
