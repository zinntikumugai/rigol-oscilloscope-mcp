"""transport/lan.py(raw socket SCPI)のテスト。

実ソケットを使う。スレッド内に「1接続だけ受け付けてスクリプトを再生する」
TCPスタブサーバーを立て、分割配送・無応答・接続断を再現する。
"""

from __future__ import annotations

import contextlib
import socket
import threading
import time
from collections.abc import Callable, Iterator
from weakref import WeakKeyDictionary

import pytest

from rigol_oscilloscope_mcp.errors import ErrorCode, ScopeError
from rigol_oscilloscope_mcp.transport import LanTransport, Transport

Handler = Callable[[socket.socket], None]

IDN = "RIGOL TECHNOLOGIES,MHO98,X,00.01.00"


# --- スタブサーバー ---------------------------------------------------------


class StubServer:
    """127.0.0.1 の空きポートで待ち受け、1接続に対し handler を実行する。"""

    def __init__(self, handler: Handler) -> None:
        self._handler = handler
        self._listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self._listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self._listener.bind(("127.0.0.1", 0))
        self._listener.listen(1)
        self.port: int = self._listener.getsockname()[1]
        self.error: BaseException | None = None
        self._thread = threading.Thread(target=self._serve, daemon=True)

    def _serve(self) -> None:
        try:
            conn, _ = self._listener.accept()
        except OSError:  # __exit__ で listener が閉じられた
            return
        with conn:
            conn.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
            conn.settimeout(2.0)
            try:
                self._handler(conn)
            except BaseException as exc:  # noqa: BLE001 - テスト側へ転送する
                self.error = exc

    def __enter__(self) -> StubServer:
        self._thread.start()
        return self

    def __exit__(self, *exc_info: object) -> None:
        # accept() 到達前に listener を閉じると handler が走らないため、先に join する。
        self._thread.join(timeout=2.0)
        self._listener.close()
        self._thread.join(timeout=0.5)


_BUFFERS: WeakKeyDictionary[socket.socket, bytearray] = WeakKeyDictionary()


def recv_command(conn: socket.socket) -> str:
    """'\\n' 終端のコマンドを1つ受信する(終端は除去して返す)。

    `open()` が送る回復用の空行は読み飛ばす。空行と後続コマンドは1回のrecvへ
    合流しうるため、接続ごとの受信バッファで行単位に切り出す。
    """
    buf = _BUFFERS.setdefault(conn, bytearray())
    while True:
        index = buf.find(b"\n")
        while index < 0:
            chunk = conn.recv(4096)
            if not chunk:
                raise AssertionError(f"コマンド受信中に切断されました: {bytes(buf)!r}")
            buf.extend(chunk)
            index = buf.find(b"\n")
        line = bytes(buf[:index])
        del buf[: index + 1]
        if line.strip():  # 空行(回復フラッシュ)は無視する
            return line.decode("ascii")


def wait_close(conn: socket.socket) -> None:
    """クライアントが閉じるまで待つ(送信済みデータのRST消失を防ぐ)。"""
    with contextlib.suppress(OSError):
        while conn.recv(4096):
            pass


@contextlib.contextmanager
def stub(handler: Handler, timeout_s: float = 1.0) -> Iterator[LanTransport]:
    server = StubServer(handler)
    with server:
        transport = LanTransport("127.0.0.1", port=server.port, timeout_s=timeout_s)
        transport.open()
        try:
            yield transport
        finally:
            transport.close()
    # handler の完了(= join)後でなければ error は確定しない。
    if server.error is not None:
        raise AssertionError(f"スタブサーバー側の失敗: {server.error!r}")


def closed_port() -> int:
    """誰も待ち受けていないポート番号を得る。"""
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.bind(("127.0.0.1", 0))
    port: int = sock.getsockname()[1]
    sock.close()
    return port


def block(payload: bytes) -> bytes:
    """definite-length block (#N<len><payload>) を組み立てる。"""
    length = str(len(payload)).encode("ascii")
    return b"#" + str(len(length)).encode("ascii") + length + payload


# --- Protocol適合 -----------------------------------------------------------


def test_lan_transport_satisfies_transport_protocol() -> None:
    assert isinstance(LanTransport("127.0.0.1"), Transport)


def test_default_port_is_5555() -> None:
    transport = LanTransport("192.0.2.1")
    assert transport.port == 5555


# --- query 正常系 -----------------------------------------------------------


def test_query_returns_line_without_trailing_newline() -> None:
    received: list[str] = []

    def handler(conn: socket.socket) -> None:
        received.append(recv_command(conn))
        conn.sendall(IDN.encode("ascii") + b"\n")
        wait_close(conn)

    with stub(handler) as transport:
        assert transport.query("*IDN?") == IDN

    assert received == ["*IDN?"]


def test_query_strips_crlf() -> None:
    def handler(conn: socket.socket) -> None:
        recv_command(conn)
        conn.sendall(b"OK\r\n")
        wait_close(conn)

    with stub(handler) as transport:
        assert transport.query(":RUN?") == "OK"


def test_query_assembles_response_split_across_two_sends() -> None:
    def handler(conn: socket.socket) -> None:
        recv_command(conn)
        conn.sendall(b"RIGOL TECHNOLOGIES,")
        time.sleep(0.02)
        conn.sendall(b"MHO98,X,00.01.00\n")
        wait_close(conn)

    with stub(handler) as transport:
        assert transport.query("*IDN?") == IDN


def test_second_query_does_not_consume_previous_response() -> None:
    """1回のsendで2応答分が届いても、queryごとに1行ずつ返す。"""
    commands: list[str] = []

    def handler(conn: socket.socket) -> None:
        commands.append(recv_command(conn))
        conn.sendall(b"A\nB\n")
        commands.append(recv_command(conn))
        wait_close(conn)

    with stub(handler) as transport:
        assert transport.query("FIRST?") == "A"
        assert transport.query("SECOND?") == "B"

    assert commands == ["FIRST?", "SECOND?"]


def test_write_appends_newline_and_sends_ascii() -> None:
    received: list[str] = []

    def handler(conn: socket.socket) -> None:
        received.append(recv_command(conn))
        wait_close(conn)

    with stub(handler) as transport:
        transport.write(":RUN")

    assert received == [":RUN"]


# --- query_lines(複数行応答)-----------------------------------------------


def test_query_lines_reads_until_the_terminating_blank_line() -> None:
    """実機MHO98の `:MATH1:FFT:SEARch:RES?`(改行区切り + 末尾に空行1本)。"""

    def handler(conn: socket.socket) -> None:
        recv_command(conn)
        conn.sendall(b"1,9.09061kHz,-1.373dBV\n2,27.0239kHz,-20.45dBV\n\n")
        wait_close(conn)

    with stub(handler) as transport:
        assert transport.query_lines(":MATH1:FFT:SEARch:RES?") == [
            "1,9.09061kHz,-1.373dBV",
            "2,27.0239kHz,-20.45dBV",
        ]


def test_query_lines_empty_table_is_just_a_blank_line() -> None:
    """探索OFF時の実機応答は空行1本のみ(行ゼロ)。"""

    def handler(conn: socket.socket) -> None:
        recv_command(conn)
        conn.sendall(b"\n")
        wait_close(conn)

    with stub(handler) as transport:
        assert transport.query_lines(":MATH1:FFT:SEARch:RES?") == []


def test_query_lines_does_not_desync_the_next_query() -> None:
    """終端の空行まで読み切るので、次のqueryが自分の応答を読む。"""
    commands: list[str] = []

    def handler(conn: socket.socket) -> None:
        commands.append(recv_command(conn))
        conn.sendall(b"1,1.00000kHz,-1.0dBV\n2,2.00000kHz,-2.0dBV\n\n")
        commands.append(recv_command(conn))
        conn.sendall(b"1.800000E+0\n")
        wait_close(conn)

    with stub(handler) as transport:
        assert len(transport.query_lines("RES?")) == 2
        assert transport.query("EXCursion?") == "1.800000E+0"

    assert commands == ["RES?", "EXCursion?"]


def test_query_lines_strips_crlf() -> None:
    def handler(conn: socket.socket) -> None:
        recv_command(conn)
        conn.sendall(b"1,1.00000kHz,-1.0dBV\r\n\r\n")
        wait_close(conn)

    with stub(handler) as transport:
        assert transport.query_lines("RES?") == ["1,1.00000kHz,-1.0dBV"]


def test_query_lines_times_out_without_the_blank_line_and_closes() -> None:
    """行は届くが終端の空行が来ない場合も query と同じくTIMEOUT + 切断。"""

    def handler(conn: socket.socket) -> None:
        recv_command(conn)
        conn.sendall(b"1,1.00000kHz,-1.0dBV\n")
        wait_close(conn)

    with stub(handler, timeout_s=0.2) as transport:
        with pytest.raises(ScopeError) as exc:
            transport.query_lines("RES?")

        assert exc.value.code == ErrorCode.TIMEOUT
        assert exc.value.detail["command"] == "RES?"
        assert transport.is_open is False


# --- query_binary -----------------------------------------------------------


def test_query_binary_waveform_delivered_one_byte_at_a_time() -> None:
    payload = bytes(i % 256 for i in range(1000))
    response = block(payload) + b"\n"
    assert response.startswith(b"#41000")

    def handler(conn: socket.socket) -> None:
        recv_command(conn)
        for i in range(len(response)):
            conn.sendall(response[i : i + 1])
        wait_close(conn)

    with stub(handler) as transport:
        assert transport.query_binary(":WAVeform:DATA?") == payload


def test_query_binary_screenshot_in_chunks() -> None:
    payload = b"\x89PNG\r\n\x1a\n" + bytes(i % 256 for i in range(97098 - 8))
    response = block(payload) + b"\n"
    assert response.startswith(b"#597098")

    def handler(conn: socket.socket) -> None:
        recv_command(conn)
        step = 8192
        for i in range(0, len(response), step):
            conn.sendall(response[i : i + step])
        wait_close(conn)

    with stub(handler) as transport:
        data = transport.query_binary(":DISPlay:DATA?")

    assert len(data) == 97098
    assert data == payload


def test_query_binary_followed_by_query_uses_fresh_response() -> None:
    payload = b"ab"

    def handler(conn: socket.socket) -> None:
        recv_command(conn)
        conn.sendall(block(payload) + b"\n")
        recv_command(conn)
        conn.sendall(b"OK\n")
        wait_close(conn)

    with stub(handler) as transport:
        assert transport.query_binary(":WAVeform:DATA?") == payload
        assert transport.query("*OPC?") == "OK"


def test_query_binary_without_trailing_newline_fails() -> None:
    def handler(conn: socket.socket) -> None:
        recv_command(conn)
        conn.sendall(block(b"ab") + b"X")
        wait_close(conn)

    with stub(handler) as transport:
        with pytest.raises(ScopeError) as exc:
            transport.query_binary(":WAVeform:DATA?")

    assert exc.value.code == ErrorCode.WAVEFORM_TRANSFER_FAILED


def test_query_binary_rejects_malformed_block() -> None:
    def handler(conn: socket.socket) -> None:
        recv_command(conn)
        conn.sendall(b"XYZ\n")
        wait_close(conn)

    with stub(handler) as transport:
        with pytest.raises(ScopeError) as exc:
            transport.query_binary(":WAVeform:DATA?")

    assert exc.value.code == ErrorCode.WAVEFORM_TRANSFER_FAILED


# --- タイムアウト -----------------------------------------------------------


def test_query_times_out_when_no_response() -> None:
    def handler(conn: socket.socket) -> None:
        wait_close(conn)

    started = time.monotonic()
    with stub(handler, timeout_s=0.2) as transport:
        with pytest.raises(ScopeError) as exc:
            transport.query("*IDN?")
    elapsed = time.monotonic() - started

    assert exc.value.code == ErrorCode.TIMEOUT
    assert exc.value.detail["command"] == "*IDN?"
    assert elapsed < 0.5


def test_query_timeout_argument_overrides_default() -> None:
    def handler(conn: socket.socket) -> None:
        wait_close(conn)

    started = time.monotonic()
    with stub(handler, timeout_s=10.0) as transport:
        with pytest.raises(ScopeError) as exc:
            transport.query("*IDN?", timeout_s=0.2)
    elapsed = time.monotonic() - started

    assert exc.value.code == ErrorCode.TIMEOUT
    assert elapsed < 0.5


def test_query_binary_deadline_covers_whole_operation() -> None:
    """recv単位ではなく操作全体でデッドラインを管理する。

    ヘッダだけ送って残りを送らないスタブに対し、timeout_s を大きく超えない。
    """

    def handler(conn: socket.socket) -> None:
        recv_command(conn)
        conn.sendall(b"#41000")
        wait_close(conn)

    started = time.monotonic()
    with stub(handler, timeout_s=0.2) as transport:
        with pytest.raises(ScopeError) as exc:
            transport.query_binary(":WAVeform:DATA?")
    elapsed = time.monotonic() - started

    assert exc.value.code == ErrorCode.TIMEOUT
    assert elapsed < 0.5


# --- タイムアウト後の切断(desync防止) --------------------------------------


def test_query_timeout_closes_connection() -> None:
    """TIMEOUT後は接続を破棄する(遅延応答を次のqueryが読まないため)。"""

    def handler(conn: socket.socket) -> None:
        recv_command(conn)
        wait_close(conn)

    with stub(handler, timeout_s=0.2) as transport:
        with pytest.raises(ScopeError) as exc:
            transport.query("*IDN?")
        assert exc.value.code == ErrorCode.TIMEOUT
        assert transport.is_open is False


def test_query_after_timeout_does_not_read_delayed_response() -> None:
    """タイムアウト後に機器が遅延応答を送っても、次のqueryはそれを読まない。

    接続ごと破棄されるため、同一transportでの次のqueryは DEVICE_DISCONNECTED
    になる(上位の ConnectionManager.require_scope() が再接続を担う)。
    """

    def handler(conn: socket.socket) -> None:
        recv_command(conn)
        time.sleep(0.3)  # timeout_s=0.1 を大きく超えてから遅延応答を送る
        with contextlib.suppress(OSError):  # 既に切断済みなら送信は失敗してよい
            conn.sendall(b"STALE\n")
        wait_close(conn)

    with stub(handler, timeout_s=0.1) as transport:
        with pytest.raises(ScopeError) as first:
            transport.query("FIRST?")
        assert first.value.code == ErrorCode.TIMEOUT

        time.sleep(0.3)  # 遅延応答が届きうる時間だけ待つ

        with pytest.raises(ScopeError) as second:
            transport.query("SECOND?")
        assert second.value.code == ErrorCode.DEVICE_DISCONNECTED


def test_query_binary_timeout_closes_connection() -> None:
    """query_binary のタイムアウト経路でも接続を破棄する。"""

    def handler(conn: socket.socket) -> None:
        recv_command(conn)
        conn.sendall(b"#41000")  # ヘッダだけ送り、本体を送らない
        wait_close(conn)

    with stub(handler, timeout_s=0.2) as transport:
        with pytest.raises(ScopeError) as exc:
            transport.query_binary(":WAVeform:DATA?")
        assert exc.value.code == ErrorCode.TIMEOUT
        assert transport.is_open is False


# --- 接続断 -----------------------------------------------------------------


def test_query_binary_disconnect_mid_block() -> None:
    """ブロック途中で切断された場合は DEVICE_DISCONNECTED に規定する。"""

    def handler(conn: socket.socket) -> None:
        recv_command(conn)
        conn.sendall(b"#41000" + b"x" * 100)
        conn.close()

    with stub(handler) as transport:
        with pytest.raises(ScopeError) as exc:
            transport.query_binary(":WAVeform:DATA?")
        assert exc.value.code == ErrorCode.DEVICE_DISCONNECTED
        assert transport.is_open is False


def test_query_disconnect_before_response() -> None:
    def handler(conn: socket.socket) -> None:
        recv_command(conn)
        conn.close()

    with stub(handler) as transport:
        with pytest.raises(ScopeError) as exc:
            transport.query("*IDN?")
        assert exc.value.code == ErrorCode.DEVICE_DISCONNECTED
        assert transport.is_open is False


# --- open / close / is_open -------------------------------------------------


def test_open_connection_refused_maps_to_device_not_found() -> None:
    port = closed_port()
    transport = LanTransport("127.0.0.1", port=port, timeout_s=0.5)

    with pytest.raises(ScopeError) as exc:
        transport.open()

    assert exc.value.code == ErrorCode.DEVICE_NOT_FOUND
    assert exc.value.detail == {"host": "127.0.0.1", "port": port}
    assert transport.is_open is False


def test_open_sends_recovery_newline_first() -> None:
    """open() 直後に空行1本を送る(wedge状態の機器を回復させる)。

    実機MHO98は未定義ヘッダのクエリ1回でSCPIサーバー全体が沈黙し、空行の送信で
    回復することが確認されている(接続・プロセス再起動では回復しない)。
    """
    first: list[bytes] = []

    def handler(conn: socket.socket) -> None:
        first.append(conn.recv(4096))
        wait_close(conn)

    with StubServer(handler) as server:
        transport = LanTransport("127.0.0.1", port=server.port, timeout_s=1.0)
        transport.open()
        transport.close()

    assert first and first[0].startswith(b"\n")


def test_recovery_newline_does_not_disturb_following_query() -> None:
    """回復用の空行のあとも、コマンドと応答の対応が崩れないこと。"""
    received: list[bytes] = []

    def handler(conn: socket.socket) -> None:
        buf = bytearray()
        while buf.count(b"\n") < 2:  # 回復用の空行 + '*IDN?'
            chunk = conn.recv(4096)
            if not chunk:
                raise AssertionError(f"切断されました: {bytes(buf)!r}")
            buf.extend(chunk)
        received.append(bytes(buf))
        conn.sendall(IDN.encode("ascii") + b"\n")
        wait_close(conn)

    with stub(handler) as transport:
        assert transport.query("*IDN?") == IDN

    assert received == [b"\n*IDN?\n"]


def test_open_unresolvable_host_maps_to_device_not_found() -> None:
    transport = LanTransport("rigol.invalid", port=5555, timeout_s=0.5)

    with pytest.raises(ScopeError) as exc:
        transport.open()

    assert exc.value.code == ErrorCode.DEVICE_NOT_FOUND
    assert exc.value.detail["host"] == "rigol.invalid"


def test_is_open_transitions() -> None:
    def handler(conn: socket.socket) -> None:
        wait_close(conn)

    with StubServer(handler) as server:
        transport = LanTransport("127.0.0.1", port=server.port, timeout_s=1.0)
        assert transport.is_open is False
        transport.open()
        assert transport.is_open is True
        transport.close()
        assert transport.is_open is False


def test_close_is_idempotent() -> None:
    def handler(conn: socket.socket) -> None:
        wait_close(conn)

    with StubServer(handler) as server:
        transport = LanTransport("127.0.0.1", port=server.port, timeout_s=1.0)
        transport.close()  # open前でも例外にしない
        transport.open()
        transport.close()
        transport.close()
        assert transport.is_open is False


@pytest.mark.parametrize(
    "call",
    [
        lambda t: t.write(":RUN"),
        lambda t: t.query("*IDN?"),
        lambda t: t.query_binary(":WAVeform:DATA?"),
    ],
    ids=["write", "query", "query_binary"],
)
def test_operations_before_open_raise_device_disconnected(
    call: Callable[[LanTransport], object],
) -> None:
    transport = LanTransport("127.0.0.1", port=5555)

    with pytest.raises(ScopeError) as exc:
        call(transport)

    assert exc.value.code == ErrorCode.DEVICE_DISCONNECTED


def test_query_after_close_raises_device_disconnected() -> None:
    def handler(conn: socket.socket) -> None:
        wait_close(conn)

    with StubServer(handler) as server:
        transport = LanTransport("127.0.0.1", port=server.port, timeout_s=1.0)
        transport.open()
        transport.close()
        with pytest.raises(ScopeError) as exc:
            transport.query("*IDN?")
        assert exc.value.code == ErrorCode.DEVICE_DISCONNECTED
