"""LANトランスポート(raw socket SCPI, 既定ポート5555)。

Rigolのraw socketポートは行志向のSCPIを話す。本実装は `Transport` Protocol に
適合し、終端改行の付与・除去と、バイナリブロック末尾改行の読み捨てを担う。

タイムアウトは「操作全体のデッドライン」で管理する。recv単位のタイムアウトだと
97KBのPNGが分割・低速配送された場合に実時間が timeout_s × recv回数 まで延びる
ため、`time.monotonic()` 基準の期限を1操作につき1つだけ持ち、recvごとに残り時間を
`socket.settimeout()` へ反映する。
"""

from __future__ import annotations

import socket
import time

from ..errors import ErrorCode, ScopeError
from .blocks import parse_block

DEFAULT_PORT = 5555
_RECV_CHUNK = 65536

# 接続直後に送る回復用の空行(理由は `LanTransport.open` を参照)
_RECOVERY_FLUSH = b"\n"


class LanTransport:
    """raw socket(TCP)によるSCPIトランスポート。"""

    def __init__(
        self, host: str, port: int = DEFAULT_PORT, timeout_s: float = 5.0
    ) -> None:
        self.host = host
        self.port = port
        self.timeout_s = timeout_s
        self._sock: socket.socket | None = None
        self._buf = bytearray()

    # --- 接続管理 ---------------------------------------------------------

    def open(self) -> None:
        """接続し、回復用の空行を1本送る。接続済みなら何もしない。

        実機MHO98では、未定義ヘッダのクエリ(例 `:MEASure:VPP?`)を1回送ると
        SCPIサーバー全体が沈黙し、以後 `*IDN?` にも応答しなくなる。TCP接続だけは
        成功し続け、プロセス再起動・再接続でも回復しないが、**空行 `\\n` を1本
        送ると即座に回復する**ことを実機で確認している。そのため接続のたびに
        空行を送り、wedge状態のまま使い始めることを防ぐ。

        健全な機器には無害である(空コマンドはSCPIパーサに無視されるか、
        エラーになっても接続シーケンスのエラーキューdrainで掃除される)。
        """
        if self._sock is not None:
            return
        try:
            sock = socket.create_connection(
                (self.host, self.port), timeout=self.timeout_s
            )
        except OSError as exc:  # gaierror / timeout / ConnectionRefusedError を含む
            raise ScopeError(
                ErrorCode.DEVICE_NOT_FOUND,
                f"cannot connect to {self.host}:{self.port}: {exc}",
                {"host": self.host, "port": self.port},
            ) from exc
        sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
        try:
            sock.sendall(_RECOVERY_FLUSH)
        except OSError as exc:
            sock.close()
            raise ScopeError(
                ErrorCode.DEVICE_NOT_FOUND,
                f"cannot send the post-connect blank line to {self.host}:{self.port}: {exc}",
                {"host": self.host, "port": self.port},
            ) from exc
        self._sock = sock
        self._buf.clear()

    def close(self) -> None:
        """切断する(冪等)。"""
        sock, self._sock = self._sock, None
        self._buf.clear()
        if sock is None:
            return
        try:
            sock.shutdown(socket.SHUT_RDWR)
        except OSError:
            pass  # 既に相手が切断済み。close は続行する。
        sock.close()

    @property
    def is_open(self) -> bool:
        return self._sock is not None

    # --- SCPI操作 ---------------------------------------------------------

    def write(self, command: str) -> None:
        self._send(command, self._deadline(None))

    def query(self, command: str, timeout_s: float | None = None) -> str:
        deadline = self._deadline(timeout_s)
        self._send(command, deadline)
        return self._next_line(command, deadline)

    def query_lines(self, command: str, timeout_s: float | None = None) -> list[str]:
        """複数行応答を、終端の**空行**が来るまで読む(空行自体は返さない)。

        実機MHO98の `:MATH<n>:FFT:SEARch:RES?` は行を改行で区切り、最後に空行を
        1本足して終わる(ピークが無いときは空行1本だけ)。`query` の1行読みでは
        残りが受信バッファに居座り、以降のqueryが前問の応答を読むdesyncになる。
        """
        deadline = self._deadline(timeout_s)
        self._send(command, deadline)
        lines: list[str] = []
        while line := self._next_line(command, deadline):
            lines.append(line)
        return lines

    def query_binary(self, command: str, timeout_s: float | None = None) -> bytes:
        deadline = self._deadline(timeout_s)
        self._send(command, deadline)

        def reader(n: int) -> bytes:
            return self._read_exact(n, command, deadline)

        payload = parse_block(reader)
        tail = self._read_exact(1, command, deadline)
        if tail != b"\n":
            raise ScopeError(
                ErrorCode.WAVEFORM_TRANSFER_FAILED,
                f"the byte after the binary block is not a newline: {tail!r}",
                {"command": command, "trailing": tail.decode("latin-1")},
            )
        return payload

    # --- 内部 -------------------------------------------------------------

    def _deadline(self, timeout_s: float | None) -> float:
        return time.monotonic() + (self.timeout_s if timeout_s is None else timeout_s)

    def _require_open(self) -> socket.socket:
        if self._sock is None:
            raise ScopeError(
                ErrorCode.DEVICE_DISCONNECTED,
                "Not connected (open() is required)",
                {"host": self.host, "port": self.port},
            )
        return self._sock

    def _timeout(self, command: str) -> ScopeError:
        """タイムアウト。接続と受信バッファを破棄する。

        機器が遅延して応答を返すと(実機MHO98では負荷時に0.9〜3.0秒の遅延応答を
        実測)、次のqueryが前問の応答を読むdesyncが起きる。接続ごと捨てれば
        次回は `ConnectionManager.require_scope()` の再接続(=回復用の空行+drain)
        からやり直せる。
        """
        self.close()
        return ScopeError(
            ErrorCode.TIMEOUT,
            f"response timed out: {command}",
            {"command": command},
        )

    def _disconnected(self, command: str, reason: str) -> ScopeError:
        """接続断。以後の操作が誤動作しないよう内部状態も閉じる。"""
        self.close()
        return ScopeError(
            ErrorCode.DEVICE_DISCONNECTED,
            f"connection was closed ({reason}): {command}",
            {"command": command, "host": self.host, "port": self.port},
        )

    def _remaining(self, command: str, deadline: float) -> float:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise self._timeout(command)
        return remaining

    def _send(self, command: str, deadline: float) -> None:
        sock = self._require_open()
        payload = (command + "\n").encode("ascii")
        try:
            sock.settimeout(self._remaining(command, deadline))
            sock.sendall(payload)
        except TimeoutError as exc:
            raise self._timeout(command) from exc
        except OSError as exc:  # ConnectionResetError / BrokenPipeError を含む
            raise self._disconnected(command, type(exc).__name__) from exc

    def _recv(self, command: str, deadline: float) -> None:
        """1回recvして内部バッファへ積む。"""
        sock = self._require_open()
        try:
            sock.settimeout(self._remaining(command, deadline))
            chunk = sock.recv(_RECV_CHUNK)
        except TimeoutError as exc:
            raise self._timeout(command) from exc
        except OSError as exc:
            raise self._disconnected(command, type(exc).__name__) from exc
        if not chunk:
            raise self._disconnected(command, "EOF")
        self._buf.extend(chunk)

    def _read_exact(self, n: int, command: str, deadline: float) -> bytes:
        """デッドラインまでに ちょうど n バイト読む。"""
        while len(self._buf) < n:
            self._recv(command, deadline)
        out = bytes(self._buf[:n])
        del self._buf[:n]
        return out

    def _next_line(self, command: str, deadline: float) -> str:
        """1行読んで、終端の改行を落とした文字列で返す。"""
        raw = self._read_line(command, deadline)
        return raw.decode("ascii", errors="replace").rstrip("\r\n")

    def _read_line(self, command: str, deadline: float) -> bytes:
        """デッドラインまでに '\\n' 終端の1行を読む(改行を含めて返す)。"""
        index = self._buf.find(b"\n")
        while index < 0:
            start = len(self._buf)
            self._recv(command, deadline)
            index = self._buf.find(b"\n", start)
        out = bytes(self._buf[: index + 1])
        del self._buf[: index + 1]
        return out
