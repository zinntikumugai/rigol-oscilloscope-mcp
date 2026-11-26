"""service/analysis.py のテスト(ホスト側解析。機器・実測値に依存しない純関数が主)。

FFT は既知波形(整数周期の正弦波)で「ビンが一致するか」「振幅が復元できるか」を、
E2E は FakeScope の作り付け正弦波(1000点 / dt=2e-6 / 1周期 = 500 Hz)で検証する。
"""

import math

import pytest

from rigol_oscilloscope_mcp.config import Config
from rigol_oscilloscope_mcp.driver.scope import ScopeDriver
from rigol_oscilloscope_mcp.driver.session import ScpiSession
from rigol_oscilloscope_mcp.errors import ErrorCode, ScopeError
from rigol_oscilloscope_mcp.service.analysis import (
    analyze_waveform,
    waveform_fft,
    waveform_stats,
)
from rigol_oscilloscope_mcp.testing import FakeScope, FakeTransport

# FakeScope の作り付け波形(testing/fake_scope.py の定数と一致すること)
YINCREMENT = 6.8267e-02
YREFERENCE = 128
XINCREMENT = 2.0e-6
POINTS = 1000
RAW_MIN = 127
RAW_MAX = 174

FAKE_VPP_V = (RAW_MAX - RAW_MIN) * YINCREMENT  # ≒3.209 V
FAKE_AMPLITUDE_V = FAKE_VPP_V / 2.0  # ≒1.604 V
FAKE_MEAN_V = ((RAW_MAX + RAW_MIN) / 2.0 - YREFERENCE) * YINCREMENT  # ≒1.536 V
FAKE_SIGNAL_HZ = 1.0 / (POINTS * XINCREMENT)  # 1周期/レコード = 500 Hz


def sine(points: int, period: int, amplitude: float = 1.0, offset: float = 0.0) -> list[float]:
    return [
        offset + amplitude * math.sin(2.0 * math.pi * i / period) for i in range(points)
    ]


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
# waveform_stats
# --------------------------------------------------------------------------


def test_stats_of_square_wave() -> None:
    result = waveform_stats([-1.0, 1.0] * 8)

    assert result == {
        "min_v": -1.0,
        "max_v": 1.0,
        "mean_v": 0.0,
        "rms_v": 1.0,
        "std_v": 1.0,
        "vpp_v": 2.0,
    }


def test_stats_of_sine() -> None:
    result = waveform_stats(sine(1024, 16, amplitude=2.0))

    assert result["min_v"] == pytest.approx(-2.0, abs=1e-9)
    assert result["max_v"] == pytest.approx(2.0, abs=1e-9)
    assert result["mean_v"] == pytest.approx(0.0, abs=1e-9)
    assert result["rms_v"] == pytest.approx(2.0 / math.sqrt(2.0), rel=1e-9)
    assert result["std_v"] == pytest.approx(2.0 / math.sqrt(2.0), rel=1e-9)
    assert result["vpp_v"] == pytest.approx(4.0, abs=1e-9)


def test_stats_mean_carries_dc_offset() -> None:
    assert waveform_stats(sine(1024, 16, offset=3.0))["mean_v"] == pytest.approx(
        3.0, abs=1e-9
    )


def test_stats_of_empty_samples_is_invalid() -> None:
    with pytest.raises(ScopeError) as excinfo:
        waveform_stats([])

    assert excinfo.value.code == ErrorCode.INVALID_PARAMETER


# --------------------------------------------------------------------------
# waveform_fft
# --------------------------------------------------------------------------


def test_fft_on_power_of_two_hits_the_exact_bin() -> None:
    # 1024点 / 周期16サンプル / dt=1e-6 → 62.5 kHz がちょうどビン64
    result = waveform_fft(sine(1024, 16, amplitude=2.0), 1e-6)

    assert result["window"] == "hann"
    assert result["dominant_frequency_hz"] == pytest.approx(62500.0, rel=1e-9)
    assert result["frequency_resolution_hz"] == pytest.approx(1.0 / (1024 * 1e-6))
    assert result["peaks"][0]["amplitude_v"] == pytest.approx(2.0, rel=0.05)


def test_fft_on_non_power_of_two_is_within_resolution() -> None:
    result = waveform_fft(sine(1000, 16, amplitude=2.0), 1e-6)

    assert result["frequency_resolution_hz"] == pytest.approx(1000.0, rel=1e-9)
    assert abs(result["dominant_frequency_hz"] - 62500.0) <= result[
        "frequency_resolution_hz"
    ]


def test_fft_ignores_the_dc_bin() -> None:
    result = waveform_fft(sine(1024, 16, amplitude=2.0, offset=5.0), 1e-6)

    assert all(peak["frequency_hz"] > 0.0 for peak in result["peaks"])
    assert result["dominant_frequency_hz"] == pytest.approx(62500.0, rel=1e-9)
    assert result["peaks"][0]["amplitude_v"] == pytest.approx(2.0, rel=0.05)


def test_fft_peaks_are_limited_and_sorted() -> None:
    samples = [
        a + b + c
        for a, b, c in zip(
            sine(1024, 16, amplitude=2.0),
            sine(1024, 32, amplitude=1.0),
            sine(1024, 8, amplitude=0.5),
            strict=True,
        )
    ]

    result = waveform_fft(samples, 1e-6)
    peaks = result["peaks"]

    assert 0 < len(peaks) <= 5
    amplitudes = [peak["amplitude_v"] for peak in peaks]
    assert amplitudes == sorted(amplitudes, reverse=True)
    # 上位3本は3成分そのもの(振幅の大きい順 = 62.5k / 31.25k / 125k)
    assert [peak["frequency_hz"] for peak in peaks[:3]] == pytest.approx(
        [62500.0, 31250.0, 125000.0], rel=1e-9
    )
    assert amplitudes[:3] == pytest.approx([2.0, 1.0, 0.5], rel=0.05)


def test_fft_top_n_is_configurable() -> None:
    samples = [
        a + b
        for a, b in zip(
            sine(1024, 16, amplitude=2.0), sine(1024, 32, amplitude=1.0), strict=True
        )
    ]

    assert len(waveform_fft(samples, 1e-6, top_n=1)["peaks"]) == 1


def test_fft_of_flat_signal_has_no_dominant_frequency() -> None:
    result = waveform_fft([1.0] * 64, 1e-6)

    assert result["peaks"] == []
    assert result["dominant_frequency_hz"] is None


@pytest.mark.parametrize("interval", [0.0, -1e-6])
def test_fft_with_non_positive_interval_is_invalid(interval: float) -> None:
    with pytest.raises(ScopeError) as excinfo:
        waveform_fft(sine(64, 8), interval)

    assert excinfo.value.code == ErrorCode.INVALID_PARAMETER


# --------------------------------------------------------------------------
# analyze_waveform(E2E: FakeScope)
# --------------------------------------------------------------------------


def test_analyze_returns_metadata_without_samples(
    driver: ScopeDriver, config: Config
) -> None:
    result = analyze_waveform(driver, config, "CH1")

    assert result["channel"] == "CH1"
    assert result["points"] == POINTS
    assert result["sample_interval_s"] == XINCREMENT
    assert result["effective_sample_rate_sa_per_s"] == 500000.0
    assert "decimated" in result["note"]
    # 生データは capture_waveform の仕事。解析結果に配列を混ぜない。
    assert "samples_v" not in result
    assert "data_file" not in result


def test_analyze_stats_match_the_fake_waveform(
    driver: ScopeDriver, config: Config
) -> None:
    stats = analyze_waveform(driver, config, "CH1")["stats"]

    assert stats["vpp_v"] == pytest.approx(FAKE_VPP_V, rel=1e-6)
    assert stats["min_v"] == pytest.approx((RAW_MIN - YREFERENCE) * YINCREMENT, rel=1e-6)
    assert stats["max_v"] == pytest.approx((RAW_MAX - YREFERENCE) * YINCREMENT, rel=1e-6)
    assert stats["mean_v"] == pytest.approx(FAKE_MEAN_V, abs=0.01)


def test_analyze_fft_finds_the_fake_signal(driver: ScopeDriver, config: Config) -> None:
    fft = analyze_waveform(driver, config, "CH1")["fft"]

    assert fft["frequency_resolution_hz"] == pytest.approx(FAKE_SIGNAL_HZ, rel=1e-9)
    assert abs(fft["dominant_frequency_hz"] - FAKE_SIGNAL_HZ) <= fft[
        "frequency_resolution_hz"
    ]
    # 1レコード=1周期はビン境界に載らないため振幅は緩めに見る
    assert fft["peaks"][0]["amplitude_v"] == pytest.approx(FAKE_AMPLITUDE_V, rel=0.15)


def test_analyze_default_runs_every_analysis(driver: ScopeDriver, config: Config) -> None:
    result = analyze_waveform(driver, config, "CH1")

    assert "stats" in result
    assert "fft" in result


def test_analyze_subset_omits_the_others(driver: ScopeDriver, config: Config) -> None:
    result = analyze_waveform(driver, config, "CH1", analyses=["stats"])

    assert "stats" in result
    assert "fft" not in result


def test_analyze_unknown_analysis_is_invalid(driver: ScopeDriver, config: Config) -> None:
    with pytest.raises(ScopeError) as excinfo:
        analyze_waveform(driver, config, "CH1", analyses=["bogus"])

    assert excinfo.value.code == ErrorCode.INVALID_PARAMETER
    assert excinfo.value.detail["valid"] == [
        "stats", "fft", "pulse", "noise", "thd", "jitter"
    ]


def test_analyze_empty_analyses_is_invalid(driver: ScopeDriver, config: Config) -> None:
    with pytest.raises(ScopeError) as excinfo:
        analyze_waveform(driver, config, "CH1", analyses=[])

    assert excinfo.value.code == ErrorCode.INVALID_PARAMETER


def test_analyze_validates_before_touching_the_device(
    driver: ScopeDriver, config: Config, scope: FakeScope
) -> None:
    with pytest.raises(ScopeError):
        analyze_waveform(driver, config, "CH1", analyses=["bogus"])

    assert scope.command_log == []


# --------------------------------------------------------------------------
# max_points(capture_waveform と同じ挙動)
# --------------------------------------------------------------------------


def test_analyze_max_points_is_forwarded_to_driver(
    driver: ScopeDriver, config: Config, scope: FakeScope
) -> None:
    analyze_waveform(driver, config, "CH1", max_points=500)

    assert ":WAVeform:STOP 500" in scope.command_log


def test_analyze_max_points_above_config_is_clamped(
    driver: ScopeDriver, scope: FakeScope
) -> None:
    result = analyze_waveform(
        driver, Config(waveform_max_points=200), "CH1", max_points=1200
    )

    assert ":WAVeform:STOP 200" in scope.command_log
    assert result["max_points_clamped"] is True


def test_analyze_max_points_within_config_is_not_clamped(
    driver: ScopeDriver, scope: FakeScope
) -> None:
    result = analyze_waveform(
        driver, Config(waveform_max_points=200), "CH1", max_points=150
    )

    assert "max_points_clamped" not in result


@pytest.mark.parametrize("max_points", [0, -1])
def test_analyze_max_points_not_positive_is_invalid(
    driver: ScopeDriver, config: Config, max_points: int
) -> None:
    with pytest.raises(ScopeError) as excinfo:
        analyze_waveform(driver, config, "CH1", max_points=max_points)

    assert excinfo.value.code == ErrorCode.INVALID_PARAMETER


# --------------------------------------------------------------------------
# MATHソース(FFTトレースは時間軸解析の対象にならない)
# --------------------------------------------------------------------------


def test_analyze_analog_channel_asks_nothing_about_math(
    driver: ScopeDriver, config: Config, scope: FakeScope
) -> None:
    analyze_waveform(driver, config, "CH1")

    assert [c for c in scope.command_log if "MATH" in c.upper()] == []


def test_analyze_non_fft_math_source(
    driver: ScopeDriver, config: Config, scope: FakeScope
) -> None:
    result = analyze_waveform(driver, config, "MATH2", analyses=["stats"])

    assert result["channel"] == "MATH2"
    assert ":WAVeform:SOURce MATH2" in scope.command_log
    assert result["stats"]["vpp_v"] == pytest.approx(FAKE_VPP_V, rel=1e-6)


def test_analyze_rejects_an_fft_math_trace(
    driver: ScopeDriver, config: Config, scope: FakeScope
) -> None:
    """FFTトレースへの時間軸統計・ホストFFTは無意味なので拒否する。"""
    scope.math[1]["operator"] = "FFT"

    with pytest.raises(ScopeError) as excinfo:
        analyze_waveform(driver, config, "MATH1")

    assert excinfo.value.code == ErrorCode.INVALID_PARAMETER
    assert "get_math_state" in excinfo.value.message
    assert "capture_waveform" in excinfo.value.message
    assert ":WAVeform:DATA?" not in scope.command_log


# --------------------------------------------------------------------------
# ホスト側解析の拡充(issue #73)。合成信号で期待値を確かめる
# --------------------------------------------------------------------------

import math as _math
import random as _random


def _square(points: int, period: int, low: float = 0.0, high: float = 1.0) -> list[float]:
    return [high if (i % period) < period // 2 else low for i in range(points)]


def test_pulse_overshoot_and_preshoot() -> None:
    """矩形波の立ち上がり直後に +0.2V、立ち下がり直後に −0.1V のはね(振幅1V)。"""
    from rigol_oscilloscope_mcp.service.analysis import waveform_pulse

    samples = _square(4000, 400)
    for start in range(0, 4000, 400):
        samples[start] = 1.2  # 立ち上がりのオーバーシュート
        samples[start + 200] = -0.1  # 立ち下がりのアンダーシュート

    result = waveform_pulse(samples)

    assert result["top_v"] == pytest.approx(1.0, abs=0.02)
    assert result["base_v"] == pytest.approx(0.0, abs=0.02)
    assert result["overshoot_percent"] == pytest.approx(20.0, abs=2.5)
    assert result["preshoot_percent"] == pytest.approx(10.0, abs=2.5)


def test_pulse_on_a_flat_signal_reports_no_amplitude() -> None:
    from rigol_oscilloscope_mcp.service.analysis import waveform_pulse

    result = waveform_pulse([0.5] * 1000)

    assert result["overshoot_percent"] is None
    assert "warning" in result


def test_noise_estimate_recovers_white_noise() -> None:
    """ゆっくりした正弦波 + σ=10mV の白色雑音。隣接差による推定で σ を取り出す。"""
    from rigol_oscilloscope_mcp.service.analysis import waveform_noise

    rng = _random.Random(7)
    samples = [
        _math.sin(2 * _math.pi * i / 20000) + rng.gauss(0.0, 0.01) for i in range(20000)
    ]

    result = waveform_noise(samples)

    assert result["noise_rms_v"] == pytest.approx(0.01, rel=0.05)


def test_thd_of_a_sine_with_known_harmonics() -> None:
    """基本波 1V + 3次 0.1V + 5次 0.05V → THD = √(0.1²+0.05²)/1 = 11.18%。"""
    from rigol_oscilloscope_mcp.service.analysis import waveform_thd

    dt = 1e-6
    f0 = 1000.0
    samples = [
        _math.sin(2 * _math.pi * f0 * i * dt)
        + 0.1 * _math.sin(2 * _math.pi * 3 * f0 * i * dt)
        + 0.05 * _math.sin(2 * _math.pi * 5 * f0 * i * dt)
        for i in range(16384)
    ]

    result = waveform_thd(samples, dt)

    assert result["fundamental_hz"] == pytest.approx(f0, rel=0.02)
    assert result["thd_percent"] == pytest.approx(11.18, abs=0.6)
    third = next(h for h in result["harmonics"] if h["order"] == 3)
    assert third["amplitude_v"] == pytest.approx(0.1, rel=0.1)


def test_jitter_of_a_square_wave_with_shifted_edges() -> None:
    """周期 100 点の矩形波で、立ち上がりを交互に ±1 点ずらす → 周期は 98 / 102 点。"""
    from rigol_oscilloscope_mcp.service.analysis import waveform_jitter

    dt = 1e-6
    samples: list[float] = []
    for cycle in range(40):
        shift = 1 if cycle % 2 else -1
        width = 100 + 2 * shift if cycle else 100
        samples += [1.0] * (width // 2) + [0.0] * (width - width // 2)

    result = waveform_jitter(samples, dt)

    assert result["cycles"] >= 30
    assert result["period_mean_s"] == pytest.approx(100e-6, rel=0.02)
    assert result["period_jitter_rms_s"] == pytest.approx(2e-6, rel=0.2)
    assert result["cycle_to_cycle_jitter_rms_s"] > result["period_jitter_rms_s"]


def test_jitter_needs_at_least_three_rising_edges() -> None:
    from rigol_oscilloscope_mcp.service.analysis import waveform_jitter

    result = waveform_jitter(_square(150, 100), 1e-6)

    assert result["period_mean_s"] is None
    assert "warning" in result


def test_default_analyses_stay_stats_and_fft() -> None:
    """analyses を省略したときの返却は従来どおり(stats / fft だけ)。"""
    from rigol_oscilloscope_mcp.service.analysis import DEFAULT_ANALYSES, VALID_ANALYSES

    assert DEFAULT_ANALYSES == ("stats", "fft")
    assert {"pulse", "noise", "thd", "jitter"} <= set(VALID_ANALYSES)


def test_thd_refuses_a_record_with_too_few_cycles() -> None:
    """**実機で踏んだ不具合**: 2周期しか無い記録では基本波のビンが小さく、高調波の
    探索範囲が基本波と重なって基本波を高調波として数えた(THD 183%)。周期が
    足りなければ計算せずに警告する。"""
    from rigol_oscilloscope_mcp.service.analysis import waveform_thd

    dt = 2e-6
    samples = [_math.sin(2 * _math.pi * 1000 * i * dt) for i in range(1000)]  # 2周期

    result = waveform_thd(samples, dt)

    assert result["thd_percent"] is None
    assert "cycles" in result["warning"]


def test_thd_of_a_square_wave_is_about_48_percent() -> None:
    """理想の矩形波(奇数次のみ、振幅 1/n)の THD は約48%(10次まで数えると約45%)。"""
    from rigol_oscilloscope_mcp.service.analysis import waveform_thd

    dt = 1e-5
    samples = _square(16000, 100)  # 1kHz・160周期

    result = waveform_thd(samples, dt)

    assert 40 < result["thd_percent"] < 50
    orders = [h["order"] for h in result["harmonics"]]
    assert orders == sorted(set(orders))


def test_noise_estimate_ignores_sparse_edges() -> None:
    """**実機で踏んだ不具合**: 矩形波だとエッジの段差が隣接差に入り、標準偏差では
    雑音を過大に見積もった。中央絶対偏差なら、まばらなエッジに引っ張られない。"""
    from rigol_oscilloscope_mcp.service.analysis import waveform_noise

    rng = _random.Random(11)
    samples = [value + rng.gauss(0.0, 0.01) for value in _square(20000, 50)]

    result = waveform_noise(samples)

    assert result["noise_rms_v"] == pytest.approx(0.01, rel=0.1)


def test_pulse_levels_ignore_sparse_large_spikes() -> None:
    """まばらな大きいスパイク(3V)で上下の境界がずれ、1V の平坦部を下側と数えて
    トップを取り違えないこと(Copilotレビュー)。"""
    from rigol_oscilloscope_mcp.service.analysis import waveform_pulse

    samples = _square(4000, 400)
    for start in range(0, 4000, 400):
        samples[start] = 3.0  # まばらな大きいオーバーシュート

    result = waveform_pulse(samples)

    assert result["top_v"] == pytest.approx(1.0, abs=0.05)
    assert result["overshoot_percent"] == pytest.approx(200.0, abs=10)


@pytest.mark.parametrize("dt", [0.0, -1e-6])
def test_thd_and_jitter_reject_a_non_positive_interval(dt: float) -> None:
    from rigol_oscilloscope_mcp.service.analysis import waveform_jitter, waveform_thd

    samples = [_math.sin(i / 10) for i in range(2000)]
    for analysis in (waveform_thd, waveform_jitter):
        with pytest.raises(ScopeError) as excinfo:
            analysis(samples, dt)
        assert excinfo.value.code == ErrorCode.INVALID_PARAMETER
