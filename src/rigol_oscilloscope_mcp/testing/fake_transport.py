"""FakeScope を Transport として差し込むアダプタ。"""

from __future__ import annotations

from ..errors import ErrorCode, ScopeError
from ..transport.blocks import parse_block
from .fake_scope import FakeScope, SilentTimeout

__all__ = ["FakeTransport"]


class FakeTransport:
    """`Transport` プロトコルを満たすインプロセス実装。

    実機のクライアント側挙動を再現する:

    - 無応答(`SilentTimeout`)は `TIMEOUT` の `ScopeError` になる
    - 書き込みは応答を待たないためタイムアウトしない(エラーキューにだけ残る)
    - バイナリ応答は `transport.blocks.parse_block` で解凍し、末尾改行を捨てる
    """

    def __init__(self, scope: FakeScope) -> None:
        self.scope = scope
        self._open = False

    # -- ライフサイクル ---------------------------------------------------

    def open(self) -> None:
        self._open = True

    def close(self) -> None:
        self._open = False

    @property
    def is_open(self) -> bool:
        return self._open

    # -- 入出力 -----------------------------------------------------------

    def write(self, command: str) -> None:
        self._require_open(command)
        try:
            self.scope.handle(command)
        except SilentTimeout:
            # 実機の書き込みは応答を待たないため、タイムアウトは起きない。
            # エラーはキューに残り、呼び出し側の :SYSTem:ERRor? で検出される。
            pass

    def query(self, command: str, timeout_s: float | None = None) -> str:
        # 実機のLAN/USBはいずれも1行しか読まない。フェイクも同じ意味論にして
        # 「複数行応答を query で読む」実装ミスがテストで露見するようにする。
        return self._respond(command, timeout_s).decode("ascii").split("\n", 1)[0]

    def query_lines(self, command: str, timeout_s: float | None = None) -> list[str]:
        text = self._respond(command, timeout_s).decode("ascii")
        head = text.split("\n\n", 1)[0].strip("\n")
        return head.split("\n") if head else []

    def query_binary(self, command: str, timeout_s: float | None = None) -> bytes:
        buffer = bytearray(self._respond(command, timeout_s))

        def read(n: int) -> bytes:
            chunk = bytes(buffer[:n])
            del buffer[:n]
            return chunk

        payload = parse_block(read)
        if buffer[:1] == b"\n":  # ブロック末尾の改行はトランスポートの責務で捨てる
            del buffer[:1]
        return payload

    # -- 内部 -------------------------------------------------------------

    def _require_open(self, command: str) -> None:
        if not self._open:
            raise ScopeError(
                ErrorCode.DEVICE_DISCONNECTED,
                "transport is not connected",
                {"command": command},
            )

    def _respond(self, command: str, timeout_s: float | None) -> bytes:
        self._require_open(command)
        try:
            response = self.scope.handle(command)
        except SilentTimeout as exc:
            raise self._timeout(command, timeout_s, str(exc)) from exc
        if response is None:
            # 応答を返さないコマンドを問い合わせた場合、実機は無応答のまま。
            raise self._timeout(command, timeout_s, "command produces no response")
        return response

    def _timeout(
        self, command: str, timeout_s: float | None, reason: str
    ) -> ScopeError:
        return ScopeError(
            ErrorCode.TIMEOUT,
            f"the device did not respond: {command}",
            {"command": command, "timeout_s": timeout_s, "reason": reason},
        )
