"""service/waveform.py のテスト。

phase0実測(docs/verification/mho98-phase0.md)のプリアンブルを持つフェイク機器で、
電圧変換・時間軸メタデータ・インライン/一時ファイルの分岐を検証する。
"""

import json
import os

import pytest

from rigol_oscilloscope_mcp.config import Config
from rigol_oscilloscope_mcp.driver.scope import ScopeDriver
from rigol_oscilloscope_mcp.driver.session import ScpiSession
from rigol_oscilloscope_mcp.errors import ErrorCode, ScopeError
from rigol_oscilloscope_mcp.service import waveform as waveform_module
from rigol_oscilloscope_mcp.service.waveform import capture_waveform
from rigol_oscilloscope_mcp.testing import FakeScope, FakeTransport

# phase0実測のプリアンブル由来の期待値
YINCREMENT = 6.8267e-02
YORIGIN = 0
YREFERENCE = 128
XINCREMENT = 2.0e-6
XORIGIN = -1.0e-3
POINTS = 1000
RAW_MIN = 127
RAW_MAX = 174


def volts(raw: int) -> float:
    return (raw - YORIGIN - YREFERENCE) * YINCREMENT


@pytest.fixture
def scope() -> FakeScope:
    return FakeScope()


@pytest.fixture
def driver(scope: FakeScope) -> ScopeDriver:
    from rigol_oscilloscope_mcp.profiles import load_profile

    transport = FakeTransport(scope)
    transport.open()
    return ScopeDriver(ScpiSession(transport), load_profile("mho98"))


@pytest.fixture
def config() -> Config:
    return Config()


# --------------------------------------------------------------------------
# インライン経路(小規模)
# --------------------------------------------------------------------------


def test_inline_conversion(driver: ScopeDriver, config: Config) -> None:
    result = capture_waveform(driver, config, "CH1")

    assert result["channel"] == "CH1"
    assert result["points"] == POINTS
    assert len(result["samples_v"]) == POINTS
    assert min(result["samples_v"]) == pytest.approx(volts(RAW_MIN), rel=1e-6)
    assert max(result["samples_v"]) == pytest.approx(volts(RAW_MAX), rel=1e-6)


def test_inline_time_metadata(driver: ScopeDriver, config: Config) -> None:
    result = capture_waveform(driver, config, "CH1")

    assert result["sample_interval_s"] == XINCREMENT
    assert result["time_origin_s"] == XORIGIN
    assert result["effective_sample_rate_sa_per_s"] == 500000.0
    assert "間引き" in result["note"]


def test_inline_has_no_file_reference(driver: ScopeDriver, config: Config) -> None:
    result = capture_waveform(driver, config, "CH1")

    assert "data_file" not in result
    assert "data_format" not in result


def test_result_is_json_serializable(driver: ScopeDriver, config: Config) -> None:
    result = capture_waveform(driver, config, "CH1")

    restored = json.loads(json.dumps(result))

    assert restored["points"] == POINTS


# --------------------------------------------------------------------------
# max_points
# --------------------------------------------------------------------------


def test_max_points_is_forwarded_to_driver(
    driver: ScopeDriver, config: Config, scope: FakeScope
) -> None:
    # FakeScope の :WAVeform:DATA? は STARt/STOP を無視して全点返すため、
    # ドライバへ渡ったことを送信コマンドで検証する。
    capture_waveform(driver, config, "CH1", max_points=500)

    assert ":WAVeform:STOP 500" in scope.command_log


def test_max_points_none_uses_config(driver: ScopeDriver, scope: FakeScope) -> None:
    capture_waveform(driver, Config(waveform_max_points=200), "CH1")

    assert ":WAVeform:STOP 200" in scope.command_log


@pytest.mark.parametrize("max_points", [0, -1])
def test_max_points_not_positive_is_invalid(
    driver: ScopeDriver, config: Config, max_points: int
) -> None:
    with pytest.raises(ScopeError) as excinfo:
        capture_waveform(driver, config, "CH1", max_points=max_points)

    assert excinfo.value.code == ErrorCode.INVALID_PARAMETER


def test_invalid_max_points_sends_nothing(
    driver: ScopeDriver, config: Config, scope: FakeScope
) -> None:
    with pytest.raises(ScopeError):
        capture_waveform(driver, config, "CH1", max_points=0)

    assert scope.command_log == []


# --------------------------------------------------------------------------
# 一時ファイル経路(大規模)
# --------------------------------------------------------------------------


def test_large_capture_writes_csv_file(
    driver: ScopeDriver, config: Config, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(waveform_module, "INLINE_POINTS_LIMIT", 100)

    result = capture_waveform(driver, config, "CH1")
    path = result["data_file"]
    try:
        assert "samples_v" not in result
        assert result["points"] == POINTS
        assert result["data_format"] == "csv"
        assert os.path.isabs(path)
        assert os.path.basename(path).startswith("rigol_waveform_")
        assert path.endswith(".csv")

        with open(path, encoding="utf-8") as fp:
            lines = fp.read().splitlines()

        assert lines[0] == "time_s,volts"
        assert len(lines) == POINTS + 1

        expected_first_raw = driver.read_waveform("CH1").data[0]
        time_text, volts_text = lines[1].split(",")
        assert float(time_text) == pytest.approx(XORIGIN, rel=1e-6)
        assert float(volts_text) == pytest.approx(
            volts(expected_first_raw), rel=1e-6
        )

        last_time, _ = lines[POINTS].split(",")
        assert float(last_time) == pytest.approx(
            XORIGIN + (POINTS - 1) * XINCREMENT, rel=1e-6
        )
    finally:
        os.unlink(path)


def test_large_capture_result_is_json_serializable(
    driver: ScopeDriver, config: Config, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(waveform_module, "INLINE_POINTS_LIMIT", 100)

    result = capture_waveform(driver, config, "CH1")
    try:
        assert json.loads(json.dumps(result))["data_format"] == "csv"
    finally:
        os.unlink(result["data_file"])
