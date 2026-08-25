"""実機read-only検証スイート(T14a)。

実機に対して**読み取り専用**の操作のみを行い、実装の想定と実機の真値が
一致しているかを確認する。設定を変更するコマンドは一切送らない
(`:WAVeform:SOURce/MODE/FORMat/STARt/STOP` は波形読み出しの前段であり、
機器の測定条件そのものは変えない)。

接続先は環境変数 `RIGOL_TEST_ADDRESS` からのみ取得する。実機のアドレスを
リポジトリに残さないため、テストコードにアドレスを直書きしてはならない
(`tests/test_ip_guard.py` が機械的に検査している)。

未設定時は `tests/conftest.py` が `device` マーカーを一括skipする。

実測値はレポート用に収集し、セッション終了時にまとめて出力する
(`-s` 付きで実行すると見える)。
"""

from __future__ import annotations

import os
import re
import statistics
import time
from collections.abc import Iterator
from pathlib import Path

import pytest

from rigol_oscilloscope_mcp.config import Config
from rigol_oscilloscope_mcp.driver.scope import MEASUREMENT_KEYS, ScopeDriver
from rigol_oscilloscope_mcp.service import (
    ConnectionManager,
    ConnectionStatus,
    capture_screenshot,
    capture_waveform,
    get_decode_result,
    get_state,
    measure,
)

pytestmark = pytest.mark.device

ADDRESS_ENV = "RIGOL_TEST_ADDRESS"

# tools.md の目標応答時間は1秒だが、実測でばらつく(phase0: 単発クエリ最大3秒)。
# ここでは「タイムアウトせず完走する」ことだけを検証し、所要時間はレポートする。
GET_STATE_BUDGET_S = 60.0

EXPECTED_MODEL_RE = re.compile(r"^MHO9[0-9]")
EXPECTED_PROFILE_NAME = "mho98"
EXPECTED_PROFILE_CONFIDENCE = "verified"
EXPECTED_MANUFACTURER_KEYWORD = "RIGOL"

EXPECTED_SECTIONS = {"channels", "timebase", "trigger", "acquisition"}
EXPECTED_CHANNELS = ("CH1", "CH2", "CH3", "CH4")

MEASUREMENT_NAMES = [
    "frequency",
    "period",
    "vpp",
    "vmax",
    "vmin",
    "vavg",
    "rms",
    "duty",
    "rise_time",
    "fall_time",
]

# 画面表示波形の点数(phase0実測: NORMalモードで1000点)
MIN_WAVEFORM_POINTS = 1000
# 妥当性の粗い上限。probe比込みでもこの範囲を外れたら変換式が疑わしい。
VOLTAGE_SANITY_LIMIT_V = 50.0

PNG_MAGIC = b"\x89PNG"
JPEG_MAGIC = b"\xff\xd8"
MIN_SCREENSHOT_BYTES = 10000

LATENCY_SAMPLES = 5

_REPORT: list[str] = []


def _report(line: str) -> None:
    """レポート行を記録しつつ、その場でも出力する(`-s` で可視)。"""
    _REPORT.append(line)
    print(line)


def _mask_serial(serial: str) -> str:
    """シリアルは先頭2文字だけ残す(ログ・レポートに実物を残さない)。"""
    return serial[:2] + "***"


@pytest.fixture(scope="session")
def device_address() -> str:
    address = os.environ.get(ADDRESS_ENV)
    if not address:
        pytest.skip(f"{ADDRESS_ENV} が未設定です")
    return address


@pytest.fixture(scope="session")
def device_config() -> Config:
    """本番と同じ既定値(timeout 5秒)で実機に当てる。"""
    return Config()


@pytest.fixture(scope="session")
def manager(device_config: Config, device_address: str) -> Iterator[ConnectionManager]:
    """接続シーケンスを1度だけ実行し、全テストで共有する。"""
    connection = ConnectionManager(device_config)
    started = time.perf_counter()
    connection.connect(address=device_address)
    _report(f"[connect] 接続シーケンス所要 {time.perf_counter() - started:.3f}s")
    try:
        yield connection
    finally:
        connection.disconnect()


@pytest.fixture(scope="session")
def status(manager: ConnectionManager) -> ConnectionStatus:
    return manager.status()


@pytest.fixture(scope="session")
def driver(manager: ConnectionManager) -> ScopeDriver:
    return manager.require_scope()


@pytest.fixture(scope="session", autouse=True)
def report_summary() -> Iterator[None]:
    yield
    if not _REPORT:
        return
    print("\n" + "=" * 68)
    print("実機read-only検証レポート")
    print("=" * 68)
    for line in _REPORT:
        print(line)


# -- 1. 識別 ---------------------------------------------------------------


def test_identify(status: ConnectionStatus) -> None:
    assert status.connected is True
    idn = status.idn
    assert idn is not None

    _report(
        "[idn] manufacturer=%r model=%r serial=%s firmware=%r"
        % (idn.manufacturer, idn.model, _mask_serial(idn.serial), idn.firmware)
    )
    _report(
        f"[idn] transport={status.transport} port={status.port} "
        f"profile={status.profile_name}/{status.profile_confidence} "
        f"unsupported_vendor={status.unsupported_vendor}"
    )

    assert EXPECTED_MANUFACTURER_KEYWORD in idn.manufacturer.upper()
    assert EXPECTED_MODEL_RE.match(idn.model), f"想定外のモデル: {idn.model!r}"
    assert status.profile_name == EXPECTED_PROFILE_NAME
    assert status.profile_confidence == EXPECTED_PROFILE_CONFIDENCE
    assert status.unsupported_vendor is False


# -- 2. エラーキュー -------------------------------------------------------


def test_error_queue_is_empty_after_connect(driver: ScopeDriver) -> None:
    """接続時drainの後、エラーキューが空のままであること。"""
    drained = driver.session.drain_error_queue()
    _report(f"[error_queue] 接続後の残留エラー: {drained}")
    assert drained == []


# -- 3. get_state 全セクション ---------------------------------------------


def test_get_state_all_sections(driver: ScopeDriver) -> None:
    started = time.perf_counter()
    state = get_state(driver)
    elapsed = time.perf_counter() - started
    _report(f"[get_state] 全セクション所要 {elapsed:.3f}s")

    assert set(state) == EXPECTED_SECTIONS

    channels = state["channels"]
    assert set(channels) == set(EXPECTED_CHANNELS)
    for name in EXPECTED_CHANNELS:
        channel = channels[name]
        _report(f"[get_state] {name}: {channel}")
        assert channel["channel"] == name
        assert isinstance(channel["enabled"], bool)
        assert isinstance(channel["scale_v_per_div"], float)
        assert isinstance(channel["offset_v"], float)
        assert channel["coupling"] in ("DC", "AC", "GND")
        assert isinstance(channel["impedance"], str)
        assert isinstance(channel["probe_ratio"], float)
        assert isinstance(channel["bandwidth_limit"], bool)

    timebase = state["timebase"]
    _report(f"[get_state] timebase: {timebase}")
    assert isinstance(timebase["scale_s_per_div"], float)
    assert isinstance(timebase["position_s"], float)
    # AUTO 等の非数値応答は None になりうる(driver._optional_number)
    assert timebase["sample_rate_sa_per_s"] is None or isinstance(
        timebase["sample_rate_sa_per_s"], float
    )
    assert timebase["memory_depth"] is None or isinstance(
        timebase["memory_depth"], float
    )

    trigger = state["trigger"]
    _report(f"[get_state] trigger: {trigger}")
    assert trigger["type"] == "edge"
    assert isinstance(trigger["source"], str)
    assert isinstance(trigger["level_v"], float)
    assert trigger["slope"] in ("rising", "falling", "either")
    assert trigger["sweep_mode"] in ("auto", "normal", "single")
    assert isinstance(trigger["status"], str)

    acquisition = state["acquisition"]
    _report(f"[get_state] acquisition: {acquisition}")
    assert isinstance(acquisition["trigger_status"], str)
    assert isinstance(acquisition["running"], bool)

    assert elapsed < GET_STATE_BUDGET_S


# -- 4. get_state セクション絞り込み ---------------------------------------


def test_get_state_trigger_section_only(driver: ScopeDriver) -> None:
    started = time.perf_counter()
    state = get_state(driver, sections=["trigger"])
    elapsed = time.perf_counter() - started
    _report(f"[get_state:trigger] 所要 {elapsed:.3f}s -> {state}")

    assert set(state) == {"trigger"}
    assert state["trigger"]["type"] == "edge"


# -- 5. 測定10項目 ---------------------------------------------------------


def test_measure_all_items_on_ch1(driver: ScopeDriver) -> None:
    """全測定項目が例外なく返ること。

    プローブ補正信号が繋がっていない可能性があるため、値そのものは検証せず
    型と quality のみを見る(実測値はレポートに残す)。
    """
    started = time.perf_counter()
    result = measure(driver, "CH1", MEASUREMENT_NAMES)
    elapsed = time.perf_counter() - started
    _report(f"[measure] 10項目所要 {elapsed:.3f}s (1項目あたり {elapsed / 10:.3f}s)")

    assert result["channel"] == "CH1"
    values = result["values"]
    quality = result["quality"]
    assert set(quality) == set(MEASUREMENT_NAMES)
    assert set(values) == {MEASUREMENT_KEYS[name] for name in MEASUREMENT_NAMES}

    for name in MEASUREMENT_NAMES:
        key = MEASUREMENT_KEYS[name]
        _report(f"[measure] {name} ({key}): value={values[key]!r} quality={quality[name]!r}")

    for name in MEASUREMENT_NAMES:
        key = MEASUREMENT_KEYS[name]
        value = values[key]
        assert isinstance(quality[name], str)
        assert value is None or isinstance(value, float), (key, value)
        # quality が valid の項目は必ず数値が入っていること
        if quality[name] == "valid":
            assert isinstance(value, float)

    _report(f"[measure] warnings: {result['warnings']}")


# -- 6. 波形取得 -----------------------------------------------------------


def test_capture_waveform_ch1(driver: ScopeDriver, device_config: Config) -> None:
    started = time.perf_counter()
    result = capture_waveform(driver, device_config, "CH1")
    elapsed = time.perf_counter() - started

    assert result["channel"] == "CH1"
    points = result["points"]
    interval = result["sample_interval_s"]
    _report(
        f"[waveform] 所要 {elapsed:.3f}s points={points} "
        f"sample_interval_s={interval:g} "
        f"effective_rate={result['effective_sample_rate_sa_per_s']:g} Sa/s "
        f"time_origin_s={result['time_origin_s']:g}"
    )

    assert points >= MIN_WAVEFORM_POINTS
    assert isinstance(interval, float)
    assert interval > 0

    samples = result["samples_v"]
    assert len(samples) == points

    vmin = min(samples)
    vmax = max(samples)
    _report(f"[waveform] samples_v 範囲: min={vmin:.6f} V max={vmax:.6f} V")
    assert -VOLTAGE_SANITY_LIMIT_V <= vmin <= VOLTAGE_SANITY_LIMIT_V
    assert -VOLTAGE_SANITY_LIMIT_V <= vmax <= VOLTAGE_SANITY_LIMIT_V


# -- 7. スクリーンショット --------------------------------------------------


def _screenshot_config(tmp_path: Path) -> Config:
    """保存先を tmp_path に限定した設定(許可ルート検証を通すため)。"""
    return Config(screenshot_dir=tmp_path, allowed_dirs=(tmp_path,))


def test_capture_screenshot_png(driver: ScopeDriver, tmp_path: Path) -> None:
    target = tmp_path / "scope.png"
    started = time.perf_counter()
    result = capture_screenshot(driver, _screenshot_config(tmp_path), path=str(target))
    elapsed = time.perf_counter() - started
    _report(
        f"[screenshot:png] 所要 {elapsed:.3f}s path={result.saved_path} "
        f"size={result.size_bytes} bytes mime={result.mime}"
    )

    saved = Path(result.saved_path)
    assert saved.is_file()
    data = saved.read_bytes()
    assert data[:4] == PNG_MAGIC
    assert result.size_bytes == len(data)
    assert result.size_bytes > MIN_SCREENSHOT_BYTES
    assert result.format == "png"


def test_capture_screenshot_jpg(driver: ScopeDriver, tmp_path: Path) -> None:
    target = tmp_path / "scope.jpg"
    started = time.perf_counter()
    result = capture_screenshot(driver, _screenshot_config(tmp_path), path=str(target))
    elapsed = time.perf_counter() - started
    _report(
        f"[screenshot:jpg] 所要 {elapsed:.3f}s path={result.saved_path} "
        f"size={result.size_bytes} bytes mime={result.mime}"
    )

    saved = Path(result.saved_path)
    assert saved.is_file()
    data = saved.read_bytes()
    assert data[:2] == JPEG_MAGIC
    assert result.size_bytes == len(data)
    assert result.format == "jpeg"


# -- 8. オプション照会 -----------------------------------------------------


def test_installed_options_answer(driver: ScopeDriver) -> None:
    """全 `<type>` が 0/1 で応答すること(ライセンス適用後も通る検証)。

    値そのものは資産状態に依存するためレポートのみ(未ライセンス時の実測は
    docs/verification/mho98-unlicensed.md)。
    """
    started = time.perf_counter()
    options = driver.installed_options()
    elapsed = time.perf_counter() - started
    _report(f"[options] {len(options)}件所要 {elapsed:.3f}s")

    for name, installed in sorted(options.items()):
        _report(f"[options] {name}: {installed!r}")
    assert all(isinstance(v, bool) for v in options.values()), options


# -- 8. レイテンシ観測(assertなし)----------------------------------------


def test_identify_latency_profile(driver: ScopeDriver) -> None:
    """`*IDN?` 1往復のレイテンシを観測する(レポート用、閾値判定はしない)。"""
    durations: list[float] = []
    for _ in range(LATENCY_SAMPLES):
        started = time.perf_counter()
        driver.identify()
        durations.append(time.perf_counter() - started)

    _report(
        "[latency] scope_identify x%d: min=%.4fs median=%.4fs max=%.4fs (all=%s)"
        % (
            LATENCY_SAMPLES,
            min(durations),
            statistics.median(durations),
            max(durations),
            ", ".join(f"{d:.4f}" for d in durations),
        )
    )


# -- 9. デコード結果(read-only。設定は一切変えない)------------------------


def test_get_decode_result_bus1(driver: ScopeDriver) -> None:
    """バス1のイベントテーブルを読む(表示・イベントテーブルの状態は変えない)。

    read-onlyスイートなのでデコードを有効化しない。既に有効ならテーブルの
    解釈可能性(時刻列がfloat)を、無効なら早期returnの警告形を検証する。
    """
    config = driver.get_decode_config(1)
    _report(
        "[decode] bus1 protocol=%s enabled=%s event_table=%s format=%s"
        % (
            config["protocol"],
            config["enabled"],
            config["event_table"],
            config["data_format"],
        )
    )

    result = get_decode_result(driver, 1)

    assert result["bus"] == 1
    assert result["truncated"] is False
    if not (config["enabled"] and config["event_table"]):
        assert result["events"] == []
        assert result["columns"] == []
        assert any("configure_decode(bus=1" in w for w in result["warnings"])
        return

    _report(f"[decode] columns={result['columns']} events={result['event_count']}")
    for event in result["events"][:3]:
        _report(f"[decode] {event}")
    # 表が空(ヘッダごと無い)ことはあり得るが、列があれば先頭は時刻列
    assert result["columns"][:1] in ([], ["time_s"])
    assert all(isinstance(e["time_s"], float) for e in result["events"])
