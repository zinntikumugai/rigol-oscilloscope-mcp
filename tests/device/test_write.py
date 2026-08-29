"""実機write検証スイート(T14b)。

実機に対して **設定変更 → read-back検証 → 必ず復元** を行う。全ての書き込みは
ControlService 経由で行い、サービス層(requested/applied・監査ログ)ごと実機で
検証する。

安全のため、本スイートは次の操作を**一切行わない**(Fakeテストで担保済み):

- 50Ω入力(`impedance="50"` / `FIFT`) — 耐圧が低く、誤接続時に機器が壊れる
- autoset(`:AUToset`) — 利用者の設定を破壊する
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
from rigol_oscilloscope_mcp.driver.scope import _TRIGGER_SUBTREES, ScopeDriver
from rigol_oscilloscope_mcp.errors import ErrorCode, ScopeError
from rigol_oscilloscope_mcp.safety import AuditLogger, ConfirmTokenStore
from rigol_oscilloscope_mcp.service import (
    ConnectionManager,
    ControlService,
    analyze_waveform,
    capture_waveform,
    get_acquisition_dict,
    get_channel_dict,
    get_meter_value,
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
#: パラレル(データソース=User)の検証値。ビット別ソースはCH1を避ける
#: (CH1はプローブ補正信号の観測に使われているため)
DECODE_BUS_WIDTH = 2
DECODE_BIT_SOURCES = ("CH2", "CH3")

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

#: ループバック時の観測条件(物理結線: AFG1(G1)→BNC→CH2。CH1はプローブ補償専用)
LOOPBACK_CHANNEL = "CH2"
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

#: MATH演算の検証チャンネルと表示周波数範囲(表示・解析層のみに効く)
MATH_CHANNEL = 1
MATH_FFT_START_HZ = 0.0
MATH_FFT_END_HZ = 100000.0

#: カーソル・計測器・ヒストグラムの検証値(いずれも表示・統計層のみに効く)
CURSOR_AX_S = -2e-4
CURSOR_BX_S = 2e-4
CURSOR_AY_V = -0.5
CURSOR_BY_V = 0.5
#: TRACkモードのY位置は波形に追従して機器側で動く(mho98-m2.md 3章)。
#: 復元漏れの判定から外す(service/control.py の `changed` 判定と同じ理由)
CURSOR_TRACKED_KEYS = ("ay", "by")

METER_CHANNEL = "CH2"
COUNTER_DIGITS = 5
HISTOGRAM_HEIGHT = 2
HISTOGRAM_LEFT_S = -3e-4
HISTOGRAM_RIGHT_S = 3e-4
HISTOGRAM_BOTTOM_V = -1.0
HISTOGRAM_TOP_V = 1.0

#: リファレンス波形(3.20)。**復元可能な設定だけ**を触る枠と値
REF_SLOT = 10
REF_SOURCE = "CH2"
REF_SCALE_V_PER_DIV = 0.5
REF_OFFSET_V = -1.0
REF_COLOR = "orange"
REF_LABEL = "m3_probe"

#: `:REFerence:SAVE` は**不可逆**(枠に入っていた波形は戻せず、「入っているか」を
#: 問い合わせるコマンドも無い)。実機は利用者の作業機なので、既定では実行せず
#: 専用の環境変数を明示したときだけ走らせる(AFG出力ONと同じ二重ゲートの考え方)
REF_SAVE_ENV = "RIGOL_TEST_ALLOW_REF_SAVE"
requires_ref_save = pytest.mark.skipif(
    os.environ.get(REF_SAVE_ENV) != "1",
    reason=(
        f"{REF_SAVE_ENV}=1 が未設定"
        "(リファレンス保存は不可逆 = 明示ゲートの下でのみ行う)"
    ),
)

#: `:SINGle` 直後に許容するトリガ状態(実測: WAIT。すぐトリガすると TD を経て STOP)
#: `:SINGle` 直後に取りうるトリガ状態。**STOP を含めるのは競合を避けるため** —
#: 生きた信号(CH1のプローブ補償 1kHz)では単発取り込みが往復時間より速く完了し、
#: 最初の読みが既に STOP になることがある(実測)。WAIT(待機)/ TD(トリガ済)/
#: STOP(完了)のいずれも「単発取り込みに入った」ことを示すので受理する
SINGLE_STATUSES = ("WAIT", "TD", "STOP")

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
    """トリガの現在値を取得し、teardownで必ず復元する(sourceは変更しない)。

    **種別(`type`)も復元する。** M5で `:TRIGger:MODE` を切り替えられるように
    なったため、元の種別へ戻してからその配下を書き戻さないと、別のサブツリーに
    書き込んでしまう。`configure_trigger` は `type` を先頭に送るので1往復で足りる。
    """
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
                type=before["type"],
                sweep_mode=before["sweep_mode"],
                settings=before["settings"],
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
    # `bit_sources` まで含めて控える(この走査だけは `:BITX` の書き込みを伴う)
    before = driver.get_decode_config(DECODE_BUS, include_bit_sources=True)
    tag = request.node.name
    _report(f"[before:{tag}] bus{DECODE_BUS}={before}")
    if before["protocol"] not in DECODE_PROTOCOLS:
        pytest.skip(f"BUS{DECODE_BUS} は未対応プロトコル({before['protocol']})")
    try:
        yield before
    finally:
        failure: Exception | None = None
        # `bus_width` / `bit_sources`(:BUS<n>:PARallel:WIDTh / :BITX + :SOURce)は
        # **データソースが User のときだけ**書ける(ガイド3.4.10.4-3.4.10.6、
        # 実測 mho98-phase4.md 5章)。`configure_decode` の送信順は `bus` → `bus_width`
        # で固定されているので、1回の呼び出しでは「USERへ入って幅を戻す」と
        # 「本来の bus へ戻す」を同時に行えない。**2段**で書き戻す。
        settings = dict(before["settings"])
        user_only = {
            key: settings.pop(key)
            for key in ("bus_width", "bit_sources")
            if key in settings
        }
        try:
            if user_only:
                driver.configure_decode(
                    DECODE_BUS, before["protocol"], settings={"bus": "user", **user_only}
                )
            driver.configure_decode(
                DECODE_BUS,
                before["protocol"],
                enabled=before["enabled"],
                event_table=before["event_table"],
                data_format=before["data_format"],
                settings=settings,
            )
        except Exception as exc:  # 復元は最後まで試みる
            failure = exc

        after = driver.get_decode_config(DECODE_BUS, include_bit_sources=True)
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


def test_configure_decode_parallel_user_bus_set_and_readback(
    control: ControlService,
    driver: ScopeDriver,
    decode_before: dict,
) -> None:
    """パラレルの `bus` → `bus_width` → `bit_sources` を1呼び出しで往復させる。

    `:PARallel:WIDTh` は**データソースが User のときだけ**受理される
    (ガイド3.4.10.4、切り分けは mho98-phase4.md 5章)。`bus` を公開したことで
    前提を同一呼び出しで満たせるようになったことを実機で確認する。

    `bit_sources` はデータソースが User のバスでしか読めない(User以外では
    `:BITX` の書き込み自体が拒否される)ため、fixtureのスナップショットには
    含まれない。**Userへ入った直後の値をこのテスト自身で控えて書き戻す。**
    """
    control.configure_decode(driver, DECODE_BUS, "parallel")
    parallel_before = driver.get_decode_config(DECODE_BUS)["settings"]
    control.configure_decode(driver, DECODE_BUS, "parallel", settings={"bus": "user"})
    user_before = driver.get_decode_config(
        DECODE_BUS, include_bit_sources=True
    )["settings"]
    _report(f"[decode] bus{DECODE_BUS} parallel before={parallel_before} user={user_before}")

    try:
        started = time.perf_counter()
        result = control.configure_decode(
            driver,
            DECODE_BUS,
            "parallel",
            settings={
                "bus": "user",
                "bus_width": DECODE_BUS_WIDTH,
                "bit_sources": list(DECODE_BIT_SOURCES),
            },
        )
        elapsed = time.perf_counter() - started

        applied = result["applied"]["settings"]
        _report(
            f"[decode] bus{DECODE_BUS} parallel applied={applied} "
            f"changed={result['changed']} 所要 {elapsed:.3f}s"
        )

        assert applied["bus"] == "user"
        assert applied["bus_width"] == DECODE_BUS_WIDTH
        assert applied["bit_sources"] == list(DECODE_BIT_SOURCES)

        readback = driver.get_decode_config(
            DECODE_BUS, include_bit_sources=True
        )["settings"]
        assert readback["bus_width"] == DECODE_BUS_WIDTH
        assert readback["bit_sources"] == list(DECODE_BIT_SOURCES)
    finally:
        # User配下の値はUserのうちに戻し、そのあとで元のデータソースへ戻す
        control.configure_decode(
            driver,
            DECODE_BUS,
            "parallel",
            settings={
                "bus": "user",
                "bus_width": user_before["bus_width"],
                "bit_sources": user_before["bit_sources"],
            },
        )
        control.configure_decode(
            driver, DECODE_BUS, "parallel", settings={"bus": parallel_before["bus"]}
        )


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


def test_configure_afg_modulation_set_and_readback(
    control: ControlService,
    driver: ScopeDriver,
    afg_before: dict,
) -> None:
    """変調(AM 50% / 1kHz sine)を設定しread-backで確認する(出力はONにしない)。

    要実機検証: PULSe同様に範囲外値がエラーキュー無しでクランプされる可能性が
    あるため、他のAFG項目と同じくapplied(read-back値)との突合で確認する。
    """
    before_modulation = afg_before["modulation"]
    _report(f"[before:afg-mod] modulation={before_modulation}")

    try:
        started = time.perf_counter()
        result = control.configure_afg(
            driver,
            AFG_CHANNEL,
            modulation={
                "type": "am",
                "am_depth_percent": 50.0,
                "frequency_hz": 1000.0,
                "waveform": "sine",
                "enabled": True,
            },
        )
        elapsed = time.perf_counter() - started

        applied = result["applied"]["modulation"]
        _report(f"[afg-mod] ch{AFG_CHANNEL} applied={applied} 所要 {elapsed:.3f}s")

        assert applied["type"] == "am"
        assert applied["am_depth_percent"] == pytest.approx(50.0, rel=REL_TOLERANCE)
        assert applied["frequency_hz"] == pytest.approx(1000.0, rel=REL_TOLERANCE)
        assert applied["waveform"] == "sine"
        assert applied["enabled"] is True

        readback = driver.get_afg_config(AFG_CHANNEL)
        assert readback["modulation"]["enabled"] is True
        # 出力は設定Toolでは一切触れない
        assert readback["output"] is False
    finally:
        restore_type = before_modulation["type"]
        depth_key = {"am": "am_depth_percent", "fm": "fm_deviation_hz", "pm": "pm_deviation_deg"}[
            restore_type
        ]
        control.configure_afg(
            driver,
            AFG_CHANNEL,
            modulation={
                "type": restore_type,
                depth_key: before_modulation[depth_key],
                "frequency_hz": before_modulation["frequency_hz"],
                "waveform": before_modulation["waveform"],
                "enabled": before_modulation["enabled"],
            },
        )
        after_modulation = driver.get_afg_config(AFG_CHANNEL)["modulation"]
        _report(f"[restore:afg-mod] modulation={after_modulation}")
        assert after_modulation == before_modulation, "AFG変調が復元されていません"


def test_sync_afg_phase(control: ControlService, driver: ScopeDriver) -> None:
    """位相同期(`:PHASe:SYNChronize`)を呼び、エラーキューが汚れないことを見る。

    要実機検証: 両チャンネルの周波数・位相を再適用する副作用そのものは
    read-backでは観測できない(ガイドの記載通りの動作を前提とする)。
    """
    result = control.sync_afg_phase(driver, AFG_CHANNEL)
    _report(f"[afg-sync] result={result}")

    assert result == {"result": "ok"}


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
    """ループバック受信チャンネル(CH2)の現在値を控え、teardownで必ず復元する。"""
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
    """AFG1出力→CH2のBNCループバックで、出した周波数をFFTで取り戻せることを見る。

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
        probe_ratio=1.0,  # 受け側経路の減衰(ケーブル/プローブ)は周波数判定に影響しない
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


# -- 11. 測定Resultビューのクリア(issue #16)------------------------------


def test_clear_measurements(control: ControlService, driver: ScopeDriver) -> None:
    """項目を追加してからクリアする。復元は不要(再測定が復元そのもの)。

    **CH1に測定可能な信号があること**が前提。信号が無いと周波数は測定不能値に
    なり、Resultビューへの追加を確認できない(実測: AFG出力をOFFにした状態で
    このテストだけが落ちた)。前提が満たされないときは失敗ではなくskipする。
    """
    results = driver.measure("CH1", ["frequency"])
    if results[0].value is None:
        pytest.skip("CH1に測定可能な信号がありません(周波数が測定不能値)")

    outcome = control.clear_measurements(driver)

    assert outcome == {"result": "ok"}  # write_checkedがエラーキューを確認済み


# -- 11b. MATH演算(表示・解析層のみ。出力には触れない)----------------------


@pytest.fixture
def math_before(request: pytest.FixtureRequest, driver: ScopeDriver) -> Iterator[dict]:
    """MATH1 の現在設定を控え、teardownで必ず書き戻す。

    復元は「演算子を元へ戻してから各パラメータ」の順で行う(パラメータの
    有効・無効は演算子に依存するため)。`display` は最後に元へ戻す。
    """
    before = driver.get_math_config(MATH_CHANNEL)
    tag = request.node.name
    _report(f"[before:{tag}] math{MATH_CHANNEL}={before}")
    try:
        yield before
    finally:
        restore: dict = {
            key: before[key]
            for key in ("operator", "source1", "source2", "invert")
        }
        # 演算子依存で返らないキーは、返っていたときだけ復元対象にする
        for key in ("lsource1", "lsource2", "scale", "offset_v", "fft", "filter"):
            if key in before:
                restore[key] = before[key]
        restore["display"] = before["display"]

        failure: Exception | None = None
        try:
            driver.configure_math(MATH_CHANNEL, **restore)
        except Exception as exc:  # 復元は最後まで試みる
            failure = exc

        after = driver.get_math_config(MATH_CHANNEL)
        drift = _diff(before, after)
        _report(f"[restore:{tag}] math{MATH_CHANNEL} 復元後={after}")
        if drift:
            _report(f"[restore:{tag}] **復元漏れ**: {drift}")
        if failure is not None:
            _report(f"[restore:{tag}] **復元コマンド失敗**: {failure!r}")
        assert failure is None, f"MATH{MATH_CHANNEL} の復元に失敗: {failure!r}"
        assert not drift, f"MATH{MATH_CHANNEL} が復元されていません: {drift}"


def test_configure_math_round_trip(
    control: ControlService, driver: ScopeDriver, math_before: dict
) -> None:
    """算術演算 → FFT の順に設定し、read-backで確認する(復元はfixture)。

    FFT経路では実機実測の3点も併せて確認する:

    - ピーク表が複数行応答でも切り詰められないこと(改行区切り + 終端の空行)
    - 振幅の単位がSI接頭辞を外した形で返ること
    - `capture_waveform` の周波数軸メタデータが実測の関係式と合うこと
    """
    arithmetic = control.configure_math(
        driver,
        MATH_CHANNEL,
        display=True,
        operator="add",
        source1="CH1",
        source2="CH2",
    )
    applied = arithmetic["applied"]
    _report(f"[math] add applied={applied} changed={arithmetic['changed']}")

    assert applied["operator"] == "add"
    assert applied["source1"] == "CH1"
    assert applied["source2"] == "CH2"

    fft_result = control.configure_math(
        driver,
        MATH_CHANNEL,
        operator="fft",
        fft={
            "source": "CH1",
            "window": "hanning",
            "unit": "vrms",
            "search_enabled": True,
            "freq_start_hz": MATH_FFT_START_HZ,
            "freq_end_hz": MATH_FFT_END_HZ,
        },
    )
    _report(f"[math] fft applied={fft_result['applied']}")

    config = driver.get_math_config(MATH_CHANNEL)
    _report(f"[math] fft readback={config['fft']} peaks={config.get('peaks')}")

    assert config["operator"] == "fft"
    assert config["fft"]["window"] == "hanning"
    assert config["fft"]["unit"] == "vrms"
    assert config["fft"]["freq_start_hz"] == pytest.approx(
        MATH_FFT_START_HZ, rel=REL_TOLERANCE, abs=ABS_TOLERANCE
    )
    assert "peak_warnings" not in config
    for peak in config["peaks"]:
        assert peak["amplitude_unit"] == "Vrms"  # `mVrms` は換算済み

    capture = capture_waveform(driver, Config(), f"MATH{MATH_CHANNEL}")
    _report(
        f"[math] capture points={capture['points']} "
        f"step={capture['frequency_step_hz']:g} Hz "
        f"start={capture['frequency_start_hz']:g} Hz"
    )

    assert capture["x_unit"] == "Hz"
    assert "sample_interval_s" not in capture
    # 実測の関係式: 点数 × 周波数刻み = 表示終端周波数
    assert capture["points"] * capture["frequency_step_hz"] == pytest.approx(
        config["fft"]["freq_end_hz"], rel=1e-3
    )


# -- 11c. カーソル・計測器・ヒストグラム(表示・統計層のみ)-------------------


def _cursor_settings(state: dict) -> dict:
    """TRACkモードのY位置を除いた設定だけを取り出す(復元漏れ判定用)。

    実測(mho98-m2.md 3章)では、TRACkモードの `CAY` / `CBY` は**設定側の値も
    波形に追従して機器が動かす**。復元しても読むたびに変わるため、そのまま
    diff を取ると必ず「復元漏れ」に見えてしまう。
    """
    if state.get("mode") != "track":
        return state
    return {k: v for k, v in state.items() if k not in CURSOR_TRACKED_KEYS}


@pytest.fixture
def cursor_before(request: pytest.FixtureRequest, driver: ScopeDriver) -> Iterator[dict]:
    """カーソルの現在設定を控え、teardownで必ず書き戻す。

    `off` / `xy` は位置サブツリーを持たないため `mode` しか返らない。その場合は
    モードだけを戻す(存在しないサブツリーへは1コマンドも送らない)。
    """
    before = driver.get_cursor_config()
    tag = request.node.name
    _report(f"[before:{tag}] cursor={before}")
    try:
        yield before
    finally:
        # モードを先に戻す(位置・ソースの書き込み先がモードで決まるため)
        restore = {k: v for k, v in before.items() if k != "mode"}
        failure: Exception | None = None
        try:
            driver.configure_cursor(mode=before["mode"], **restore)
        except Exception as exc:  # 復元は最後まで試みる
            failure = exc

        after = driver.get_cursor_config()
        drift = _diff(_cursor_settings(before), _cursor_settings(after))
        _report(f"[restore:{tag}] cursor 復元後={after}")
        if drift:
            _report(f"[restore:{tag}] **復元漏れ**: {drift}")
        if failure is not None:
            _report(f"[restore:{tag}] **復元コマンド失敗**: {failure!r}")
        assert failure is None, f"カーソルの復元に失敗: {failure!r}"
        assert not drift, f"カーソルが復元されていません: {drift}"


def test_configure_cursor_round_trip(
    control: ControlService, driver: ScopeDriver, cursor_before: dict
) -> None:
    """manualカーソルを置いて read-back し、読み値のΔXが位置差と一致すること。"""
    result = control.configure_cursor(
        driver,
        mode="manual",
        type="time",
        source=METER_CHANNEL,
        ax=CURSOR_AX_S,
        ay=CURSOR_AY_V,
        bx=CURSOR_BX_S,
        by=CURSOR_BY_V,
    )
    applied = result["applied"]
    _report(f"[cursor] applied={applied} changed={result['changed']}")

    assert result["mode"] == "manual"
    assert applied["mode"] == "manual"
    assert applied["type"] == "time"
    assert applied["source"] == METER_CHANNEL
    for key, expected in (
        ("ax", CURSOR_AX_S),
        ("ay", CURSOR_AY_V),
        ("bx", CURSOR_BX_S),
        ("by", CURSOR_BY_V),
    ):
        assert applied[key] == pytest.approx(expected, rel=1e-3, abs=1e-9), key

    measurement = driver.get_cursor_measurement()
    _report(f"[cursor] measurement={measurement}")
    assert measurement["mode"] == "manual"
    # ΔX = B − A(読み値の有効数字は4桁程度なので相対誤差で見る)
    assert measurement["xdelta_s"] == pytest.approx(
        CURSOR_BX_S - CURSOR_AX_S, rel=1e-3
    )
    # 1/ΔX。ΔX が 0 でなければ数値が返る
    assert measurement["ixdelta_hz"] == pytest.approx(
        1.0 / (CURSOR_BX_S - CURSOR_AX_S), rel=1e-2
    )


@pytest.fixture
def counter_before(request: pytest.FixtureRequest, driver: ScopeDriver) -> Iterator[dict]:
    """周波数カウンタの現在設定を控え、teardownで必ず書き戻す。

    **復元の順序に実機都合がある。** `:COUNter:TOTalize:ENABle` は
    **カウンタが無効のとき、現在値と同じ値を書いても `-200,"Command execute
    failed"` で拒否される**(mho98-m2.md 5章。プローブ用スクリプトが実際に
    ここで失敗した)。そのため復元は次の2段階に分ける:

    1. `enabled` 以外を先に書く(カウンタがまだ有効なうちに書けば通る)
    2. 最後に `enabled` を元へ戻す

    それでも「復元開始時点でカウンタが無効」なら `totalize_enabled` は
    書けないので、そのときは**復元対象から外す**(その状態では機器が値を
    保持したまま書き込みを受け付けないため、そもそも変えられていない)。
    """
    before = driver.get_meter_config("counter")
    tag = request.node.name
    _report(f"[before:{tag}] counter={before}")
    try:
        yield before
    finally:
        restore = {
            key: value
            for key, value in before.items()
            if key not in ("kind", "enabled")
        }
        if not driver.get_meter_config("counter")["enabled"]:
            restore.pop("totalize_enabled", None)

        failure: Exception | None = None
        try:
            if restore:
                driver.configure_meter("counter", **restore)
            driver.configure_meter("counter", enabled=before["enabled"])
        except Exception as exc:  # 復元は最後まで試みる
            failure = exc

        after = driver.get_meter_config("counter")
        drift = _diff(before, after)
        _report(f"[restore:{tag}] counter 復元後={after}")
        if drift:
            _report(f"[restore:{tag}] **復元漏れ**: {drift}")
        if failure is not None:
            _report(f"[restore:{tag}] **復元コマンド失敗**: {failure!r}")
        assert failure is None, f"カウンタの復元に失敗: {failure!r}"
        assert not drift, f"カウンタが復元されていません: {drift}"


def test_configure_meter_counter_round_trip(
    control: ControlService, driver: ScopeDriver, counter_before: dict
) -> None:
    """周波数カウンタを有効化して read-back し、現在値を単位つきで読む。

    **モード依存の項目は同じ呼び出しで指定する**(`digits` は Totalize モード
    では機器が拒否する。tools.md 12章)。
    """
    result = control.configure_meter(
        driver,
        "counter",
        enabled=True,
        source=METER_CHANNEL,
        mode="frequency",
        digits=COUNTER_DIGITS,
    )
    applied = result["applied"]
    _report(f"[meter:counter] applied={applied} changed={result['changed']}")

    assert result["kind"] == "counter"
    assert applied["enabled"] is True
    assert applied["source"] == METER_CHANNEL
    assert applied["mode"] == "frequency"
    assert applied["digits"] == COUNTER_DIGITS

    value = get_meter_value(driver, "counter")
    _report(f"[meter:counter] value={value}")
    assert value["unit"] == "Hz"
    # 実測ではカウンタ有効でも 0 が返ることがある(mho98-m2.md 6.1、未解決)
    assert value["value"] is None or isinstance(value["value"], float)


@pytest.fixture
def dvm_before(request: pytest.FixtureRequest, driver: ScopeDriver) -> Iterator[dict]:
    """電圧計の現在設定を控え、teardownで必ず書き戻す。

    電圧計にはカウンタのような無効時の書き込み拒否は無いが、順序は揃えて
    `enabled` を最後に戻す(無効にしてから他の項目を書かない)。
    """
    before = driver.get_meter_config("dvm")
    tag = request.node.name
    _report(f"[before:{tag}] dvm={before}")
    try:
        yield before
    finally:
        restore = {
            key: value
            for key, value in before.items()
            if key not in ("kind", "enabled")
        }
        failure: Exception | None = None
        try:
            driver.configure_meter("dvm", **restore)
            driver.configure_meter("dvm", enabled=before["enabled"])
        except Exception as exc:  # 復元は最後まで試みる
            failure = exc

        after = driver.get_meter_config("dvm")
        drift = _diff(before, after)
        _report(f"[restore:{tag}] dvm 復元後={after}")
        if drift:
            _report(f"[restore:{tag}] **復元漏れ**: {drift}")
        if failure is not None:
            _report(f"[restore:{tag}] **復元コマンド失敗**: {failure!r}")
        assert failure is None, f"電圧計の復元に失敗: {failure!r}"
        assert not drift, f"電圧計が復元されていません: {drift}"


def test_configure_meter_dvm_round_trip(
    control: ControlService, driver: ScopeDriver, dvm_before: dict
) -> None:
    """電圧計を有効化して read-back し、現在値がVで読めること。

    無効時の `:DVM:CURRent?` は空応答(mho98-m2.md 1章)。**有効化した後は
    数値が返る**ことがこのテストの主眼。
    """
    result = control.configure_meter(
        driver,
        "dvm",
        enabled=True,
        source=METER_CHANNEL,
        mode="dc",
    )
    applied = result["applied"]
    _report(f"[meter:dvm] applied={applied} changed={result['changed']}")

    assert result["kind"] == "dvm"
    assert applied["enabled"] is True
    assert applied["source"] == METER_CHANNEL
    assert applied["mode"] == "dc"

    value = get_meter_value(driver, "dvm")
    _report(f"[meter:dvm] value={value}")
    assert value["unit"] == "V"
    assert isinstance(value["value"], float), "有効な電圧計は数値を返す"


@pytest.fixture
def histogram_before(
    request: pytest.FixtureRequest, driver: ScopeDriver
) -> Iterator[dict]:
    """ヒストグラムの現在設定を控え、teardownで必ず書き戻す。

    範囲は**両端を同時に**書き戻す(片側だけを動かして現在の反対側を追い越すと
    機器が拒否する。tools.md 12章)。`configure_histogram` は項目表の順に
    LEFT → RIGHt → BOTTom → TOP を1回の呼び出しで送るため、1往復で足りる。
    """
    before = driver.get_histogram_config()
    tag = request.node.name
    _report(f"[before:{tag}] histogram={before}")
    try:
        yield before
    finally:
        failure: Exception | None = None
        try:
            driver.configure_histogram(**before)
        except Exception as exc:  # 復元は最後まで試みる
            failure = exc

        after = driver.get_histogram_config()
        drift = _diff(before, after)
        _report(f"[restore:{tag}] histogram 復元後={after}")
        if drift:
            _report(f"[restore:{tag}] **復元漏れ**: {drift}")
        if failure is not None:
            _report(f"[restore:{tag}] **復元コマンド失敗**: {failure!r}")
        assert failure is None, f"ヒストグラムの復元に失敗: {failure!r}"
        assert not drift, f"ヒストグラムが復元されていません: {drift}"


def test_configure_histogram_round_trip(
    control: ControlService, driver: ScopeDriver, histogram_before: dict
) -> None:
    """ヒストグラムを有効化して read-back し、統計を読む。

    統計応答は**単一行・終端の空行なし**で、機器自身がラベルを持つ
    (mho98-m2.md 2章)。`raw` は常に返り、`stats` はSI換算済みの数値。
    """
    result = control.configure_histogram(
        driver,
        enabled=True,
        type="vertical",
        source=METER_CHANNEL,
        height=HISTOGRAM_HEIGHT,
        left_s=HISTOGRAM_LEFT_S,
        right_s=HISTOGRAM_RIGHT_S,
        bottom_v=HISTOGRAM_BOTTOM_V,
        top_v=HISTOGRAM_TOP_V,
        reset=True,
    )
    applied = result["applied"]
    _report(f"[histogram] applied={applied} changed={result['changed']}")

    assert applied["enabled"] is True
    assert applied["type"] == "vertical"
    assert applied["source"] == METER_CHANNEL
    assert applied["height"] == HISTOGRAM_HEIGHT
    assert applied["reset"] is True
    for key, expected in (
        ("left_s", HISTOGRAM_LEFT_S),
        ("right_s", HISTOGRAM_RIGHT_S),
        ("bottom_v", HISTOGRAM_BOTTOM_V),
        ("top_v", HISTOGRAM_TOP_V),
    ):
        assert applied[key] == pytest.approx(expected, rel=1e-3, abs=1e-9), key

    outcome = driver.get_histogram_result()
    _report(f"[histogram] result={outcome}")
    assert outcome["raw"].startswith("[")
    assert "warnings" not in outcome, "統計の解釈に失敗した項目がある"
    stats = outcome["stats"]
    # SI接頭辞は換算済み(`30.37khits` → 30370.0 / `sum_unit="hits"`)。
    # reset直後はヒット数が少なく接頭辞が付かないこともあるため、単位は
    # 「付いていれば hits」で見る
    assert isinstance(stats["sum"], float)
    assert stats.get("sum_unit", "hits") == "hits"


# -- 11d. リファレンス波形(表示・解析層のみ。保存は既定で実行しない)--------


@pytest.fixture
def reference_before(
    request: pytest.FixtureRequest, driver: ScopeDriver
) -> Iterator[dict]:
    """対象枠の現在設定を控え、teardownで必ず書き戻す。

    `label_display` は**全枠共通のスイッチ**なので、書き戻しも全枠に効く
    (枠ごとの値ではないため、他の枠を巻き込む心配はない)。保存済みの波形
    そのものは触らない(このfixtureが復元できるのは設定だけ)。

    なお `source` の書き戻しは、元のソースが**現在無効なチャンネル**だと機器に
    拒否されうる(ガイド3.20.2 のRemarks)。その場合は復元失敗として大きな声で
    落ちる — 結合制約の実機挙動はまさにここで確かめたい未検証事項。
    """
    before = driver.get_reference_config(REF_SLOT)
    tag = request.node.name
    _report(f"[before:{tag}] reference={before}")
    try:
        yield before
    finally:
        restore = {k: v for k, v in before.items() if k != "ref"}
        failure: Exception | None = None
        try:
            driver.configure_reference(REF_SLOT, **restore)
        except Exception as exc:  # 復元は最後まで試みる
            failure = exc

        after = driver.get_reference_config(REF_SLOT)
        drift = _diff(before, after)
        _report(f"[restore:{tag}] reference 復元後={after}")
        if drift:
            _report(f"[restore:{tag}] **復元漏れ**: {drift}")
        if failure is not None:
            _report(f"[restore:{tag}] **復元コマンド失敗**: {failure!r}")
        assert failure is None, f"リファレンスの復元に失敗: {failure!r}"
        assert not drift, f"リファレンスが復元されていません: {drift}"


def test_configure_reference_round_trip(
    control: ControlService, driver: ScopeDriver, reference_before: dict
) -> None:
    """復元可能な設定だけを書いて read-back する(**保存は行わない**)。

    このサブシステムだけは枠番号を**コマンド引数**で渡す
    (`:REFerence:VSCale 10,0.5` / `:REFerence:VSCale? 10`)。実機がこの形を
    受理することの確認が主眼。

    ガイド3.20.1のRemarksは「有効なチャンネルしか選べない」と書くが、**実機では
    成立しない**(CH4を表示OFFにしても `:REFerence:SOURce 1,CHANnel4` は
    エラーなく通り `CHAN4` を返す。mho98-m3.md)。よってチャンネルの表示状態は
    ここでは操作しない。
    """
    result = control.configure_reference(
        driver,
        REF_SLOT,
        source=REF_SOURCE,
        scale=REF_SCALE_V_PER_DIV,
        offset_v=REF_OFFSET_V,
        color=REF_COLOR,
        label=REF_LABEL,
    )
    applied = result["applied"]
    _report(f"[reference] applied={applied} changed={result['changed']}")

    assert result["ref"] == REF_SLOT
    assert applied["source"] == REF_SOURCE
    assert applied["color"] == REF_COLOR
    assert applied["label"] == REF_LABEL
    for key, expected in (
        ("scale", REF_SCALE_V_PER_DIV),
        ("offset_v", REF_OFFSET_V),
    ):
        assert applied[key] == pytest.approx(expected, rel=1e-3, abs=1e-9), key


def test_configure_reference_reset_runs_before_the_settings(
    control: ControlService, driver: ScopeDriver, reference_before: dict
) -> None:
    """`reset=True` と垂直設定を同時に指定したとき、設定側が残ること。

    `:REFerence:RESet` は垂直スケール/位置を既定へ戻すため、同じ呼び出しの
    scale / offset_v より**前**に送っている。順序が逆なら設定が捨てられる。
    保存済みの波形は消えない(消えるのは垂直の見え方だけ)。
    """
    result = control.configure_reference(
        driver,
        REF_SLOT,
        reset=True,
        scale=REF_SCALE_V_PER_DIV,
        offset_v=REF_OFFSET_V,
    )
    applied = result["applied"]
    _report(f"[reference] reset+設定 applied={applied}")

    assert applied["reset"] is True
    assert applied["scale"] == pytest.approx(REF_SCALE_V_PER_DIV, rel=1e-3, abs=1e-9)
    assert applied["offset_v"] == pytest.approx(REF_OFFSET_V, rel=1e-3, abs=1e-9)


@requires_ref_save
def test_save_reference_overwrites_the_slot(
    control: ControlService, driver: ScopeDriver, reference_before: dict
) -> None:
    """**枠の中身を上書きする不可逆テスト**。三重ゲートの下でのみ走る。

    `RIGOL_TEST_ADDRESS` + `RIGOL_TEST_ALLOW_WRITE=1` に加えて
    `RIGOL_TEST_ALLOW_REF_SAVE=1` が要る。**この枠に入っていた波形は元に戻せ
    ない**(機器に undo も「保存済みか」の照会も無い)。fixtureが復元するのは
    設定だけで、波形の中身は復元できないことを承知の上で実行すること。
    """
    # ソースに有効/無効の前提は無い(mho98-m3.md。ガイドのRemarksは実機で不成立)
    result = control.configure_reference(
        driver, REF_SLOT, source=REF_SOURCE, save=True
    )
    _report(f"[reference] save slot={REF_SLOT} applied={result['applied']}")

    assert result["applied"]["save"] is True
    # 保存自体は read-back できない(機器にクエリが無い)。エラーキューが唯一の判定
    assert driver.session.drain_error_queue() == []


# -- 12. 監査ログ(最後に実行し、それまでの全操作を検証)--------------------


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

    # 設定変更の記録は Before / Action / After が揃っていること。
    # 状態を持たない動作コマンド(位相同期・測定項目クリア)は before を持たない
    # ── 取る対象の状態が無く、service側も record.before を呼ばない(設計どおり)
    ACTION_ONLY_TOOLS = frozenset({"sync_afg_phase", "clear_measurements"})
    for entry in entries:
        if entry.get("result") != "success":
            continue
        assert entry["after"] is not None, entry.get("tool")
        if entry.get("tool") not in ACTION_ONLY_TOOLS:
            assert entry["before"] is not None, entry.get("tool")

    # 無条件に走るTool(このスイートに必ず含まれる)
    assert {
        "configure_channel",
        "configure_timebase",
        "configure_trigger",
        "configure_decode",
        "configure_math",
        "configure_reference",
    } <= set(tools)
    assert {"run", "stop", "single"} <= set(tools)

    # ゲート付き・機器状態依存でskipされうるToolは「走ったなら記録がある」だけを見る。
    # 例: AFG設定テストは出力ONの機器では自ら安全にskipするため(afg_before)、
    # configure_afg を必須にすると機器の状態でテスト結果が変わってしまう
    CONDITIONAL_TOOLS = ("configure_afg", "configure_meter", "configure_histogram",
                         "configure_cursor", "sync_afg_phase", "clear_measurements")
    for tool in CONDITIONAL_TOOLS:
        if tool in tools:
            assert tools[tool] >= 1


# --------------------------------------------------------------------------
# 測定の前提設定(Phase M4)
# --------------------------------------------------------------------------


@pytest.fixture
def measurement_before(
    request: pytest.FixtureRequest, driver: ScopeDriver
) -> Iterator[dict]:
    """測定の前提設定を控え、teardownで必ず書き戻す。

    しきい値は上限 > 中央 > 下限 の順序制約があり、片側だけを動かすと機器が
    拒否しうる。`configure_measurement` は項目表の順に MAX → MID → MIN を
    1回の呼び出しで送るため1往復で足りる。
    """
    before = driver.get_measurement_config()
    tag = request.node.name
    _report(f"[before:{tag}] measurement={before}")
    try:
        yield before
    finally:
        failure: Exception | None = None
        try:
            driver.configure_measurement(**before)
        except Exception as exc:  # 復元は最後まで試みる
            failure = exc

        after = driver.get_measurement_config()
        drift = _diff(before, after)
        _report(f"[restore:{tag}] measurement 復元後={after}")
        if drift:
            _report(f"[restore:{tag}] **復元漏れ**: {drift}")
        if failure is not None:
            _report(f"[restore:{tag}] **復元コマンド失敗**: {failure!r}")
        assert failure is None, f"測定の前提設定の復元に失敗: {failure!r}"
        assert not drift, f"測定の前提設定が復元されていません: {drift}"


def test_configure_measurement_set_and_readback(
    control: ControlService, driver: ScopeDriver, measurement_before: dict
) -> None:
    """しきい値と振幅算出方式の往復。**area は触らない**。

    `area="zoom"` は遅延掃引の有効化が前提で機器が拒否しうる(ガイド3.17.19)。
    `area="cursor"` は画面にカーソルを出すため、復元漏れの影響が見えやすい。
    どちらも本テストの目的(往復の確認)には不要なので `main` のまま動かさない。
    """
    result = control.configure_measurement(
        driver,
        threshold_type="percent",
        threshold_max=88.0,
        threshold_mid=48.0,
        threshold_min=12.0,
        amp_type="manual",
        amp_top="maxmin",
    )
    _report(f"[measurement] applied={result['applied']}")

    applied = result["applied"]
    assert applied["threshold_type"] == "percent"
    assert applied["amp_top"] == "maxmin"
    # しきい値は機器がスナップしうるので requested との一致は求めない
    assert isinstance(applied["threshold_max"], float)


def test_measurement_statistics_round_trip(
    control: ControlService, driver: ScopeDriver, measurement_before: dict
) -> None:
    """統計の有効化 → 読み出し。**V-5(応答形式)の実測を兼ねる**。

    ガイド3.17.8 の Return Format は「科学表記の統計結果」で、Example は単一値
    (`9.120000E-1`)。実機がその通りかをここで確かめる。
    """
    control.configure_measurement(
        driver,
        source="CH2",
        statistics_enabled=True,
        statistics_items=["vpp", "frequency"],
        statistics_reset=True,
    )
    stats = driver.get_measurement_statistics("CH2", ["vpp", "frequency"])

    for name, values in stats.items():
        _report(f"[statistics] {name}: {values}")
        assert set(values) == {
            "maximum",
            "minimum",
            "current",
            "average",
            "deviation",
            "count",
        }
        for kind, value in values.items():
            assert value is None or isinstance(value, float), (name, kind, value)


def test_configure_trigger_switches_type_and_restores(
    control: ControlService, driver: ScopeDriver, trigger_before: dict
) -> None:
    """種別の切り替えと配下の設定の往復(Phase M5)。

    パルス幅トリガを選ぶ。**取り込みを止めることも出力に触れることもない**。
    復元は fixture が元の種別へ戻す。
    """
    result = control.configure_trigger(
        driver,
        type="pulse",
        settings={"when": "less", "upper_width_s": 1e-6},
    )
    _report(f"[trigger] applied={result['applied']}")

    assert result["applied"]["type"] == "pulse"
    assert result["applied"]["when"] == "less"
    assert result["trigger"]["type"] == "pulse"
    # 他の種別のサブツリーは読んでいない(edge の項目が settings に無い)
    assert "polarity" in result["trigger"]["settings"]


def test_get_trigger_position_is_read_only(driver: ScopeDriver) -> None:
    """`:TRIGger:POSition?`(ガイド3.27.7)。読み取りのみで副作用が無い。"""
    position = driver.get_trigger_position()
    _report(f"[trigger] position={position!r}")

    assert position is None or isinstance(position, float)


# 種別ごとの代表設定。**全16種を実機へ送る**(3種だけの確認では
# pattern / duration / lin の応答形式の違いを取り逃がした)。
TRIGGER_PROBES: dict[str, dict] = {
    "edge": {"slope": "falling"},
    "pulse": {"when": "less", "upper_width_s": 1e-6},
    "slope": {"when": "greater", "lower_time_s": 2e-6, "window": "ab"},
    "pattern": {"pattern": ["high", "low", "ignore", "ignore"]},
    "duration": {"pattern": ["low", "ignore", "high", "low"], "when": "outside"},
    "timeout": {"time_s": 5e-6},
    "runt": {"polarity": "negative", "when": "greater"},
    "window": {"position": "enter", "time_s": 4e-6, "slope": "either"},
    "delay": {"when": "between", "lower_time_s": 1e-6},
    "setup_hold": {"when": "both", "setup_time_s": 3e-6, "hold_time_s": 2e-6},
    "nth_edge": {"edge_number": 5, "idle_time_s": 2e-6},
    "uart": {"when": "data", "baud_bps": 115200, "data_bits": 7, "stop_bits": 2},
    "i2c": {"when": "address", "address_bits": 10, "address": 300},
    "spi": {"when": "timeout", "timeout_s": 2e-5, "data_bits": 12},
    "can": {"when": "bit_error", "baud_bps": 250000, "sample_point_percent": 60},
    "lin": {"when": "data", "standard": "v1x", "frame_id": 33, "data": 100},
}


@pytest.mark.parametrize("trigger_type", sorted(TRIGGER_PROBES))
def test_configure_trigger_every_type_round_trips(
    control: ControlService,
    driver: ScopeDriver,
    trigger_before: dict,
    trigger_type: str,
) -> None:
    """**全16種**の往復。取り込みを止めることも出力に触れることもない。

    復元は fixture が元の種別・設定へ戻す。**種別を切り替えるとトリガレベルが
    サブツリー間で伝播する**ため、復元は種別を先に戻してから設定を書く
    (`configure_trigger` が `type` を先頭に送る)。
    """
    settings = TRIGGER_PROBES[trigger_type]
    result = control.configure_trigger(driver, type=trigger_type, settings=settings)
    _report(f"[trigger:{trigger_type}] applied={result['applied']}")

    assert result["applied"]["type"] == trigger_type
    assert result["trigger"]["type"] == trigger_type
    # 読み戻しはその種別のサブツリーだけ(他種別の項目が混ざらない)
    allowed = {key for key, _, _ in _TRIGGER_SUBTREES[trigger_type][1]}
    assert set(result["trigger"]["settings"]) <= allowed
    _report(f"[trigger:{trigger_type}] settings={result['trigger']['settings']}")


def test_configure_trigger_serial_type_and_restore(
    control: ControlService, driver: ScopeDriver, trigger_before: dict
) -> None:
    """シリアルバストリガ(I2C)の往復(Phase M5)。

    デコード(`configure_decode`)とは**別サブシステム**なので、綴りが違う点を
    実機で確かめる — トリガ側のアドレス幅は `:TRIGger:IIC:AWIDth`(デコード側は
    `:BUS<n>:IIC:ADDBits`)。取り込みを止めることも出力に触れることもない。
    """
    result = control.configure_trigger(
        driver,
        type="i2c",
        settings={"when": "nack", "address_bits": 7, "address": 42},
    )
    _report(f"[trigger:i2c] applied={result['applied']}")

    assert result["applied"]["type"] == "i2c"
    assert result["applied"]["when"] == "nack"
    assert result["trigger"]["settings"]["address"] == 42
