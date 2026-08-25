"""safety/classes.py のテスト(Requirements.md 6.1 / tools.md 8章)。"""

import pytest

from rigol_oscilloscope_mcp.errors import ErrorCode, ScopeError
from rigol_oscilloscope_mcp.safety.classes import (
    TOOL_CLASSES,
    OperationClass,
    classify,
)

# docs/tools.md 8章のサマリ表(Phase 1/2/4 + 開発用 raw_scpi)
EXPECTED = {
    "connect": "SAFE_WRITE",
    "disconnect": "SAFE_WRITE",
    "scope_identify": "READ_ONLY",
    "get_capabilities": "READ_ONLY",
    "get_state": "READ_ONLY",
    "get_channel": "READ_ONLY",
    "get_timebase": "READ_ONLY",
    "get_trigger": "READ_ONLY",
    "get_acquisition_state": "READ_ONLY",
    "measure": "READ_ONLY",
    "capture_waveform": "READ_ONLY",
    "analyze_waveform": "READ_ONLY",
    "capture_screenshot": "READ_ONLY",
    "configure_channel": "SAFE_WRITE",
    "configure_timebase": "SAFE_WRITE",
    "configure_trigger": "SAFE_WRITE",
    "run": "SAFE_WRITE",
    "stop": "SAFE_WRITE",
    "single": "SAFE_WRITE",
    "autoset": "RESTRICTED_WRITE",
    "raw_scpi": "DANGEROUS_WRITE",
}


def test_operation_class_members_and_values() -> None:
    assert {member.name: member.value for member in OperationClass} == {
        "READ_ONLY": "READ_ONLY",
        "SAFE_WRITE": "SAFE_WRITE",
        "RESTRICTED_WRITE": "RESTRICTED_WRITE",
        "DANGEROUS_WRITE": "DANGEROUS_WRITE",
    }


def test_operation_class_is_str() -> None:
    """StrEnum のため JSON 直列化・文字列比較がそのまま通ること。"""
    assert OperationClass.READ_ONLY == "READ_ONLY"
    assert f"{OperationClass.DANGEROUS_WRITE}" == "DANGEROUS_WRITE"


def test_tool_classes_covers_exactly_the_catalog() -> None:
    assert set(TOOL_CLASSES) == set(EXPECTED)


@pytest.mark.parametrize(("tool", "expected"), sorted(EXPECTED.items()))
def test_classify_matches_catalog(tool: str, expected: str) -> None:
    result = classify(tool)
    assert result == expected
    assert isinstance(result, OperationClass)
    assert TOOL_CLASSES[tool] == expected


def test_classify_unknown_tool_raises_invalid_parameter() -> None:
    with pytest.raises(ScopeError) as exc_info:
        classify("factory_default")
    err = exc_info.value
    assert err.code == ErrorCode.INVALID_PARAMETER
    assert err.detail.get("tool") == "factory_default"


def test_classify_is_case_sensitive() -> None:
    """Tool名は tools.md 0.1 のスネークケース。大文字表記は未知として扱う。"""
    with pytest.raises(ScopeError):
        classify("RAW_SCPI")


def test_tool_classes_values_are_operation_class() -> None:
    for value in TOOL_CLASSES.values():
        assert isinstance(value, OperationClass)


def test_dangerous_and_restricted_sets() -> None:
    """confirmトークンを要する集合が意図どおりであること(6.1)。"""
    needs_confirm = {
        tool
        for tool, cls in TOOL_CLASSES.items()
        if cls in (OperationClass.RESTRICTED_WRITE, OperationClass.DANGEROUS_WRITE)
    }
    assert needs_confirm == {"autoset", "raw_scpi"}
