"""SCPIメッセージレベルのトランスポート抽象。"""

from __future__ import annotations

from typing import Protocol, runtime_checkable


@runtime_checkable
class Transport(Protocol):
    """SCPIメッセージレベルのトランスポート抽象。

    実装: LAN(raw socket)/USB(PyVISA)/Fake。
    終端(改行)の付与・除去や、バイナリブロック読み出し時の末尾改行の処理は
    各実装の責務であり、上位ドライバはこの抽象のみに依存する。

    `query` は**1行だけ**読む。複数行で返る応答(実機MHO98で確認できているのは
    `:MATH<n>:FFT:SEARch:RES?` のピーク表だけ)は `query_lines` を使う。
    """

    def open(self) -> None: ...

    def close(self) -> None: ...

    def write(self, command: str) -> None: ...

    def query(self, command: str, timeout_s: float | None = None) -> str: ...

    def query_lines(
        self, command: str, timeout_s: float | None = None
    ) -> list[str]: ...

    def query_binary(self, command: str, timeout_s: float | None = None) -> bytes: ...

    def query_binary_or_none(
        self, command: str, timeout_s: float | None = None
    ) -> bytes | None:
        """ブロック応答を読む。**機器が応答を返さなかったら None**(待たずに判別する)。

        `<command>` の後ろに `*OPC?` を送り、最初に `#` が来ればブロック(後に続く
        `*OPC?` の `1` も読み捨てる)、`1` だけなら機器が応答を出さなかったとみなす。
        実機 MHO98 の RS232 の `:BUS<n>:DATA?` が `-200` を積んで何も返さないことが
        あるため(issue #92)。通常の `query_binary` だとタイムアウトまで待ち、接続を
        閉じてしまう。
        """
        ...

    def write_block(self, command: str, payload: bytes) -> None:
        """`<command> #N<len><payload>` + 終端を送る(バイナリの書き込み)。"""
        ...

    @property
    def is_open(self) -> bool: ...
