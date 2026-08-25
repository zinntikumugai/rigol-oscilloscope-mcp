"""Toolエラーの機械可読表現(Requirements.md 7.4 / tools.md 0.3)。"""

from __future__ import annotations


class ErrorCode:
    """Tool返却の `code` に用いる文字列定数。"""

    DEVICE_NOT_FOUND = "DEVICE_NOT_FOUND"
    DEVICE_DISCONNECTED = "DEVICE_DISCONNECTED"
    DEVICE_BUSY = "DEVICE_BUSY"
    TIMEOUT = "TIMEOUT"
    INVALID_PARAMETER = "INVALID_PARAMETER"
    UNSUPPORTED_FEATURE = "UNSUPPORTED_FEATURE"
    SAFETY_POLICY_DENIED = "SAFETY_POLICY_DENIED"
    USER_CONFIRMATION_REQUIRED = "USER_CONFIRMATION_REQUIRED"
    ACQUISITION_FAILED = "ACQUISITION_FAILED"
    NO_SIGNAL = "NO_SIGNAL"
    WAVEFORM_TRANSFER_FAILED = "WAVEFORM_TRANSFER_FAILED"
    SCPI_ERROR = "SCPI_ERROR"
    UNSUPPORTED_COMMAND = "UNSUPPORTED_COMMAND"


class ScopeError(Exception):
    """MCP Toolが返す機械可読エラー。

    `to_dict()` の結果がそのままTool返却の error オブジェクトになる。
    """

    def __init__(self, code: str, message: str, detail: dict | None = None) -> None:
        super().__init__(f"{code}: {message}")
        self.code = code
        self.message = message
        self.detail: dict = detail if detail is not None else {}

    def to_dict(self) -> dict:
        return {"code": self.code, "message": self.message, "detail": self.detail}
