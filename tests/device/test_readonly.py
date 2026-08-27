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
    get_meter_value,
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


# -- 10. 信号発生(read-only。出力状態は読むだけで変えない)------------------


def test_afg_state_answers(driver: ScopeDriver) -> None:
    """`:SOURce1` の設定一式が読めること(書き込みは一切行わない)。

    値そのものは利用者の設定次第なので、型と項目の揃いだけを検証する
    (実測値はレポートに残す)。
    """
    config = driver.get_afg_config(1)
    _report(f"[afg] ch1={config}")

    assert set(config) == {
        "channel",
        "output",
        "waveform",
        "impedance",
        "modulation",
        "frequency_hz",
        "amplitude_vpp",
        "offset_v",
        "phase_deg",
        "duty_percent",
        "symmetry_percent",
    }
    assert config["channel"] == 1
    assert isinstance(config["output"], bool)
    assert isinstance(config["waveform"], str)
    assert config["impedance"] in ("highz", "50")
    assert all(
        isinstance(config[key], float)
        for key in (
            "frequency_hz",
            "amplitude_vpp",
            "offset_v",
            "phase_deg",
            "duty_percent",
            "symmetry_percent",
        )
    )


# -- 11. MATH演算(read-only。表示・設定は一切変えない)---------------------

#: どの演算子でも返る共通キー
MATH_COMMON_KEYS = frozenset(
    {"channel", "display", "operator", "source1", "source2", "invert"}
)


def test_math_state_all_channels(driver: ScopeDriver) -> None:
    """MATH1〜4 の設定を読む(表示OFFのトレースも含めて全チャンネル)。

    2026-08-27 実測: 表示OFFのMATHチャンネルへ `:MATH<n>:DISPlay?` 等を送っても
    SCPIサーバーは沈黙しない(mho98-math.md (a))。ここはその回帰確認も兼ねる。
    """
    assert driver.math_channels == 4

    for number in range(1, driver.math_channels + 1):
        config = driver.get_math_config(number)
        _report(f"[math] math{number}={config}")

        assert config["channel"] == number
        assert MATH_COMMON_KEYS <= set(config)
        assert isinstance(config["display"], bool)
        assert isinstance(config["operator"], str)
        assert isinstance(config["invert"], bool)


def test_math_fft_peak_table_and_frequency_axis(
    driver: ScopeDriver, device_config: Config
) -> None:
    """FFT演算のMATHがあれば、ピーク表と周波数軸メタデータを読む。

    read-onlyスイートなので**FFTの設定は行わない**。利用者が前面パネル等で
    FFTを組んでいるときだけ実行し、そうでなければskipする(write側の
    `test_configure_math_round_trip` が設定つきの経路を担う)。
    """
    fft_channel = next(
        (
            n
            for n in range(1, driver.math_channels + 1)
            if driver.get_math_config(n)["operator"] == "fft"
        ),
        None,
    )
    if fft_channel is None:
        pytest.skip("FFT演算のMATHトレースがありません(前面パネルで設定が必要)")

    config = driver.get_math_config(fft_channel)
    _report(f"[math] math{fft_channel} fft={config['fft']}")

    # ピーク表は複数行応答(改行区切り + 終端の空行)。1行読みだと切り詰められる
    if config["fft"]["search_enabled"]:
        peaks = config["peaks"]
        _report(f"[math] peaks={peaks}")
        assert "peak_warnings" not in config
        for peak in peaks:
            assert isinstance(peak["frequency_hz"], float)
            # 振幅の単位はSI接頭辞を外した形(`851.6mVrms` → 0.8516 `Vrms`)
            assert peak["amplitude_unit"] in ("Vrms", "dBV", "dBm")

    result = capture_waveform(driver, device_config, f"MATH{fft_channel}")
    _report(
        f"[math] capture MATH{fft_channel}: points={result['points']} "
        f"step={result['frequency_step_hz']:g} Hz "
        f"start={result['frequency_start_hz']:g} Hz"
    )

    assert result["x_unit"] == "Hz"
    assert "sample_interval_s" not in result
    assert "effective_sample_rate_sa_per_s" not in result
    # 実測の関係式: 点数 × 周波数刻み = 表示終端周波数
    end_hz = config["fft"]["freq_end_hz"]
    assert result["points"] * result["frequency_step_hz"] == pytest.approx(
        end_hz, rel=1e-3
    )


# -- 12. カーソル・計測器・ヒストグラム(read-only。設定は一切変えない)-----

#: カーソルの読み値キー(`off` / `xy` 以外のモードで返る)
CURSOR_READOUT_KEYS = frozenset(
    {"ax_s", "ay_v", "bx_s", "by_v", "xdelta_s", "ydelta_v", "ixdelta_hz"}
)
#: 計の種別 → 期待する単位の集合(tools.md 12章)
METER_UNITS = {
    "counter": {"Hz", "s", "counts"},
    "dvm": {"V"},
}


def test_cursor_config_and_measurement(driver: ScopeDriver) -> None:
    """カーソルの設定と読み値を読む(モードは変えない)。

    **カーソルOFFが通常の休息状態**であり、そこで壊れないことがこのテストの
    主眼。OFF / XY では位置サブツリーを持たないため `mode` だけが返る
    (mho98-m2.md 3章)。
    """
    config = driver.get_cursor_config()
    _report(f"[cursor] config={config}")
    assert config["mode"] in ("off", "manual", "track", "xy")

    measurement = driver.get_cursor_measurement()
    _report(f"[cursor] measurement={measurement}")
    assert measurement["mode"] == config["mode"]

    if config["mode"] in ("off", "xy"):
        # 非活性のサブツリーは問い合わせない = 値のキーは付かない
        assert set(measurement) == {"mode"}
        return

    assert CURSOR_READOUT_KEYS <= set(measurement)
    for key in CURSOR_READOUT_KEYS:
        value = measurement[key]
        # 測定不能の番兵値(±9.9E37)と空応答は None に落ちる
        assert value is None or isinstance(value, float), (key, value)


@pytest.mark.parametrize("kind", ["counter", "dvm"])
def test_meter_value_answers(driver: ScopeDriver, kind: str) -> None:
    """周波数カウンタ / 電圧計を読む(有効化はしない)。

    **無効が通常の休息状態**であり、そこが実装を壊した経路そのものである
    (無効な電圧計の `:DVM:CURRent?` は空応答 → mho98-m2.md 1章)。
    無効なら `value` は None、有効なら数値が返ること。
    """
    result = get_meter_value(driver, kind)
    _report(f"[meter:{kind}] {result}")

    assert result["kind"] == kind
    assert isinstance(result["enabled"], bool)
    assert isinstance(result["source"], str)
    assert isinstance(result["mode"], str)
    assert result["unit"] in METER_UNITS[kind]

    value = result["value"]
    if not result["enabled"]:
        assert value is None, "無効な計は現在値を問い合わせない"
    else:
        assert value is None or isinstance(value, float)

    if kind == "counter":
        assert isinstance(result["digits"], int)
        assert isinstance(result["totalize_enabled"], bool)


def test_histogram_config_and_result(driver: ScopeDriver) -> None:
    """ヒストグラムの設定と統計を読む(有効化はしない)。

    **無効が通常の休息状態**で、無効時の統計クエリは `[]` を返しつつ
    エラーキューに `-200` を積む(mho98-m2.md 1章)。実装は無効なら
    クエリ自体を送らないため、**このテストの後でエラーキューが汚れて
    いないこと**まで確認する。
    """
    config = driver.get_histogram_config()
    _report(f"[histogram] config={config}")

    assert isinstance(config["enabled"], bool)
    assert config["type"] in ("horizontal", "vertical")
    assert isinstance(config["source"], str)
    assert isinstance(config["height"], int)
    for key in ("left_s", "right_s", "bottom_v", "top_v"):
        assert isinstance(config[key], float), key
    assert config["left_s"] < config["right_s"]
    assert config["bottom_v"] < config["top_v"]

    result = driver.get_histogram_result()
    _report(f"[histogram] result={result}")
    assert isinstance(result["raw"], str)

    if not config["enabled"]:
        assert result["raw"] == ""
        assert any("disabled" in w for w in result["warnings"])
    else:
        # 単一行・終端の空行なし(query_lines では固まる)
        assert result["raw"].startswith("[")
        for key, value in result.get("stats", {}).items():
            assert isinstance(value, (float, str)), key

    # 無効時に統計クエリを送っていないことの回帰確認
    drained = driver.session.drain_error_queue()
    _report(f"[histogram] 読み取り後のエラーキュー: {drained}")
    assert drained == []
