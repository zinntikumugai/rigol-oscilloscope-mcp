"""errors.py のテスト。"""

import pytest

from rigol_oscilloscope_mcp.errors import ErrorCode, ScopeError

# Requirements.md 7.4 のエラーコード + UNSUPPORTED_COMMAND
EXPECTED_CODES = {
    "DEVICE_NOT_FOUND",
    "DEVICE_DISCONNECTED",
    "DEVICE_BUSY",
    "TIMEOUT",
    "INVALID_PARAMETER",
    "UNSUPPORTED_FEATURE",
    "SAFETY_POLICY_DENIED",
    "USER_CONFIRMATION_REQUIRED",
    "ACQUISITION_FAILED",
    "NO_SIGNAL",
    "WAVEFORM_TRANSFER_FAILED",
    "SCPI_ERROR",
    "UNSUPPORTED_COMMAND",
}


def _defined_codes() -> dict[str, str]:
    return {
        name: value
        for name, value in vars(ErrorCode).items()
        if not name.startswith("_") and isinstance(value, str)
    }


def test_all_error_codes_defined() -> None:
    assert set(_defined_codes()) == EXPECTED_CODES


def test_error_code_name_equals_value() -> None:
    """定数名と文字列値が一致すること(Tool返却のcodeがそのまま使えるため)。"""
    for name, value in _defined_codes().items():
        assert name == value


def test_scope_error_is_exception() -> None:
    with pytest.raises(ScopeError):
        raise ScopeError(ErrorCode.TIMEOUT, "timed out")


def test_scope_error_attributes() -> None:
    err = ScopeError(ErrorCode.INVALID_PARAMETER, "bad value", {"field": "port"})
    assert err.code == "INVALID_PARAMETER"
    assert err.message == "bad value"
    assert err.detail == {"field": "port"}


def test_scope_error_detail_defaults_to_empty_dict() -> None:
    err = ScopeError(ErrorCode.NO_SIGNAL, "no signal")
    assert err.detail == {}


def test_scope_error_to_dict() -> None:
    err = ScopeError(ErrorCode.SCPI_ERROR, "boom", {"scpi": "-113"})
    assert err.to_dict() == {
        "code": "SCPI_ERROR",
        "message": "boom",
        "detail": {"scpi": "-113"},
    }


def test_scope_error_to_dict_without_detail() -> None:
    err = ScopeError(ErrorCode.DEVICE_BUSY, "busy")
    assert err.to_dict() == {"code": "DEVICE_BUSY", "message": "busy", "detail": {}}


def test_scope_error_str_contains_code_and_message() -> None:
    err = ScopeError(ErrorCode.DEVICE_NOT_FOUND, "not found")
    text = str(err)
    assert "DEVICE_NOT_FOUND" in text
    assert "not found" in text
