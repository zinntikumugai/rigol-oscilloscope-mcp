"""confirmトークン(Requirements.md 6.2 確認フロー)。

RESTRICTED_WRITE / DANGEROUS_WRITE はホストUIに依存せず2段階呼び出しで承認する:

1. 1回目: 実行せず USER_CONFIRMATION_REQUIRED と confirm_token を返す
2. 2回目: 同一引数 + confirm_token で実行

トークンは短寿命・単回有効で、Tool名と引数にバインドされる。
"""

from __future__ import annotations

import hashlib
import json
import secrets
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass

from ..errors import ErrorCode, ScopeError

DEFAULT_TTL_S = 300.0
TOKEN_BYTES = 16

#: confirm_token 自身はバインド対象から除外する(2回目の呼び出しで増える引数のため)
CONFIRM_TOKEN_KEY = "confirm_token"

#: LLMへの固定指示文言(Requirements.md 6.2)。文言の変更は安全要件の変更にあたる。
CONFIRM_INSTRUCTION = (
    "このトークンを使う前に、必ず人間の利用者へこの操作を実行してよいか確認してください。"
    "利用者の明示的な同意なしにトークンを使用してはいけません。"
)


@dataclass(frozen=True)
class ConfirmRequest:
    """1回目の呼び出しで返す確認要求(6.2)。"""

    token: str
    tool: str
    description: str
    risk: str
    expires_in_s: float
    instruction: str = CONFIRM_INSTRUCTION


@dataclass(frozen=True)
class _Entry:
    tool: str
    args_digest: str
    generation: int
    expires_at: float


def _args_digest(args: dict) -> str:
    """引数のcanonical JSON(sort_keys)のSHA-256。confirm_token キーは除外。"""
    payload = {key: value for key, value in args.items() if key != CONFIRM_TOKEN_KEY}
    canonical = json.dumps(
        payload,
        sort_keys=True,
        ensure_ascii=False,
        separators=(",", ":"),
        default=repr,
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _rejected(reason: str, tool: str, message: str) -> ScopeError:
    return ScopeError(
        ErrorCode.USER_CONFIRMATION_REQUIRED,
        message,
        {"reason": reason, "tool": tool},
    )


class ConfirmTokenStore:
    """confirmトークンの発行・検証・単回消費。

    FastMCPはsync toolを並行スレッドで実行しうるため、自前のLockで保護する。
    TTLは clock()(既定 time.monotonic)基準。
    """

    def __init__(
        self,
        ttl_s: float = DEFAULT_TTL_S,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._ttl_s = ttl_s
        self._clock = clock
        self._lock = threading.Lock()
        self._entries: dict[str, _Entry] = {}

    @property
    def ttl_s(self) -> float:
        return self._ttl_s

    def issue(
        self,
        tool: str,
        args: dict,
        description: str,
        risk: str,
        generation: int = 0,
    ) -> ConfirmRequest:
        """操作内容にバインドした短寿命トークンを発行する。"""
        token = secrets.token_urlsafe(TOKEN_BYTES)
        entry = _Entry(
            tool=tool,
            args_digest=_args_digest(args),
            generation=generation,
            expires_at=self._clock() + self._ttl_s,
        )
        with self._lock:
            self._purge_expired()
            self._entries[token] = entry
        return ConfirmRequest(
            token=token,
            tool=tool,
            description=description,
            risk=risk,
            expires_in_s=self._ttl_s,
            instruction=CONFIRM_INSTRUCTION,
        )

    def consume(
        self,
        token: str,
        tool: str,
        args: dict,
        generation: int = 0,
    ) -> None:
        """トークンを検証して単回消費する。

        検証失敗は ScopeError(USER_CONFIRMATION_REQUIRED)。失敗時も
        トークンは無効化される(総当たり・再試行の防止)。
        """
        now = self._clock()
        with self._lock:
            entry = self._entries.pop(token, None)
            self._purge_expired(now)

        if entry is None:
            raise _rejected(
                "unknown_token", tool, "confirm_token が無効か、既に使用されています"
            )
        if entry.expires_at <= now:
            raise _rejected(
                "expired", tool, "confirm_token の有効期限が切れています"
            )
        if entry.tool != tool:
            raise _rejected(
                "tool_mismatch",
                tool,
                f"confirm_token は別の操作({entry.tool})に対して発行されています",
            )
        if entry.generation != generation:
            raise _rejected(
                "generation_mismatch",
                tool,
                "confirm_token の発行後に接続が切り替わっています",
            )
        if entry.args_digest != _args_digest(args):
            raise _rejected(
                "args_mismatch",
                tool,
                "confirm_token の発行時と引数が異なります",
            )

    def _purge_expired(self, now: float | None = None) -> None:
        """期限切れエントリを掃除する(呼び出し側でLock保持)。"""
        deadline = self._clock() if now is None else now
        expired = [
            token
            for token, entry in self._entries.items()
            if entry.expires_at <= deadline
        ]
        for token in expired:
            del self._entries[token]
