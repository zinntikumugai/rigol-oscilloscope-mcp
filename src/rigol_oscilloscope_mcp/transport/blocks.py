"""IEEE 488.2 definite-length arbitrary block の純粋関数パーサ。"""

from __future__ import annotations

from collections.abc import Callable

from ..errors import ErrorCode, ScopeError

_DIGITS = frozenset(b"0123456789")


def _fail(message: str, detail: dict | None = None) -> ScopeError:
    return ScopeError(ErrorCode.WAVEFORM_TRANSFER_FAILED, message, detail or {})


def _read_exact(reader: Callable[[int], bytes], n: int, what: str) -> bytes:
    """readerを1回呼び、ちょうどnバイト返ることを検証する。"""
    data = reader(n)
    if len(data) != n:
        raise _fail(
            f"{what}の読み出しが不完全です({len(data)}/{n} bytes)",
            {"expected": n, "received": len(data)},
        )
    return data


def parse_block(reader: Callable[[int], bytes]) -> bytes:
    """definite-length block (#N<len><payload>) を解凍して payload を返す。

    `reader(n)` は「ちょうど n バイト読む」コールバック(不足時は reader 側が
    例外を投げる契約)。末尾の改行はここでは読まない(トランスポートの責務)。
    """
    head = _read_exact(reader, 1, "ブロックヘッダ")
    if head != b"#":
        raise _fail(
            f"ブロックヘッダが '#' で始まっていません: {head!r}",
            {"head": head.decode("latin-1")},
        )

    digit_count_raw = _read_exact(reader, 1, "ブロック長の桁数")
    if digit_count_raw[0] not in _DIGITS or digit_count_raw == b"0":
        # '#0' は不定長ブロック(非対応)。それ以外の非数字は破損ヘッダ。
        raise _fail(
            f"ブロック長の桁数が不正です(1〜9のみ対応): {digit_count_raw!r}",
            {"digit_count": digit_count_raw.decode("latin-1")},
        )
    digit_count = digit_count_raw[0] - 48

    length_raw = _read_exact(reader, digit_count, "ブロック長")
    if not all(b in _DIGITS for b in length_raw):
        raise _fail(
            f"ブロック長が10進数ではありません: {length_raw!r}",
            {"length": length_raw.decode("latin-1")},
        )
    length = int(length_raw)

    if length == 0:
        return b""
    return _read_exact(reader, length, "ブロックペイロード")
