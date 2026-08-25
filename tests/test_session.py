"""driver/session.py のテスト。

エラーキュー管理(drain)と、書き込み後の検証(set検証)の順序を検証する。
実機の癖(接続直後にエラーが残留する / 不正値の書き込みは無応答でキューに積む)は
FakeScope が再現しているため、本テストは全てフェイク機器で完結する。
"""

import pytest

from rigol_oscilloscope_mcp.driver.session import ScpiSession
from rigol_oscilloscope_mcp.errors import ErrorCode, ScopeError
from rigol_oscilloscope_mcp.testing import FakeScope, FakeTransport

NO_ERROR = '0,"No error"'
OUT_OF_RANGE = '-222,"Data out of range"'
ERROR_QUERY = ":SYSTem:ERRor?"


def make_session(scope: FakeScope) -> ScpiSession:
    transport = FakeTransport(scope)
    transport.open()
    return ScpiSession(transport)


@pytest.fixture
def scope() -> FakeScope:
    return FakeScope()


@pytest.fixture
def session(scope: FakeScope) -> ScpiSession:
    return make_session(scope)


# --------------------------------------------------------------------------
# drain_error_queue
# --------------------------------------------------------------------------


def test_drain_on_clean_queue_returns_empty_list(session: ScpiSession) -> None:
    assert session.drain_error_queue() == []


def test_drain_returns_stale_errors() -> None:
    scope = FakeScope(stale_error_queue=True)
    session = make_session(scope)

    assert session.drain_error_queue() == [OUT_OF_RANGE]
    assert not scope.error_queue


def test_drain_stops_at_no_error(session: ScpiSession, scope: FakeScope) -> None:
    scope.error_queue.extend([OUT_OF_RANGE, '-100,"Command err"'])

    drained = session.drain_error_queue()

    assert drained == [OUT_OF_RANGE, '-100,"Command err"']
    # 最後に No error を1回読んで止まる(=3回問い合わせ)
    assert scope.command_log == [ERROR_QUERY] * 3


def test_drain_exceeding_max_iter_raises_scpi_error(
    session: ScpiSession, scope: FakeScope
) -> None:
    scope.error_queue.extend([OUT_OF_RANGE] * 5)

    with pytest.raises(ScopeError) as excinfo:
        session.drain_error_queue(max_iter=3)

    assert excinfo.value.code == ErrorCode.SCPI_ERROR


# --------------------------------------------------------------------------
# query / query_binary
# --------------------------------------------------------------------------


def test_query_passes_through(session: ScpiSession, scope: FakeScope) -> None:
    assert session.query("*IDN?").startswith("RIGOL TECHNOLOGIES,MHO98")
    assert scope.command_log == ["*IDN?"]


def test_query_does_not_check_error_queue(session: ScpiSession, scope: FakeScope) -> None:
    scope.error_queue.append(OUT_OF_RANGE)

    session.query(":CHANnel1:SCALe?")

    assert scope.command_log == [":CHANnel1:SCALe?"]


def test_query_binary_returns_payload(session: ScpiSession) -> None:
    assert session.query_binary(":DISPlay:DATA?").startswith(b"\x89PNG\r\n\x1a\n")


# --------------------------------------------------------------------------
# query_checked
# --------------------------------------------------------------------------


def test_query_checked_returns_response_and_checks_queue(
    session: ScpiSession, scope: FakeScope
) -> None:
    response = session.query_checked(":CHANnel1:SCALe?")

    assert response == "1.000000E+1"
    assert scope.command_log == [":CHANnel1:SCALe?", ERROR_QUERY]


def test_query_checked_raises_when_error_queued(
    session: ScpiSession, scope: FakeScope
) -> None:
    scope.error_queue.append(OUT_OF_RANGE)

    with pytest.raises(ScopeError) as excinfo:
        session.query_checked(":CHANnel1:SCALe?")

    error = excinfo.value
    assert error.code == ErrorCode.SCPI_ERROR
    assert error.detail["command"] == ":CHANnel1:SCALe?"
    assert error.detail["scpi_error"] == OUT_OF_RANGE


# --------------------------------------------------------------------------
# write_checked
# --------------------------------------------------------------------------


def test_write_checked_sends_and_checks(session: ScpiSession, scope: FakeScope) -> None:
    session.write_checked(":CHANnel1:COUPling AC")

    assert scope.command_log == [":CHANnel1:COUPling AC", ERROR_QUERY]
    assert scope.channels[1]["coupling"] == "AC"


def test_write_checked_raises_on_rejected_value(
    session: ScpiSession, scope: FakeScope
) -> None:
    """不正値の書き込みは実機では無応答+キュー汚染。write_checkedが検出する。"""
    with pytest.raises(ScopeError) as excinfo:
        session.write_checked(":CHANnel1:COUPling BOGUS")

    error = excinfo.value
    assert error.code == ErrorCode.SCPI_ERROR
    assert error.detail["command"] == ":CHANnel1:COUPling BOGUS"
    assert error.detail["scpi_error"] == OUT_OF_RANGE


# --------------------------------------------------------------------------
# set_and_verify
# --------------------------------------------------------------------------


def test_set_and_verify_order_is_write_check_readback(
    session: ScpiSession, scope: FakeScope
) -> None:
    readback = session.set_and_verify(":CHANnel1:SCALe 2.0", ":CHANnel1:SCALe?")

    assert scope.command_log == [
        ":CHANnel1:SCALe 2.0",
        ERROR_QUERY,
        ":CHANnel1:SCALe?",
    ]
    assert readback == "2.000000E+0"


def test_set_and_verify_raises_before_readback_on_error(
    session: ScpiSession, scope: FakeScope
) -> None:
    with pytest.raises(ScopeError) as excinfo:
        session.set_and_verify(":CHANnel1:COUPling BOGUS", ":CHANnel1:COUPling?")

    assert excinfo.value.code == ErrorCode.SCPI_ERROR
    # エラー検出後に readback を送らない
    assert ":CHANnel1:COUPling?" not in scope.command_log
