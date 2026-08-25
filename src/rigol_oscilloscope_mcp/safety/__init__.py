"""Safetyレイヤ(Requirements.md 6章 安全要件 / 7.6 監査ログ)。

- classes: 操作クラス分類(READ_ONLY / SAFE_WRITE / RESTRICTED_WRITE / DANGEROUS_WRITE)
- confirm: 危険操作の2段階確認(confirmトークン)
- audit:   JSONL監査ログ(AuditLogger / 操作単位の AuditScope)
"""

from .audit import AuditLogger, AuditScope, token_digest
from .classes import TOOL_CLASSES, OperationClass, classify
from .confirm import CONFIRM_INSTRUCTION, ConfirmRequest, ConfirmTokenStore

__all__ = [
    "CONFIRM_INSTRUCTION",
    "TOOL_CLASSES",
    "AuditLogger",
    "AuditScope",
    "ConfirmRequest",
    "ConfirmTokenStore",
    "OperationClass",
    "classify",
    "token_digest",
]
