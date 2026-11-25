"""service/clock.py のテスト(機器時刻とホスト時刻のずれ / issue #54)。"""

from datetime import datetime, timedelta, timezone

import pytest

from rigol_oscilloscope_mcp.driver.scope import ScopeDriver
from rigol_oscilloscope_mcp.driver.session import ScpiSession
from rigol_oscilloscope_mcp.profiles import load_profile
from rigol_oscilloscope_mcp.service.clock import CLOCK_TOLERANCE_S, clock_check
from rigol_oscilloscope_mcp.testing import FakeScope, FakeTransport

#: ホスト時刻(UTC+0 のローカル時刻として与える)
HOST = datetime(2026, 11, 25, 17, 18, 50, 400000, tzinfo=timezone.utc)


def make_driver(scope: FakeScope, profile_name: str = "mho98") -> ScopeDriver:
    transport = FakeTransport(scope)
    transport.open()
    return ScopeDriver(ScpiSession(transport), load_profile(profile_name))


@pytest.fixture
def scope() -> FakeScope:
    return FakeScope()


def test_clock_in_sync_has_no_warning(scope: FakeScope) -> None:
    scope.clock = datetime(2026, 11, 25, 17, 18, 51)

    result = clock_check(make_driver(scope), now=HOST)

    assert result == {
        "device_time": "2026-11-25T17:18:51",
        "host_time": "2026-11-25T17:18:50+00:00",
        "time_skew_s": 0.6,
        "warning": None,
    }


def test_clock_skew_beyond_tolerance_warns(scope: FakeScope) -> None:
    scope.clock = datetime(2026, 11, 25, 17, 21, 0)

    result = clock_check(make_driver(scope), now=HOST)

    assert result["time_skew_s"] == pytest.approx(129.6)
    assert abs(result["time_skew_s"]) > CLOCK_TOLERANCE_S
    assert "129" in result["warning"]
    assert "timezone" not in result["warning"]


def test_whole_hour_skew_suggests_a_timezone_mismatch(scope: FakeScope) -> None:
    """実機で観測した状態(機器はJST、ホストはUTC)。"""
    scope.clock = datetime(2026, 11, 26, 2, 18, 51)

    result = clock_check(make_driver(scope), now=HOST)

    assert result["time_skew_s"] == pytest.approx(9 * 3600 + 0.6)
    assert "timezone" in result["warning"]
    assert "+09:00" in result["warning"]


def test_timezone_hint_tolerates_clock_drift(scope: FakeScope) -> None:
    """実機で観測した値(+9時間 + 29秒)。時差と残りのドリフトを分けて伝える。"""
    scope.clock = datetime(2026, 11, 26, 2, 19, 20)

    result = clock_check(make_driver(scope), now=HOST)

    assert result["time_skew_s"] == pytest.approx(9 * 3600 + 29.6)
    assert "+09:00" in result["warning"]
    assert "+29.6 s" in result["warning"]


def test_far_from_a_timezone_step_gives_no_hint(scope: FakeScope) -> None:
    """15分単位から大きく外れたずれ(9時間7分)は時差と断定しない。"""
    scope.clock = datetime(2026, 11, 26, 2, 26, 0)

    result = clock_check(make_driver(scope), now=HOST)

    assert "timezone" not in result["warning"]


def test_negative_half_hour_offset_is_also_a_timezone_hint(scope: FakeScope) -> None:
    scope.clock = (HOST - timedelta(hours=5, minutes=30)).replace(tzinfo=None, microsecond=0)

    result = clock_check(make_driver(scope), now=HOST)

    assert "-05:30" in result["warning"]


def test_clock_check_is_none_on_generic(scope: FakeScope) -> None:
    driver = make_driver(scope, "rigol-generic")

    assert clock_check(driver, now=HOST) is None
    assert scope.command_log == []
