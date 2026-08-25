"""SCPIメッセージレベルのトランスポート抽象。"""

from __future__ import annotations

from typing import Protocol, runtime_checkable


@runtime_checkable
class Transport(Protocol):
    """SCPIメッセージレベルのトランスポート抽象。

    実装: LAN(raw socket)/USB(PyVISA)/Fake。
    終端(改行)の付与・除去や、バイナリブロック読み出し時の末尾改行の処理は
    各実装の責務であり、上位ドライバはこの抽象のみに依存する。
    """

    def open(self) -> None: ...

    def close(self) -> None: ...

    def write(self, command: str) -> None: ...

    def query(self, command: str, timeout_s: float | None = None) -> str: ...

    def query_binary(self, command: str, timeout_s: float | None = None) -> bytes: ...

    @property
    def is_open(self) -> bool: ...
