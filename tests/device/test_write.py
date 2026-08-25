"""実機write検証スイート(T14b)。

実機に対して **設定変更 → read-back検証 → 必ず復元** を行う。全ての書き込みは
ControlService 経由で行い、サービス層(requested/applied・監査ログ)ごと実機で
検証する。

安全のため、本スイートは次の操作を**一切行わない**(Fakeテストで担保済み):

- 50Ω入力(`impedance="50"` / `FIFT`) — 耐圧が低く、誤接続時に機器が壊れる
- autoset(`:AUToscale`) — 利用者の設定を破壊する
- factory reset

対象チャンネルは **CH2** を使う。CH1 はプローブ補正信号の観測に使われているため、
一時的にでも表示を乱さない(それでも変更した項目は必ず復元する)。

接続先は環境変数 `RIGOL_TEST_ADDRESS` からのみ取得する。実機のアドレスを
リポジトリに残さないため、テストコードに直書きしてはならない
(`tests/test_ip_guard.py` が機械的に検査している)。

二重ゲート: `RIGOL_TEST_ADDRESS`(device)+ `RIGOL_TEST_ALLOW_WRITE=1`
(device_write)。いずれか欠ければ `tests/conftest.py` が一括skipする。

実測値はレポート用に収集し、セッション終了時にまとめて出力する
(`-s` 付きで実行すると逐次見える)。
"""

from __future__ import annotations

import json
import os
import time
from collections.abc import Iterator
from pathlib import Path

import pytest

from rigol_oscilloscope_mcp.config import Config
from rigol_oscilloscope_mcp.driver.scope import ScopeDriver
from rigol_oscilloscope_mcp.errors import ErrorCode, ScopeError
from rigol_oscilloscope_mcp.safety import AuditLogger, ConfirmTokenStore
from rigol_oscilloscope_mcp.service import (
    ConnectionManager,
    ControlService,
    analyze_waveform,
    get_acquisition_dict,
    get_channel_dict,
    get_timebase_dict,
    get_trigger_dict,
)

pytestmark = [pytest.mark.device, pytest.mark.device_write]

ADDRESS_ENV = "RIGOL_TEST_ADDRESS"

#: 書き込み対象チャンネル。CH1(プローブ補正信号)は触らない。
TEST_CHANNEL = "CH2"

#: 浮動小数の一致判定(機器のNR3応答は有効数字7桁程度)
REL_TOLERANCE = 1e-6
ABS_TOLERANCE = 1e-12

#: phase0 で verbatim 適用が確認済みの値(1-2-5スナップが無いことの再確認)
VERBATIM_SCALE_V_PER_DIV = 3.0
VERBATIM_TIMEBASE_S_PER_DIV = 3e-4
TIMEBASE_POSITION_S = 0.001

#: 機器が受理しないかもしれない中間値(丸め挙動の観測用。assertは緩い)
ODD_SCALE_V_PER_DIV = 3.3

#: デコード検証に使うバスと設定値(BUS1。設定は表示・解析層のみに効く)
DECODE_BUS = 1
DECODE_BAUD_BPS = 115200
#: 標準搭載プロトコル(オプション必須のものはプロファイルに無い)
DECODE_PROTOCOLS = ("uart", "i2c", "spi", "can", "lin", "parallel")

#: 信号発生(AFG)の検証チャンネルと値。**設定系のテストは出力に触れない**
AFG_CHANNEL = 1
AFG_FREQUENCY_HZ = 2000.0
AFG_AMPLITUDE_VPP = 1.0
AFG_DUTY_PERCENT = 60.0
#: 周波数を持たない波形(実機は書き込みを -200 で拒否する)
AFG_NO_FREQUENCY = ("dc", "noise")

#: 出力ON検証(PR-AFG2)の**追加ゲート**。device_write の2重ゲートに加えて必要で、
#: 「AFG出力に何も接続していない」ことを人が確認したうえでのみ渡す
AFG_OUTPUT_ENV = "RIGOL_TEST_ALLOW_AFG_OUTPUT"
#: ループバック(AFG出力 → CH1 をBNCケーブルで接続)検証の**さらに追加のゲート**
AFG_LOOPBACK_ENV = "RIGOL_TEST_AFG_LOOPBACK"

#: 出力ONで出す信号(開放でもループバックでも同じ: 1 kHz / 1 Vpp / オフセット0)
AFG_OUTPUT_WAVEFORM = "sine"
AFG_OUTPUT_FREQUENCY_HZ = 1000.0
AFG_OUTPUT_AMPLITUDE_VPP = 1.0

#: ループバック時の観測条件(CH1で受ける)
LOOPBACK_CHANNEL = "CH1"
LOOPBACK_SCALE_V_PER_DIV = 0.5
#: 画面レコード長 = 10 div × 5 ms = 50 ms → FFT分解能 ≈ 20 Hz = 1 kHzの2%。
#: `:WAVeform:MODE NORMal`(画面データ)なので分解能はレコード長だけで決まり、
#: これより速いタイムベース(例 500 µs/div → 200 Hz刻み)では±2%判定が原理的に
#: できない。テスト側でも frequency_resolution_hz を assert して取り違えを防ぐ
LOOPBACK_TIMEBASE_S_PER_DIV = 5e-3
LOOPBACK_TOLERANCE = 0.02
#: 出力ON後に波形が安定するまでの待ち(取り込みは run → 待ち → stop)
LOOPBACK_SETTLE_S = 1.0

requires_afg_output = pytest.mark.skipif(
    os.environ.get(AFG_OUTPUT_ENV) != "1",
    reason=f"{AFG_OUTPUT_ENV}=1 が未設定(AFG出力ONは明示ゲートの下でのみ行う)",
)
requires_afg_loopback = pytest.mark.skipif(
    os.environ.get(AFG_LOOPBACK_ENV) != "1",
    reason=f"{AFG_LOOPBACK_ENV}=1 が未設定(AFG出力→CH1のBNC接続が必要)",
)

#: `:SINGle` 直後に許容するトリガ状態(実測: WAIT。すぐトリガすると TD を経て STOP)
SINGLE_STATUSES = ("WAIT", "TD")

#: `:RUN` / `:STOP` の反映待ち。実測で `:RUN` 直後の1回目の `:TRIGger:STATus?` は
#: まだ STOP を返し、約0.2秒後に TD へ変わる(機器側の再アーム待ち)。
ACQUISITION_SETTLE_S = 3.0
ACQUISITION_POLL_S = 0.1

#: 状態dictのうち、比較対象から外すキー(実行のたびに変わる)
VOLATILE_KEYS = frozenset({"status", "trigger_status", "sample_rate_sa_per_s", "memory_depth"})

_REPORT: list[str] = []


def _report(line: str) -> None:
    """レポート行を記録しつつ、その場でも出力する(`-s` で可視)。"""
    _REPORT.append(line)
    print(line)


def _same(left: object, right: object) -> bool:
    """read-back値の一致判定(floatは許容誤差つき)。"""
    if isinstance(left, float) or isinstance(right, float):
        if not isinstance(left, (int, float)) or not isinstance(right, (int, float)):
            return False
        return left == pytest.approx(right, rel=REL_TOLERANCE, abs=ABS_TOLERANCE)
    return left == right


def _diff(before: dict, after: dict) -> dict[str, tuple[object, object]]:
    """復元漏れの検出用。揮発キーは無視する。"""
    return {
        key: (value, after.get(key))
        for key, value in before.items()
        if key not in VOLATILE_KEYS and not _same(value, after.get(key))
    }


# -- session fixtures ------------------------------------------------------


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
def driver(manager: ConnectionManager) -> ScopeDriver:
    return manager.require_scope()


@pytest.fixture(scope="session")
def generation(manager: ConnectionManager) -> int:
    return manager.generation


@pytest.fixture(scope="session")
def audit_path(tmp_path_factory: pytest.TempPathFactory) -> Path:
    return tmp_path_factory.mktemp("audit") / "device-write.jsonl"


@pytest.fixture(scope="session")
def control(audit_path: Path) -> ControlService:
    """本番と同じ構成(confirmトークン + JSONL監査)のサービス。"""
    return ControlService(ConfirmTokenStore(), AuditLogger(audit_path))


@pytest.fixture(scope="session", autouse=True)
def report_summary() -> Iterator[None]:
    yield
    if not _REPORT:
        return
    print("\n" + "=" * 72)
    print("実機write検証レポート(T14b)")
    print("=" * 72)
    for line in _REPORT:
        print(line)


# -- 復元fixture -----------------------------------------------------------


def _wait_running(driver: ScopeDriver, expected: bool) -> dict:
    """取り込み状態が `expected` になるまで待つ(タイムアウトしても最後の値を返す)。

    `:RUN` の反映は即時ではないため(実測: 直後の1クエリはまだ STOP)、
    1回のクエリで判定すると偽陰性になる。
    """
    deadline = time.monotonic() + ACQUISITION_SETTLE_S
    state = get_acquisition_dict(driver)
    while state["running"] is not expected and time.monotonic() < deadline:
        time.sleep(ACQUISITION_POLL_S)
        state = get_acquisition_dict(driver)
    return state


def _restore_channel(
    control: ControlService,
    driver: ScopeDriver,
    generation: int,
    before: dict,
    tag: str,
    channel: str = TEST_CHANNEL,
) -> None:
    """チャンネル(既定はCH2)の可変項目を元の値へ戻す。

    順序に意味がある:

    - `enabled` は**先に ON** にする(実測: 表示OFFのチャンネルへの
      `:CHANnel<n>:SCALe` / `:OFFSet` はエラーにならず黙って無視される)
    - `probe_ratio` は scale / offset の読み値そのものを変えるため、値の復元より先
    - `enabled` を元へ戻すのは**最後**

    途中で失敗しても残りの復元は試み、最後にまとめて失敗させる。
    """
    failures: list[str] = []
    steps: list[dict] = [
        {"enabled": True},
        {"probe_ratio": before["probe_ratio"]},
        {
            "scale_v_per_div": before["scale_v_per_div"],
            "offset_v": before["offset_v"],
            "coupling": before["coupling"],
        },
        {"enabled": before["enabled"]},
    ]
    for step in steps:
        try:
            control.configure_channel(driver, generation, channel, **step)
        except Exception as exc:  # 復元は最後まで試みる
            failures.append(f"{step} -> {exc!r}")

    after = get_channel_dict(driver, channel)
    drift = _diff(before, after)
    _report(f"[restore:{tag}] {channel} 復元後={after}")
    if drift:
        _report(f"[restore:{tag}] **復元漏れ**: {drift}")
    if failures:
        _report(f"[restore:{tag}] **復元コマンド失敗**: {failures}")
    assert not failures, f"{channel} の復元に失敗: {failures}"
    assert not drift, f"{channel} が復元されていません: {drift}"


@pytest.fixture
def channel_before(
    request: pytest.FixtureRequest,
    control: ControlService,
    driver: ScopeDriver,
    generation: int,
) -> Iterator[dict]:
    """CH2の現在値を取得し、表示をONにしてから yield、teardownで必ず復元する。

    表示をONにするのは機器の実測挙動のため: **表示OFFのチャンネルへの
    `:CHANnel<n>:SCALe` / `:OFFSet` は、エラーを返さずに無視される**
    (`:COUPling` / `:PROBe` はOFFでも適用される)。CH2 は既定でOFFのことが多く、
    ONにしないと垂直軸の書き込み検証そのものが成立しない。`enabled` も
    teardown で元の値へ戻す。

    テストが失敗して途中で抜けても teardown は実行されるため、復元は保証される。
    """
    before = get_channel_dict(driver, TEST_CHANNEL)
    tag = request.node.name
    _report(f"[before:{tag}] {TEST_CHANNEL}={before}")
    try:
        if not before["enabled"]:
            enabled = control.configure_channel(
                driver, generation, TEST_CHANNEL, enabled=True
            )
            _report(f"[before:{tag}] 表示をONにした(applied={enabled['applied']})")
        yield before
    finally:
        _restore_channel(control, driver, generation, before, tag)


@pytest.fixture
def timebase_before(
    request: pytest.FixtureRequest, control: ControlService, driver: ScopeDriver
) -> Iterator[dict]:
    """水平軸の現在値を取得し、teardownで必ず復元する。"""
    before = get_timebase_dict(driver)
    tag = request.node.name
    _report(f"[before:{tag}] timebase={before}")
    try:
        yield before
    finally:
        failure: Exception | None = None
        try:
            control.configure_timebase(
                driver,
                scale_s_per_div=before["scale_s_per_div"],
                position_s=before["position_s"],
            )
        except Exception as exc:
            failure = exc

        after = get_timebase_dict(driver)
        drift = _diff(before, after)
        _report(f"[restore:{tag}] timebase 復元後={after}")
        if drift:
            _report(f"[restore:{tag}] **復元漏れ**: {drift}")
        if failure is not None:
            _report(f"[restore:{tag}] **復元コマンド失敗**: {failure!r}")
        assert failure is None, f"timebase の復元に失敗: {failure!r}"
        assert not drift, f"timebase が復元されていません: {drift}"


@pytest.fixture
def trigger_before(
    request: pytest.FixtureRequest, control: ControlService, driver: ScopeDriver
) -> Iterator[dict]:
    """トリガの現在値を取得し、teardownで必ず復元する(sourceは変更しない)。"""
    before = get_trigger_dict(driver)
    tag = request.node.name
    _report(f"[before:{tag}] trigger={before}")
    try:
        yield before
    finally:
        failure: Exception | None = None
        try:
            control.configure_trigger(
                driver,
                level_v=before["level_v"],
                slope=before["slope"],
                sweep_mode=before["sweep_mode"],
            )
        except Exception as exc:
            failure = exc

        after = get_trigger_dict(driver)
        drift = _diff(before, after)
        _report(f"[restore:{tag}] trigger 復元後={after}")
        if drift:
            _report(f"[restore:{tag}] **復元漏れ**: {drift}")
        if failure is not None:
            _report(f"[restore:{tag}] **復元コマンド失敗**: {failure!r}")
        assert failure is None, f"trigger の復元に失敗: {failure!r}"
        assert not drift, f"trigger が復元されていません: {drift}"


# -- 1. CH2 scale(verbatim適用の再確認)-----------------------------------


def test_channel_scale_is_applied_verbatim(
    control: ControlService,
    driver: ScopeDriver,
    generation: int,
    channel_before: dict,
) -> None:
    """3 V/div がそのまま適用されること(phase0: 1-2-5スナップ無し)。"""
    started = time.perf_counter()
    result = control.configure_channel(
        driver, generation, TEST_CHANNEL, scale_v_per_div=VERBATIM_SCALE_V_PER_DIV
    )
    elapsed = time.perf_counter() - started

    applied = result["applied"]["scale_v_per_div"]
    _report(
        f"[scale] before={channel_before['scale_v_per_div']} "
        f"requested={VERBATIM_SCALE_V_PER_DIV} applied={applied} "
        f"changed={result['changed']} 所要 {elapsed:.3f}s"
    )

    assert result["channel"] == TEST_CHANNEL
    assert result["requested"] == {"scale_v_per_div": VERBATIM_SCALE_V_PER_DIV}
    # verbatim: 1-2-5 へスナップしない(2.0 や 5.0 に化けない)
    assert applied == pytest.approx(VERBATIM_SCALE_V_PER_DIV, rel=REL_TOLERANCE)
    assert get_channel_dict(driver, TEST_CHANNEL)["scale_v_per_div"] == pytest.approx(
        VERBATIM_SCALE_V_PER_DIV, rel=REL_TOLERANCE
    )


# -- 2. CH2 offset ---------------------------------------------------------


def test_channel_offset_set_and_readback(
    control: ControlService,
    driver: ScopeDriver,
    generation: int,
    channel_before: dict,
) -> None:
    """オフセットを1目盛ぶん動かし、read-backで確認する。

    絶対値を固定すると現在の V/div 次第でレンジ外になりうるため、
    「現在値 + 1div」という必ず表示範囲に入る値を使う。
    """
    target = channel_before["offset_v"] + channel_before["scale_v_per_div"]

    started = time.perf_counter()
    result = control.configure_channel(
        driver, generation, TEST_CHANNEL, offset_v=target
    )
    elapsed = time.perf_counter() - started

    applied = result["applied"]["offset_v"]
    _report(
        f"[offset] before={channel_before['offset_v']} requested={target} "
        f"applied={applied} changed={result['changed']} 所要 {elapsed:.3f}s"
    )

    assert applied == pytest.approx(target, rel=1e-4, abs=1e-6)
    assert result["changed"] is True


# -- 3. CH2 coupling -------------------------------------------------------


def test_channel_coupling_set_and_readback(
    control: ControlService,
    driver: ScopeDriver,
    generation: int,
    channel_before: dict,
) -> None:
    """カップリングを切り替える(既定は DC なので AC へ)。"""
    target = "AC" if channel_before["coupling"] != "AC" else "DC"

    started = time.perf_counter()
    result = control.configure_channel(driver, generation, TEST_CHANNEL, coupling=target)
    elapsed = time.perf_counter() - started

    applied = result["applied"]["coupling"]
    _report(
        f"[coupling] before={channel_before['coupling']!r} requested={target!r} "
        f"applied={applied!r} changed={result['changed']} 所要 {elapsed:.3f}s"
    )

    assert applied == target
    assert get_channel_dict(driver, TEST_CHANNEL)["coupling"] == target


# -- 4. CH2 probe_ratio ----------------------------------------------------


def test_channel_probe_ratio_set_and_readback(
    control: ControlService,
    driver: ScopeDriver,
    generation: int,
    channel_before: dict,
) -> None:
    """プローブ減衰比を現在値と異なる値(1 ⇔ 10)へ切り替える。"""
    target = 1.0 if channel_before["probe_ratio"] != 1.0 else 10.0

    started = time.perf_counter()
    result = control.configure_channel(
        driver, generation, TEST_CHANNEL, probe_ratio=target
    )
    elapsed = time.perf_counter() - started

    applied = result["applied"]["probe_ratio"]
    after = get_channel_dict(driver, TEST_CHANNEL)
    _report(
        f"[probe_ratio] before={channel_before['probe_ratio']} requested={target} "
        f"applied={applied} changed={result['changed']} 所要 {elapsed:.3f}s"
    )
    # probe比変更は V/div の読み値そのものを変える(復元順の根拠)
    _report(
        f"[probe_ratio] 連動: scale_v_per_div "
        f"{channel_before['scale_v_per_div']} -> {after['scale_v_per_div']}"
    )

    assert applied == pytest.approx(target, rel=REL_TOLERANCE)
    assert after["probe_ratio"] == pytest.approx(target, rel=REL_TOLERANCE)


# -- 5. timebase -----------------------------------------------------------


def test_timebase_scale_and_position(
    control: ControlService, driver: ScopeDriver, timebase_before: dict
) -> None:
    """0.3 ms/div がそのまま適用されること + position の set→readback。"""
    started = time.perf_counter()
    scale_result = control.configure_timebase(
        driver, scale_s_per_div=VERBATIM_TIMEBASE_S_PER_DIV
    )
    scale_elapsed = time.perf_counter() - started

    applied_scale = scale_result["applied"]["scale_s_per_div"]
    _report(
        f"[timebase.scale] before={timebase_before['scale_s_per_div']:g} "
        f"requested={VERBATIM_TIMEBASE_S_PER_DIV:g} applied={applied_scale:g} "
        f"changed={scale_result['changed']} 所要 {scale_elapsed:.3f}s"
    )
    # verbatim: 1-2-5 へスナップしない
    assert applied_scale == pytest.approx(VERBATIM_TIMEBASE_S_PER_DIV, rel=REL_TOLERANCE)

    started = time.perf_counter()
    position_result = control.configure_timebase(driver, position_s=TIMEBASE_POSITION_S)
    position_elapsed = time.perf_counter() - started

    applied_position = position_result["applied"]["position_s"]
    after = get_timebase_dict(driver)
    _report(
        f"[timebase.position] before={timebase_before['position_s']:g} "
        f"requested={TIMEBASE_POSITION_S:g} applied={applied_position:g} "
        f"changed={position_result['changed']} 所要 {position_elapsed:.3f}s"
    )
    _report(f"[timebase] 変更後の全状態={after}")

    assert applied_position == pytest.approx(TIMEBASE_POSITION_S, rel=1e-4, abs=1e-9)
    assert after["position_s"] == pytest.approx(applied_position, rel=REL_TOLERANCE)


# -- 6. trigger(sourceは変更しない)---------------------------------------


def test_trigger_level_slope_sweep(
    control: ControlService, driver: ScopeDriver, trigger_before: dict
) -> None:
    """レベル・スロープ・スイープを変更し、trigger dict に反映されること。"""
    target_level = trigger_before["level_v"] + 0.1
    target_slope = "falling" if trigger_before["slope"] != "falling" else "rising"
    target_sweep = "normal" if trigger_before["sweep_mode"] != "normal" else "auto"

    started = time.perf_counter()
    result = control.configure_trigger(
        driver,
        level_v=target_level,
        slope=target_slope,
        sweep_mode=target_sweep,
    )
    elapsed = time.perf_counter() - started

    applied = result["applied"]
    trigger = result["trigger"]
    _report(
        f"[trigger] level_v {trigger_before['level_v']:g} -> requested {target_level:g} "
        f"-> applied {applied['level_v']:g}"
    )
    _report(
        f"[trigger] slope {trigger_before['slope']!r} -> requested {target_slope!r} "
        f"-> applied {applied['slope']!r}"
    )
    _report(
        f"[trigger] sweep_mode {trigger_before['sweep_mode']!r} -> requested "
        f"{target_sweep!r} -> applied {applied['sweep_mode']!r}"
    )
    _report(f"[trigger] 変更後の全状態={trigger} 所要 {elapsed:.3f}s")

    # source は要求していない = 1コマンドも送っていない(CH1のまま)
    assert "source" not in result["requested"]
    assert trigger["source"] == trigger_before["source"]

    assert applied["level_v"] == pytest.approx(target_level, rel=1e-3, abs=1e-4)
    assert applied["slope"] == target_slope
    assert applied["sweep_mode"] == target_sweep
    assert trigger["type"] == "edge"


# -- 7. run / stop / single ------------------------------------------------


def test_run_stop_single(
    control: ControlService, driver: ScopeDriver, trigger_before: dict
) -> None:
    """取り込み制御。最後は必ず run + 元のsweep_modeへ戻す。

    `:SINGle` は機器側で sweep を SINGle に切り替えるため、復元は
    trigger_before fixture(teardown)と本テスト末尾の run で担保する。
    """
    acquisition_before = get_acquisition_dict(driver)
    _report(
        f"[acquisition] before={acquisition_before} "
        f"sweep_mode={trigger_before['sweep_mode']!r}"
    )

    try:
        stop_result = control.stop(driver)
        stopped = _wait_running(driver, False)
        _report(f"[acquisition] stop -> {stop_result} state={stopped}")
        assert stop_result["result"] == "ok"
        assert stopped["running"] is False

        run_result = control.run(driver)
        running = _wait_running(driver, True)
        _report(f"[acquisition] run -> {run_result} state={running}")
        assert run_result["result"] == "ok"
        assert running["running"] is True

        single_result = control.single(driver)
        single_state = get_acquisition_dict(driver)
        _report(f"[acquisition] single -> {single_result} state(直後)={single_state}")
        assert single_result["result"] == "ok"
        assert single_result["trigger_status"] in SINGLE_STATUSES, (
            f"single 直後のトリガ状態が想定外: {single_result['trigger_status']!r}"
        )
        # 単発取り込みは完了すると STOP へ落ちる(信号があると WAIT はごく短い)
        _report(f"[acquisition] single 完了後={_wait_running(driver, False)}")
    finally:
        # 機器を連続取り込みへ戻す(sweep_mode は trigger_before の teardown が復元)
        final = control.run(driver)
        restored = _wait_running(driver, True)
        _report(f"[acquisition] 復元: run -> {final} state={restored}")

    assert final["result"] == "ok"
    assert restored["running"] is True


# -- 8. requested ≠ applied の観測(レポート重視、assertは緩く)-------------


def test_odd_scale_value_is_observed(
    control: ControlService,
    driver: ScopeDriver,
    generation: int,
    channel_before: dict,
) -> None:
    """機器が受理しないかもしれない中間値を送り、丸め挙動を記録する。

    assertは「エラーにならないこと」のみ。目的は実機挙動の観測であって、
    特定の丸め結果を仕様として固定することではない。
    """
    started = time.perf_counter()
    result = control.configure_channel(
        driver, generation, TEST_CHANNEL, scale_v_per_div=ODD_SCALE_V_PER_DIV
    )
    elapsed = time.perf_counter() - started

    applied = result["applied"]["scale_v_per_div"]
    verbatim = applied == pytest.approx(ODD_SCALE_V_PER_DIV, rel=REL_TOLERANCE)
    _report(
        f"[odd_value] requested={ODD_SCALE_V_PER_DIV} applied={applied} "
        f"verbatim={verbatim} 所要 {elapsed:.3f}s"
    )
    if not verbatim:
        _report(
            f"[odd_value] **requested ≠ applied**: 機器が {ODD_SCALE_V_PER_DIV} を "
            f"{applied} に丸めた(requested/applied 両値返却の実機的根拠)"
        )

    assert isinstance(applied, float)
    assert applied > 0


# -- 9. シリアルデコード(tools.md 6章)-------------------------------------


@pytest.fixture
def decode_before(
    request: pytest.FixtureRequest, driver: ScopeDriver
) -> Iterator[dict]:
    """BUS1の現在設定を丸ごと控え、teardownで必ず書き戻す。

    デコード設定は表示・解析層のみで取り込み設定を変えないが、それでも
    利用者の設定であることに変わりはないので他のwrite検証と同じ規律で扱う。
    プロトコルがオプション必須(未対応)の場合はテスト自体をskipする。
    """
    before = driver.get_decode_config(DECODE_BUS)
    tag = request.node.name
    _report(f"[before:{tag}] bus{DECODE_BUS}={before}")
    if before["protocol"] not in DECODE_PROTOCOLS:
        pytest.skip(f"BUS{DECODE_BUS} は未対応プロトコル({before['protocol']})")
    try:
        yield before
    finally:
        failure: Exception | None = None
        try:
            driver.configure_decode(
                DECODE_BUS,
                before["protocol"],
                enabled=before["enabled"],
                event_table=before["event_table"],
                data_format=before["data_format"],
                settings=before["settings"],
            )
        except Exception as exc:  # 復元は最後まで試みる
            failure = exc

        after = driver.get_decode_config(DECODE_BUS)
        drift = _diff(before, after)
        _report(f"[restore:{tag}] bus{DECODE_BUS} 復元後={after}")
        if drift:
            _report(f"[restore:{tag}] **復元漏れ**: {drift}")
        if failure is not None:
            _report(f"[restore:{tag}] **復元コマンド失敗**: {failure!r}")
        assert failure is None, f"BUS{DECODE_BUS} の復元に失敗: {failure!r}"
        assert not drift, f"BUS{DECODE_BUS} が復元されていません: {drift}"


def test_configure_decode_uart_set_and_readback(
    control: ControlService,
    driver: ScopeDriver,
    decode_before: dict,
) -> None:
    """UARTデコードを設定し、read-backで確認する(復元はfixtureが行う)。

    ソースは CH2 を使う(CH1 はプローブ補正信号の観測に使われているため)。
    """
    started = time.perf_counter()
    result = control.configure_decode(
        driver,
        DECODE_BUS,
        "uart",
        settings={
            "tx_source": TEST_CHANNEL,
            "rx_source": "off",
            "baud_bps": DECODE_BAUD_BPS,
            "data_bits": 8,
            "parity": "none",
            "stop_bits": 1,
        },
    )
    elapsed = time.perf_counter() - started

    applied = result["applied"]["settings"]
    _report(
        f"[decode] bus{DECODE_BUS} applied={applied} "
        f"changed={result['changed']} 所要 {elapsed:.3f}s"
    )

    assert result["applied"]["protocol"] == "uart"
    assert applied["tx_source"] == TEST_CHANNEL
    assert applied["baud_bps"] == DECODE_BAUD_BPS
    assert applied["parity"] == "none"

    readback = driver.get_decode_config(DECODE_BUS)
    assert readback["protocol"] == "uart"
    assert readback["settings"]["baud_bps"] == DECODE_BAUD_BPS


# -- 10. 信号発生(出力は一切ONにしない)-------------------------------------


@pytest.fixture
def afg_before(request: pytest.FixtureRequest, driver: ScopeDriver) -> Iterator[dict]:
    """AFG ch1 の現在設定を控え、teardownで必ず書き戻す。

    出力がONの状態では検証しない(本スイートは出力に触れないため、ONのまま
    設定を動かすと外部の被測定回路へ出る信号が変わってしまう)。
    """
    before = driver.get_afg_config(AFG_CHANNEL)
    tag = request.node.name
    _report(f"[before:{tag}] afg{AFG_CHANNEL}={before}")
    if before["output"]:
        pytest.skip("AFG出力がONです(出力ON状態では検証しない)")
    try:
        yield before
    finally:
        restore = {
            key: before[key]
            for key in (
                "waveform",
                "impedance",
                "amplitude_vpp",
                "offset_v",
                "phase_deg",
                "duty_percent",
                "symmetry_percent",
            )
        }
        if before["waveform"] not in AFG_NO_FREQUENCY:
            restore["frequency_hz"] = before["frequency_hz"]

        failure: Exception | None = None
        try:
            driver.configure_afg(AFG_CHANNEL, **restore)
        except Exception as exc:  # 復元は最後まで試みる
            failure = exc

        after = driver.get_afg_config(AFG_CHANNEL)
        drift = _diff(before, after)
        _report(f"[restore:{tag}] afg{AFG_CHANNEL} 復元後={after}")
        if drift:
            _report(f"[restore:{tag}] **復元漏れ**: {drift}")
        if failure is not None:
            _report(f"[restore:{tag}] **復元コマンド失敗**: {failure!r}")
        assert after["output"] is False, "AFG出力が変化しています"
        assert failure is None, f"AFG ch{AFG_CHANNEL} の復元に失敗: {failure!r}"
        assert not drift, f"AFG ch{AFG_CHANNEL} が復元されていません: {drift}"


def test_configure_afg_set_and_readback(
    control: ControlService,
    driver: ScopeDriver,
    afg_before: dict,
) -> None:
    """波形・周波数・振幅・デューティを設定し read-back で確認する。

    範囲外値は**エラーキューに何も積まずクランプされる**(mho98-afg.md 2章)ため、
    applied(read-back値)との突合が唯一の検出手段になる。復元はfixtureが行う。
    """
    started = time.perf_counter()
    result = control.configure_afg(
        driver,
        AFG_CHANNEL,
        waveform="square",
        frequency_hz=AFG_FREQUENCY_HZ,
        amplitude_vpp=AFG_AMPLITUDE_VPP,
        duty_percent=AFG_DUTY_PERCENT,
    )
    elapsed = time.perf_counter() - started

    applied = result["applied"]
    _report(
        f"[afg] ch{AFG_CHANNEL} applied={applied} "
        f"changed={result['changed']} 所要 {elapsed:.3f}s"
    )

    assert applied["waveform"] == "square"
    assert applied["frequency_hz"] == pytest.approx(AFG_FREQUENCY_HZ, rel=REL_TOLERANCE)
    assert applied["amplitude_vpp"] == pytest.approx(
        AFG_AMPLITUDE_VPP, rel=REL_TOLERANCE
    )
    assert applied["duty_percent"] == pytest.approx(
        AFG_DUTY_PERCENT, rel=REL_TOLERANCE
    )

    readback = driver.get_afg_config(AFG_CHANNEL)
    assert readback["waveform"] == "square"
    assert readback["frequency_hz"] == pytest.approx(
        AFG_FREQUENCY_HZ, rel=REL_TOLERANCE
    )
    # 設定Toolは出力状態に触れない(検証中ずっとOFFのまま)
    assert readback["output"] is False


# -- 10b. 信号発生の出力制御(追加ゲート必須)--------------------------------


def _enable_afg_confirmed(
    control: ControlService,
    driver: ScopeDriver,
    generation: int,
    channel: int = AFG_CHANNEL,
) -> dict:
    """確認フローを2段階とも実機で通し、出力をONにする。

    1段目(トークン無し)は**機器へ1コマンドも送らずに** USER_CONFIRMATION_REQUIRED
    で中断すること自体を検証する。2段目でそのトークンを渡して初めて出力が出る。
    """
    with pytest.raises(ScopeError) as excinfo:
        control.enable_afg(driver, generation, channel)

    assert excinfo.value.code == ErrorCode.USER_CONFIRMATION_REQUIRED
    token = excinfo.value.detail["confirm_token"]
    assert driver.get_afg_config(channel)["output"] is False, "承認前に出力がONになった"

    return control.enable_afg(driver, generation, channel, confirm_token=token)


@pytest.fixture
def loopback_channel_before(
    request: pytest.FixtureRequest,
    control: ControlService,
    driver: ScopeDriver,
    generation: int,
) -> Iterator[dict]:
    """ループバック受信に使うCH1の現在値を控え、teardownで必ず復元する。"""
    before = get_channel_dict(driver, LOOPBACK_CHANNEL)
    tag = request.node.name
    _report(f"[before:{tag}] {LOOPBACK_CHANNEL}={before}")
    try:
        yield before
    finally:
        _restore_channel(
            control, driver, generation, before, tag, channel=LOOPBACK_CHANNEL
        )


@requires_afg_output
def test_enable_afg_output_open_circuit(
    control: ControlService,
    driver: ScopeDriver,
    generation: int,
    afg_before: dict,
) -> None:
    """**AFG出力に何も接続していない状態**で 出力ON → 確認 → OFF を1往復する。

    出力ONは DANGEROUS_WRITE なので、実機でも確認フローを2段階とも通す。
    finally で必ず出力をOFFへ戻し、設定の復元は afg_before fixture が行う。
    """
    assert afg_before["output"] is False

    configured = control.configure_afg(
        driver,
        AFG_CHANNEL,
        waveform=AFG_OUTPUT_WAVEFORM,
        frequency_hz=AFG_OUTPUT_FREQUENCY_HZ,
        amplitude_vpp=AFG_OUTPUT_AMPLITUDE_VPP,
        offset_v=0.0,
    )
    _report(f"[afg-output] 出力ON前の設定 applied={configured['applied']}")

    try:
        started = time.perf_counter()
        result = _enable_afg_confirmed(control, driver, generation)
        elapsed = time.perf_counter() - started

        _report(f"[afg-output] ON state={result['state']} 所要 {elapsed:.3f}s")
        assert result["result"] == "ok"
        assert result["state"]["output"] is True
        assert driver.get_afg_config(AFG_CHANNEL)["output"] is True
    finally:
        off = control.disable_afg(driver, AFG_CHANNEL)
        _report(f"[afg-output] OFF state={off['state']}")

    assert off["state"]["output"] is False
    assert driver.get_afg_config(AFG_CHANNEL)["output"] is False


@requires_afg_output
@requires_afg_loopback
def test_afg_loopback_fft(
    control: ControlService,
    driver: ScopeDriver,
    device_config: Config,
    generation: int,
    afg_before: dict,
    loopback_channel_before: dict,
    timebase_before: dict,
    trigger_before: dict,
) -> None:
    """AFG出力→CH1のBNCループバックで、出した周波数をFFTで取り戻せることを見る。

    要ケーブル(2重ゲートに加えて `RIGOL_TEST_AFG_LOOPBACK=1`)。取り込みは
    single ではなく run → 待ち → stop で行う: トリガ源は変更しない方針のため、
    未トリガでも画面が更新されるよう sweep を auto にして掃引を保証する
    (FFTは位相に依らないので、トリガの有無は周波数の判定に影響しない)。
    """
    assert afg_before["output"] is False

    control.configure_afg(
        driver,
        AFG_CHANNEL,
        waveform=AFG_OUTPUT_WAVEFORM,
        frequency_hz=AFG_OUTPUT_FREQUENCY_HZ,
        amplitude_vpp=AFG_OUTPUT_AMPLITUDE_VPP,
        offset_v=0.0,
    )
    control.configure_channel(
        driver,
        generation,
        LOOPBACK_CHANNEL,
        enabled=True,
        scale_v_per_div=LOOPBACK_SCALE_V_PER_DIV,
        coupling="DC",
    )
    control.configure_timebase(driver, scale_s_per_div=LOOPBACK_TIMEBASE_S_PER_DIV)
    control.configure_trigger(driver, sweep_mode="auto")

    try:
        result = _enable_afg_confirmed(control, driver, generation)
        assert result["state"]["output"] is True

        control.run(driver)
        time.sleep(LOOPBACK_SETTLE_S)
        control.stop(driver)

        analysis = analyze_waveform(
            driver, device_config, LOOPBACK_CHANNEL, analyses=["fft"]
        )
    finally:
        off = control.disable_afg(driver, AFG_CHANNEL)
        _report(f"[afg-loopback] OFF state={off['state']}")

    fft = analysis["fft"]
    dominant = fft["dominant_frequency_hz"]
    _report(
        f"[afg-loopback] points={analysis['points']} "
        f"分解能={fft['frequency_resolution_hz']:.1f}Hz "
        f"dominant={dominant}Hz peaks={fft['peaks'][:3]}"
    )

    # 判定条件そのものの妥当性を先に検証する(レコード長が短いと±2%は原理的に
    # 判定できず、失敗しても機器のせいではない)
    assert fft["frequency_resolution_hz"] <= LOOPBACK_TOLERANCE * AFG_OUTPUT_FREQUENCY_HZ
    assert dominant == pytest.approx(
        AFG_OUTPUT_FREQUENCY_HZ, rel=LOOPBACK_TOLERANCE
    )
    assert off["state"]["output"] is False


# -- 11. 監査ログ(最後に実行し、それまでの全操作を検証)--------------------


def test_audit_log_records_every_write(audit_path: Path) -> None:
    """一連の書き込みがJSONLとして記録されていること。"""
    assert audit_path.is_file(), "監査ログが作成されていません"

    lines = [line for line in audit_path.read_text(encoding="utf-8").splitlines() if line]
    assert lines, "監査ログが空です"

    entries = [json.loads(line) for line in lines]  # 1行でも壊れていれば失敗する
    tools: dict[str, int] = {}
    for entry in entries:
        assert "timestamp" in entry
        tool = entry.get("tool")
        assert isinstance(tool, str)
        tools[tool] = tools.get(tool, 0) + 1

    results = {entry.get("result") for entry in entries}
    _report(f"[audit] {len(entries)}行 tools={tools} results={sorted(map(str, results))}")

    # 書き込み記録は Before / Action / After が揃っていること
    for entry in entries:
        if entry.get("result") == "success":
            assert entry["before"] is not None
            assert entry["after"] is not None

    assert {
        "configure_channel",
        "configure_timebase",
        "configure_trigger",
        "configure_decode",
        "configure_afg",
    } <= set(tools)
    assert {"run", "stop", "single"} <= set(tools)
