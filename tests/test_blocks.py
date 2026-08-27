"""transport/blocks.py と transport/base.py のテスト。"""

from collections import deque
from collections.abc import Iterable

import pytest

from rigol_oscilloscope_mcp.errors import ErrorCode, ScopeError
from rigol_oscilloscope_mcp.transport import Transport, parse_block


class ChunkedReader:
    """チャンク分割で届くバイト列から「ちょうどnバイト」を返すreader。

    実トランスポート(socket.recv)同様、1回の配送がヘッダ境界/ペイロード境界を
    またぐ状況を再現する。不足時は例外を送出する(parse_blockの契約)。
    """

    def __init__(self, chunks: Iterable[bytes]) -> None:
        self._chunks: deque[bytes] = deque(chunks)
        self._buf = bytearray()
        self.calls: list[int] = []

    def __call__(self, n: int) -> bytes:
        self.calls.append(n)
        while len(self._buf) < n:
            if not self._chunks:
                raise EOFError(f"need {n} bytes, have {len(self._buf)}")
            self._buf.extend(self._chunks.popleft())
        out = bytes(self._buf[:n])
        del self._buf[:n]
        return out

    @property
    def remaining(self) -> bytes:
        return bytes(self._buf) + b"".join(self._chunks)


def _split(data: bytes, size: int) -> list[bytes]:
    return [data[i : i + size] for i in range(0, len(data), size)]


def _reader(data: bytes, chunk: int | None = None) -> ChunkedReader:
    return ChunkedReader(_split(data, chunk) if chunk else [data])


# --- 正常系 ---------------------------------------------------------------


def test_parse_block_minimal() -> None:
    assert parse_block(_reader(b"#12ab")) == b"ab"


def test_parse_block_empty_payload() -> None:
    assert parse_block(_reader(b"#10")) == b""


def test_parse_block_waveform_1000_bytes() -> None:
    """phase0実測: :WAVeform:DATA? が返す1000バイトブロック(#41000)。"""
    payload = bytes(i % 256 for i in range(1000))
    assert parse_block(_reader(b"#41000" + payload)) == payload


def test_parse_block_waveform_1000_bytes_chunked() -> None:
    payload = bytes(i % 256 for i in range(1000))
    reader = _reader(b"#41000" + payload + b"\n", chunk=7)
    assert parse_block(reader) == payload


def test_parse_block_screenshot_97098_bytes() -> None:
    """phase0実測: :DISPlay:DATA? が返すPNG 97098バイト(#800097098)。"""
    payload = b"\x89PNG\r\n\x1a\n" + bytes(i % 256 for i in range(97098 - 8))
    assert len(payload) == 97098
    header = b"#800097098"
    assert parse_block(_reader(header + payload)) == payload


@pytest.mark.parametrize("chunk", [1, 3, 11, 1024, 4096, 65536])
def test_parse_block_screenshot_chunked(chunk: int) -> None:
    payload = b"\x89PNG\r\n\x1a\n" + bytes(i % 256 for i in range(97098 - 8))
    reader = _reader(b"#800097098" + payload + b"\n", chunk=chunk)
    assert parse_block(reader) == payload


def test_parse_block_leaves_trailing_newline_unread() -> None:
    """末尾の改行はトランスポートの責務。parse_blockは読まない。"""
    reader = _reader(b"#12ab\n")
    assert parse_block(reader) == b"ab"
    assert reader.remaining == b"\n"


def test_parse_block_reads_exactly_payload_length() -> None:
    """ペイロード長を超えて先読みしない(後続応答を食わない)。"""
    reader = _reader(b"#12ab\n#13cde\n")
    assert parse_block(reader) == b"ab"
    assert parse_block(_reader(reader.remaining[1:])) == b"cde"


def test_parse_block_max_digits() -> None:
    """桁数部は1〜9桁を許容する。"""
    assert parse_block(_reader(b"#9000000003xyz")) == b"xyz"


def test_parse_block_payload_with_hash_and_newlines() -> None:
    """ペイロード中の '#' や改行を区切りと誤認しない。"""
    payload = b"#41000\n\n#0\n"
    reader = _reader(b"#2" + f"{len(payload):02d}".encode() + payload)
    assert parse_block(reader) == payload


# --- 異常系 ---------------------------------------------------------------


def _assert_transfer_error(data: bytes) -> ScopeError:
    with pytest.raises(ScopeError) as exc:
        parse_block(_reader(data))
    assert exc.value.code == ErrorCode.WAVEFORM_TRANSFER_FAILED
    assert exc.value.message
    return exc.value


def test_parse_block_rejects_missing_hash() -> None:
    _assert_transfer_error(b"X12ab")


def test_parse_block_rejects_indefinite_length() -> None:
    """'#0' = 不定長ブロックは明示的に非対応。"""
    _assert_transfer_error(b"#0abcdef\n")


def test_parse_block_rejects_non_digit_digit_count() -> None:
    _assert_transfer_error(b"#X100abc")


def test_parse_block_rejects_non_digit_length() -> None:
    _assert_transfer_error(b"#412a4" + b"x" * 16)


def test_parse_block_rejects_length_with_sign() -> None:
    _assert_transfer_error(b"#2-1abc")


def test_parse_block_rejects_length_with_space() -> None:
    _assert_transfer_error(b"#4 100" + b"x" * 100)


def test_parse_block_rejects_short_read_from_reader() -> None:
    """readerが要求より少なく返した場合も転送失敗として扱う。"""

    def bad_reader(n: int) -> bytes:
        return b"#12ab"[:1] if n == 1 else b""

    with pytest.raises(ScopeError) as exc:
        parse_block(bad_reader)
    assert exc.value.code == ErrorCode.WAVEFORM_TRANSFER_FAILED


def test_parse_block_propagates_reader_exception() -> None:
    """データ不足時のreader側例外はそのまま伝播する(契約)。"""
    with pytest.raises(EOFError):
        parse_block(_reader(b"#41000" + b"x" * 10))


# --- Transport Protocol ---------------------------------------------------


class _FakeTransport:
    def __init__(self) -> None:
        self._open = False

    def open(self) -> None:
        self._open = True

    def close(self) -> None:
        self._open = False

    def write(self, command: str) -> None:
        pass

    def query(self, command: str, timeout_s: float | None = None) -> str:
        return "OK"

    def query_lines(self, command: str, timeout_s: float | None = None) -> list[str]:
        return ["OK"]

    def query_binary(self, command: str, timeout_s: float | None = None) -> bytes:
        return b"OK"

    @property
    def is_open(self) -> bool:
        return self._open


def test_transport_protocol_accepts_conforming_object() -> None:
    assert isinstance(_FakeTransport(), Transport)


def test_transport_protocol_rejects_incomplete_object() -> None:
    class Partial:
        def open(self) -> None:
            pass

    assert not isinstance(Partial(), Transport)


def test_transport_protocol_member_names() -> None:
    for name in ("open", "close", "write", "query", "query_binary", "is_open"):
        assert hasattr(Transport, name), name
