"""設定変更・Acquisition操作(tools.md 3章・4章)。

Safety Layer(confirmトークン・監査ログ)とドライバの結合点。本層の責務は3つ:

- **未指定は変更しない**: `None` の項目にはコマンドを1件も送らない(送信コストと
  「知らないうちに変わっていた」事故の双方を避ける)
- **requested / applied 両値返却**(Requirements.md 7.3): 機器がスナップしても
  要求値は書き換えず、read-backで得た適用値を併せて返す
- **承認と記録**(Requirements.md 6.2 / 7.6): 昇格操作は confirmトークン無しで
  機器へ**1コマンドも送らない**。書き込みは Before / Action / After を監査する

操作クラスの静的分類は safety.classes、引数依存の昇格(impedance="50")は本層の
責務(safety/classes.py の注記どおり)。
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import asdict

from ..driver.scope import ScopeDriver
from ..errors import ErrorCode, ScopeError
from ..safety import AuditLogger, AuditScope, ConfirmTokenStore, token_digest
from .state import get_channel_dict, get_state, get_timebase_dict, get_trigger_dict

#: RESTRICTED_WRITE へ昇格させる入力インピーダンス(tools.md 3章)
RESTRICTED_IMPEDANCE = "50"

#: autoset 実行後、返却に必ず添える注記(tools.md 4章)
AUTOSET_NOTE = "Auto Setup was executed. The settings have been changed substantially."

#: autoset 実行後に返す状態のセクション
AUTOSET_SECTIONS = ["channels", "timebase", "trigger"]

_50OHM_RISK = (
    "The 50 ohm input has a low voltage rating; an excessive input "
    "(high voltage or large amplitude) will damage the device. "
    "Confirm the signal source output level and what is connected "
    "with the human user."
)

_AUTOSET_DESCRIPTION = "Run Auto Setup (autoscale)"
_AUTOSET_RISK = (
    "The current settings will be changed substantially. "
    "Vertical scale, timebase and trigger are auto-adjusted, and the previous "
    "settings are lost."
)

_AFG_OUTPUT_DESCRIPTION = (
    "Turn on AFG channel {n} output (inject a real signal into whatever is "
    "connected)"
)
_AFG_OUTPUT_RISK = (
    "Turning the generator output on drives a real signal into whatever is "
    "physically connected to the AFG output right now. Ask the human user to "
    "confirm the physical setup first: what is connected to the generator "
    "output, and is it safe to drive? Do not enable the output into a live or "
    "powered circuit unless the user has explicitly confirmed it is safe. The "
    "configured waveform, frequency, amplitude and offset are applied the "
    "moment the output turns on — read them back with get_afg_state and show "
    "them to the user before asking for confirmation."
)


def _invalid(message: str, detail: dict) -> ScopeError:
    return ScopeError(ErrorCode.INVALID_PARAMETER, message, detail)


def _specified(values: dict) -> dict:
    """`None` でない項目だけを、宣言順のまま取り出す。"""
    return {key: value for key, value in values.items() if value is not None}


#: `get_math_config` の返却のうち、設定ではなく測定結果として毎回変わりうるキー。
#: `changed` の判定から除く(監査ログには完全なスナップショットを残す)。
_MATH_DYNAMIC_KEYS = ("peaks", "peak_warnings")


def _math_settings(state: dict) -> dict:
    """MATH状態から設定項目だけを取り出す(ピーク表などの動的値を除く)。"""
    return {key: value for key, value in state.items() if key not in _MATH_DYNAMIC_KEYS}


#: TRACkモードのカーソルは**Y位置が波形に追従して機器側で動く**。設定として
#: 指定していなくても読むたびに変わるため、`changed` の判定から除く
#: (MANualのY位置は利用者が決める設定値なので除かない)。
_CURSOR_TRACKED_KEYS = ("ay", "by")


def _cursor_settings(state: dict) -> dict:
    """カーソル状態から設定項目だけを取り出す(TRACkの追従Y位置を除く)。"""
    if state.get("mode") != "track":
        return state
    return {
        key: value for key, value in state.items() if key not in _CURSOR_TRACKED_KEYS
    }


def _is_restricted_impedance(impedance: str | None) -> bool:
    """`"50"` のみ昇格対象。表記ゆれ(前後空白)は吸収する。"""
    return isinstance(impedance, str) and impedance.strip() == RESTRICTED_IMPEDANCE


class ControlService:
    """設定変更とAcquisition操作を、承認・監査つきで実行する。

    ドライバは呼び出しごとに受け取る(接続の生存管理は ConnectionManager の責務)。
    `generation` は接続世代で、トークン発行後に接続が切り替わった場合に
    確認を無効化するために使う(Requirements.md 6.2)。
    """

    def __init__(self, confirm_store: ConfirmTokenStore, audit: AuditLogger) -> None:
        self._confirm = confirm_store
        self._audit = audit

    # -- 設定変更 ---------------------------------------------------------

    def configure_channel(
        self,
        driver: ScopeDriver,
        generation: int,
        channel: str,
        *,
        enabled: bool | None = None,
        scale_v_per_div: float | None = None,
        offset_v: float | None = None,
        coupling: str | None = None,
        probe_ratio: float | None = None,
        bandwidth_limit: bool | None = None,
        impedance: str | None = None,
        confirm_token: str | None = None,
    ) -> dict:
        """垂直軸を設定する。未指定(None)の項目は変更しない。

        `impedance="50"` のみ RESTRICTED_WRITE へ昇格し、confirmトークンを要求する。
        """
        requested = _specified(
            {
                "enabled": enabled,
                "scale_v_per_div": scale_v_per_div,
                "offset_v": offset_v,
                "coupling": coupling,
                "probe_ratio": probe_ratio,
                "bandwidth_limit": bandwidth_limit,
                "impedance": impedance,
            }
        )
        if not requested:
            raise _invalid(
                "No item to change was specified "
                "(specify at least one of enabled / scale_v_per_div / offset_v / "
                "coupling / probe_ratio / bandwidth_limit / impedance)",
                {"channel": channel},
            )

        args = {"channel": channel, **requested}
        if _is_restricted_impedance(impedance):
            self._require_confirmation(
                "configure_channel",
                args,
                generation,
                confirm_token,
                description=f"Change the input impedance of {channel} to 50 ohm",
                risk=_50OHM_RISK,
            )

        # 適用順は setter の宣言順(requested の順)。1項目でも失敗したら中断する。
        setters: dict[str, Callable[[object], object]] = {
            "enabled": lambda v: driver.set_channel_enabled(channel, bool(v)),
            "scale_v_per_div": lambda v: driver.set_channel_scale(channel, float(v)),
            "offset_v": lambda v: driver.set_channel_offset(channel, float(v)),
            "coupling": lambda v: driver.set_channel_coupling(channel, str(v)),
            "probe_ratio": lambda v: driver.set_channel_probe_ratio(channel, float(v)),
            "bandwidth_limit": lambda v: driver.set_channel_bwlimit(channel, bool(v)),
            "impedance": lambda v: driver.set_channel_impedance(channel, str(v)),
        }

        with self._audited("configure_channel", args) as record:
            before = get_channel_dict(driver, channel)
            record.before(before)
            applied = {key: setters[key](value) for key, value in requested.items()}
            after = get_channel_dict(driver, channel)
            record.after(after)

        return {
            "channel": after["channel"],
            "requested": requested,
            "applied": applied,
            "changed": before != after,
        }

    def configure_timebase(
        self,
        driver: ScopeDriver,
        *,
        scale_s_per_div: float | None = None,
        position_s: float | None = None,
    ) -> dict:
        """水平軸を設定する(SAFE_WRITE)。未指定の項目は変更しない。"""
        requested = _specified(
            {"scale_s_per_div": scale_s_per_div, "position_s": position_s}
        )
        if not requested:
            raise _invalid(
                "No item to change was specified "
                "(specify at least one of scale_s_per_div / position_s)",
                {},
            )

        setters: dict[str, Callable[[object], object]] = {
            "scale_s_per_div": lambda v: driver.set_timebase_scale(float(v)),
            "position_s": lambda v: driver.set_timebase_position(float(v)),
        }

        with self._audited("configure_timebase", requested) as record:
            before = get_timebase_dict(driver)
            record.before(before)
            applied = {key: setters[key](value) for key, value in requested.items()}
            after = get_timebase_dict(driver)
            record.after(after)

        return {"requested": requested, "applied": applied, "changed": before != after}

    def configure_trigger(
        self,
        driver: ScopeDriver,
        *,
        source: str | None = None,
        level_v: float | None = None,
        slope: str | None = None,
        sweep_mode: str | None = None,
    ) -> dict:
        """エッジトリガを設定する(SAFE_WRITE)。未指定の項目は変更しない。

        ドライバが一括で設定・read-backするため、`applied` はその結果から抽出する
        (項目ごとに read-back を送らない)。
        """
        requested = _specified(
            {
                "source": source,
                "level_v": level_v,
                "slope": slope,
                "sweep_mode": sweep_mode,
            }
        )
        if not requested:
            raise _invalid(
                "No item to change was specified "
                "(specify at least one of source / level_v / slope / sweep_mode)",
                {},
            )

        with self._audited("configure_trigger", requested) as record:
            before = get_trigger_dict(driver)
            record.before(before)
            after = asdict(driver.set_trigger_edge(**requested))
            record.after(after)

        return {
            "requested": requested,
            "applied": {key: after[key] for key in requested},
            "changed": before != after,
            "trigger": after,
        }

    def configure_decode(
        self,
        driver: ScopeDriver,
        bus: int,
        protocol: str,
        *,
        enabled: bool | None = None,
        event_table: bool | None = None,
        data_format: str | None = None,
        settings: dict | None = None,
    ) -> dict:
        """シリアルデコードを設定する(SAFE_WRITE)。未指定の項目は変更しない。

        表示・解析層のみを変える完全に可逆な操作なので承認は要求しない。
        引数依存の昇格も無い(デコード設定は取り込み設定も出力も変えない)。
        """
        requested = _specified(
            {
                "enabled": enabled,
                "event_table": event_table,
                "data_format": data_format,
                "settings": settings,
            }
        )
        args = {"bus": bus, "protocol": protocol, **requested}

        with self._audited("configure_decode", args) as record:
            # ビット別ソースまで見ないと `changed` が取りこぼす(走査は書き込みを伴う)
            before = driver.get_decode_config(bus, include_bit_sources=True)
            record.before(before)
            applied = driver.configure_decode(
                bus,
                protocol,
                enabled=enabled,
                event_table=event_table,
                data_format=data_format,
                settings=settings,
            )
            after = driver.get_decode_config(bus, include_bit_sources=True)
            record.after(after)

        return {
            "bus": after["bus"],
            "requested": requested,
            "applied": applied,
            "changed": before != after,
        }

    def configure_afg(
        self,
        driver: ScopeDriver,
        channel: int = 1,
        *,
        waveform: str | None = None,
        frequency_hz: float | None = None,
        amplitude_vpp: float | None = None,
        offset_v: float | None = None,
        phase_deg: float | None = None,
        duty_percent: float | None = None,
        symmetry_percent: float | None = None,
        impedance: str | None = None,
        arb_file: str | None = None,
        modulation: dict | None = None,
    ) -> dict:
        """信号発生器を設定する(SAFE_WRITE)。未指定の項目は変更しない。

        **出力状態には一切触れない**ため、この操作だけで信号が外へ出ることはない
        (出力のON/OFFは承認フロー付きの別Toolの責務)。したがって引数依存の昇格も
        承認要求も無い。`arb_file` は機器内蔵ストレージの既存ARBファイルを選択する
        だけで、ファイルの作成・転送・削除は行わない(docs/Requirements.md 3.4)。
        """
        requested = _specified(
            {
                "waveform": waveform,
                "frequency_hz": frequency_hz,
                "amplitude_vpp": amplitude_vpp,
                "offset_v": offset_v,
                "phase_deg": phase_deg,
                "duty_percent": duty_percent,
                "symmetry_percent": symmetry_percent,
                "impedance": impedance,
                "arb_file": arb_file,
                "modulation": modulation,
            }
        )
        if not requested:
            raise _invalid(
                "No item to change was specified "
                "(specify at least one of waveform / frequency_hz / amplitude_vpp / "
                "offset_v / phase_deg / duty_percent / symmetry_percent / "
                "impedance / arb_file / modulation)",
                {"channel": channel},
            )

        args = {"channel": channel, **requested}
        with self._audited("configure_afg", args) as record:
            before = driver.get_afg_config(channel)
            record.before(before)
            applied = driver.configure_afg(channel, **requested)
            after = driver.get_afg_config(channel)
            record.after(after)

        return {
            "channel": after["channel"],
            "requested": requested,
            "applied": applied,
            "changed": before != after,
        }

    def enable_afg(
        self,
        driver: ScopeDriver,
        generation: int,
        channel: int = 1,
        confirm_token: str | None = None,
    ) -> dict:
        """信号発生の出力をONにする(DANGEROUS_WRITE)。

        **本Toolだけが実際に信号を外へ出す。** 接続先の被測定回路に何が繋がって
        いるかはサーバー側からは分からないため、承認は必須(トークンはチャンネル
        単位・単回・接続世代つき)。トークン無しでは機器へ**書き込みを1つも
        送らない**(現在設定の読み取りのみ行う)。

        トークンは発行時点のAFG設定スナップショットにも束縛する: 発行と消費の
        間に設定(振幅等)を変更すると引数ダイジェストが一致せず、トークンは
        無効になる(承認後の振幅吊り上げの防止)。
        """
        settings = driver.get_afg_config(channel)
        self._require_confirmation(
            "enable_afg",
            {"channel": channel, "settings": settings},
            generation,
            confirm_token,
            description=_AFG_OUTPUT_DESCRIPTION.format(n=channel),
            risk=_AFG_OUTPUT_RISK,
        )
        return self._set_afg_output(driver, "enable_afg", channel, True)

    def disable_afg(self, driver: ScopeDriver, channel: int = 1) -> dict:
        """信号発生の出力をOFFにする(SAFE_WRITE)。

        **承認は要求しない。** 出力停止は常に安全側への操作であり、緊急停止を
        確認フローでブロックしてはならない(tools.md 7章)。
        """
        return self._set_afg_output(driver, "disable_afg", channel, False)

    def _set_afg_output(
        self, driver: ScopeDriver, tool: str, channel: int, enabled: bool
    ) -> dict:
        """出力の切り替えを監査つきで実行し、切替後の全設定を返す。

        返却に設定一式を添えるのは、出力ONで実際に何が出ているか(波形・周波数・
        振幅・オフセット)を呼び出し側がそのまま利用者へ示せるようにするため。
        """
        with self._audited(tool, {"channel": channel}) as record:
            record.before(driver.get_afg_config(channel))
            driver.set_afg_output(channel, enabled)
            after = driver.get_afg_config(channel)
            record.after(after)

        return {"result": "ok", "channel": after["channel"], "state": after}

    def sync_afg_phase(self, driver: ScopeDriver, channel: int = 1) -> dict:
        """信号発生の両チャンネルの位相を同期する(SAFE_WRITE)。

        振幅・出力状態には一切触れない整列操作(プリセットの周波数・位相を
        再適用するだけ)なので承認は要求しない。**両チャンネルとも影響を受ける**
        操作のため監査の requested には `channel`(送信先の選択にすぎない)を
        含めない。read-back対象が存在しないwrite-onlyコマンドのため、
        run/stop・clear_measurementsと同型で `{"result": "ok"}` のみ返す。
        """
        with self._audited("sync_afg_phase", {}) as record:
            driver.sync_afg_phase(channel)
            record.after({})
        return {"result": "ok"}

    def configure_math(
        self,
        driver: ScopeDriver,
        channel: int = 1,
        *,
        display: bool | None = None,
        operator: str | None = None,
        source1: str | None = None,
        source2: str | None = None,
        lsource1: str | None = None,
        lsource2: str | None = None,
        scale: float | None = None,
        offset_v: float | None = None,
        invert: bool | None = None,
        fft: dict | None = None,
        # `filter` は組込み名と重なるが、Tool引数名(ガイドの :FILTer)を優先する
        filter: dict | None = None,
    ) -> dict:
        """MATH演算を設定する(SAFE_WRITE)。未指定の項目は変更しない。

        configure_decode と同じ根拠で承認は要求しない: 表示・解析層のみを変える
        完全に可逆な操作で、取り込み設定にも出力にも触れない。引数依存の昇格も無い。
        """
        items = {
            "display": display,
            "operator": operator,
            "source1": source1,
            "source2": source2,
            "lsource1": lsource1,
            "lsource2": lsource2,
            "scale": scale,
            "offset_v": offset_v,
            "invert": invert,
            "fft": fft,
            "filter": filter,
        }
        requested = _specified(items)
        if not requested:
            raise _invalid(
                "No item to change was specified "
                f"(specify at least one of {' / '.join(items)})",
                {"channel": channel},
            )

        args = {"channel": channel, **requested}
        with self._audited("configure_math", args) as record:
            before = driver.get_math_config(channel)
            record.before(before)
            applied = driver.configure_math(channel, **requested)
            after = driver.get_math_config(channel)
            record.after(after)

        return {
            "channel": after["channel"],
            "requested": requested,
            "applied": applied,
            "changed": _math_settings(before) != _math_settings(after),
        }

    # -- カーソル・計測器・ヒストグラム(Phase M2)-------------------------

    def configure_cursor(
        self,
        driver: ScopeDriver,
        *,
        mode: str | None = None,
        # `type` は組込み名と重なるが、Tool引数名(ガイドの :TYPE)を優先する
        type: str | None = None,
        source: str | None = None,
        source1: str | None = None,
        source2: str | None = None,
        ax: float | None = None,
        ay: float | None = None,
        bx: float | None = None,
        by: float | None = None,
    ) -> dict:
        """カーソル測定を設定する(SAFE_WRITE)。未指定の項目は変更しない。

        configure_math / configure_decode と同じ根拠で承認は要求しない: 画面の
        カーソルを動かすだけの完全に可逆な操作で、取り込み設定にも出力にも触れない。

        `changed` の判定はカーソルの**設定**だけで行う(読み値 —
        ΔX・ΔY等 — は get_cursor_measurement の責務で、常に動く)。
        """
        items = {
            "mode": mode,
            "type": type,
            "source": source,
            "source1": source1,
            "source2": source2,
            "ax": ax,
            "ay": ay,
            "bx": bx,
            "by": by,
        }
        requested = _specified(items)
        if not requested:
            raise _invalid(
                "No item to change was specified "
                f"(specify at least one of {' / '.join(items)})",
                {},
            )

        with self._audited("configure_cursor", requested) as record:
            before = driver.get_cursor_config()
            record.before(before)
            applied = driver.configure_cursor(**requested)
            after = driver.get_cursor_config()
            record.after(after)

        return {
            "mode": after["mode"],
            "requested": requested,
            "applied": applied,
            "changed": _cursor_settings(before) != _cursor_settings(after),
        }

    def configure_meter(
        self,
        driver: ScopeDriver,
        kind: str,
        *,
        enabled: bool | None = None,
        source: str | None = None,
        mode: str | None = None,
        digits: int | None = None,
        totalize_enabled: bool | None = None,
        clear_totalize: bool | None = None,
    ) -> dict:
        """周波数カウンタ / 電圧計を設定する(SAFE_WRITE)。未指定の項目は変更しない。

        承認を要求しない根拠は configure_cursor と同じ(測定表示のみ)。

        `clear_totalize` は設定ではなく総カウントの一発クリアなので、**設定を
        送り終えた後**に実行する(「モードを変えて数え直す」が1往復で済む)。
        カウンタ専用の統計なので、電圧計へ指定すれば送信前に拒否する。
        設定を1つも伴わないクリアだけの呼び出しも受け付ける。
        """
        items = {
            "enabled": enabled,
            "source": source,
            "mode": mode,
            "digits": digits,
            "totalize_enabled": totalize_enabled,
            "clear_totalize": clear_totalize,
        }
        requested = _specified(items)
        if not requested:
            raise _invalid(
                "No item to change was specified "
                f"(specify at least one of {' / '.join(items)})",
                {"kind": kind},
            )
        if clear_totalize is not None and kind != "counter":
            raise _invalid(
                "clear_totalize is only available for the counter meter "
                f"(kind is {kind!r}): the totalized count is a counter statistic",
                {"kind": kind},
            )

        settings = {
            key: value for key, value in requested.items() if key != "clear_totalize"
        }
        args = {"kind": kind, **requested}
        with self._audited("configure_meter", args) as record:
            before = driver.get_meter_config(kind)
            record.before(before)
            # 設定が空(クリアのみ)なら設定コマンドは1件も送らない
            applied = (
                driver.configure_meter(kind, **settings)
                if settings
                else {"kind": kind}
            )
            if clear_totalize:
                driver.clear_counter_totalize()
                applied["clear_totalize"] = True
            after = driver.get_meter_config(kind)
            record.after(after)

        return {
            "kind": after["kind"],
            "requested": requested,
            "applied": applied,
            "changed": before != after,
        }

    def configure_histogram(
        self,
        driver: ScopeDriver,
        *,
        enabled: bool | None = None,
        # `type` は組込み名と重なるが、Tool引数名(ガイドの :TYPE)を優先する
        type: str | None = None,
        source: str | None = None,
        height: int | None = None,
        left_s: float | None = None,
        right_s: float | None = None,
        bottom_v: float | None = None,
        top_v: float | None = None,
        reset: bool | None = None,
    ) -> dict:
        """ヒストグラムを設定する(SAFE_WRITE)。未指定の項目は変更しない。

        承認を要求しない根拠は configure_cursor と同じ(表示・解析層のみ)。

        `reset` は設定ではなく統計の一発リセットなので、**設定を送り終えた後**に
        実行する(「対象を変えて取り直す」が1往復で済む)。設定を1つも伴わない
        リセットだけの呼び出しも受け付ける。

        `changed` の判定はヒストグラムの**設定**だけで行う(統計はヒット数が
        増え続ける動的値で、get_histogram_result の責務)。
        """
        items = {
            "enabled": enabled,
            "type": type,
            "source": source,
            "height": height,
            "left_s": left_s,
            "right_s": right_s,
            "bottom_v": bottom_v,
            "top_v": top_v,
            "reset": reset,
        }
        requested = _specified(items)
        if not requested:
            raise _invalid(
                "No item to change was specified "
                f"(specify at least one of {' / '.join(items)})",
                {},
            )

        settings = {key: value for key, value in requested.items() if key != "reset"}
        with self._audited("configure_histogram", requested) as record:
            before = driver.get_histogram_config()
            record.before(before)
            # 設定が空(リセットのみ)なら設定コマンドは1件も送らない
            applied = driver.configure_histogram(**settings) if settings else {}
            if reset:
                driver.reset_histogram()
                applied["reset"] = True
            after = driver.get_histogram_config()
            record.after(after)

        return {
            "requested": requested,
            "applied": applied,
            "changed": before != after,
        }

    # -- リファレンス波形(Phase M3)---------------------------------------

    def configure_reference(
        self,
        driver: ScopeDriver,
        ref: int = 1,
        *,
        source: str | None = None,
        scale: float | None = None,
        offset_v: float | None = None,
        color: str | None = None,
        label: str | None = None,
        label_display: bool | None = None,
        save: bool | None = None,
        reset: bool | None = None,
    ) -> dict:
        """リファレンス波形を設定する(SAFE_WRITE)。未指定の項目は変更しない。

        承認を要求しない根拠は configure_math / configure_cursor と同じ(表示・
        解析層のみで、取り込み設定にも出力にも触れない)。

        設定ではない一発動作が2つあり、送る位置が意味的に決まっている:

        - `reset`(`:REFerence:RESet`)は垂直スケール/位置を既定へ戻す**設定**
          なので、**設定より前**に送る(後に送ると同じ呼び出しの scale /
          offset_v を捨ててしまう)
        - `save`(`:REFerence:SAVE`)は「今の波形をこの枠へ焼く」操作なので、
          ソース選択を含む設定を送り終えた**最後**に送る。**不可逆**で、その枠に
          入っていた波形は戻せない(枠にデータがあるかを問い合わせる手段も無い)

        設定を1つも伴わない、動作だけの呼び出しも受け付ける。
        """
        items = {
            "source": source,
            "scale": scale,
            "offset_v": offset_v,
            "color": color,
            "label": label,
            "label_display": label_display,
            "save": save,
            "reset": reset,
        }
        requested = _specified(items)
        if not requested:
            raise _invalid(
                "No item to change was specified "
                f"(specify at least one of {' / '.join(items)})",
                {"ref": ref},
            )

        actions = ("save", "reset")
        settings = {
            key: value for key, value in requested.items() if key not in actions
        }
        args = {"ref": ref, **requested}
        with self._audited("configure_reference", args) as record:
            before = driver.get_reference_config(ref)
            record.before(before)
            if reset:
                driver.reset_reference(ref)
            # 設定が空(動作のみ)なら設定コマンドは1件も送らない
            applied = (
                driver.configure_reference(ref, **settings)
                if settings
                else {"ref": ref}
            )
            if reset:
                applied["reset"] = True
            if save:
                driver.save_reference(ref)
                applied["save"] = True
            after = driver.get_reference_config(ref)
            record.after(after)

        return {
            "ref": after["ref"],
            "requested": requested,
            "applied": applied,
            "changed": before != after,
        }

    # -- Acquisition ------------------------------------------------------

    def run(self, driver: ScopeDriver) -> dict:
        """波形取り込みを開始する。"""
        return self._acquisition(driver, "run", driver.run)

    def stop(self, driver: ScopeDriver) -> dict:
        """波形取り込みを停止する。"""
        return self._acquisition(driver, "stop", driver.stop)

    def single(self, driver: ScopeDriver) -> dict:
        """シングルショット取り込みを行う。"""
        return self._acquisition(driver, "single", driver.single)

    def clear_measurements(self, driver: ScopeDriver) -> dict:
        """Resultビューの全測定項目を消す(SAFE_WRITE。issue #16)。

        表示のみの変更で取得条件に触れず、再測定で完全に可逆。readbackの
        対象が存在しないため requested/applied は返さない(run/stopと同型)。
        """
        with self._audited("clear_measurements", {}) as record:
            driver.clear_measurements()
            record.after({})
        return {"result": "ok"}

    def _acquisition(
        self, driver: ScopeDriver, tool: str, action: Callable[[], None]
    ) -> dict:
        with self._audited(tool, {}) as record:
            # before は監査記録専用。無効時は実機への1クエリ(≒30ms)を省く
            if self._audit.enabled:
                record.before({"trigger_status": driver.get_trigger_status()})
            action()
            status = driver.get_trigger_status()
            record.after({"trigger_status": status})

        return {"result": "ok", "trigger_status": status}

    def autoset(
        self,
        driver: ScopeDriver,
        generation: int,
        confirm_token: str | None = None,
    ) -> dict:
        """Auto Setup(RESTRICTED_WRITE)。confirmトークンによる承認を要求する。

        利用者の設定を大きく上書きするため、返却には実行済みである旨の注記と
        実行後の主要設定を必ず添える(tools.md 4章)。
        """
        self._require_confirmation(
            "autoset",
            {},
            generation,
            confirm_token,
            description=_AUTOSET_DESCRIPTION,
            risk=_AUTOSET_RISK,
        )

        with self._audited("autoset", {}) as record:
            # before はチャンネル4本分(28クエリ)を避け、水平軸とトリガに絞る。
            # 記録専用なので、監査無効時はクエリごと省く
            if self._audit.enabled:
                record.before(
                    {
                        "timebase": get_timebase_dict(driver),
                        "trigger": get_trigger_dict(driver),
                    }
                )
            driver.autoset()
            state = get_state(driver, AUTOSET_SECTIONS)
            record.after(
                {"timebase": state["timebase"], "trigger": state["trigger"]}
            )

        return {"result": "ok", "note": AUTOSET_NOTE, "state": state}

    # -- 承認(Requirements.md 6.2)---------------------------------------

    def _require_confirmation(
        self,
        tool: str,
        args: dict,
        generation: int,
        confirm_token: str | None,
        description: str,
        risk: str,
    ) -> None:
        """トークンが無ければ発行して中断し、あれば検証・消費する。

        中断する場合、機器へは1コマンドも送っていない。
        """
        if confirm_token is None:
            request = self._confirm.issue(tool, args, description, risk, generation)
            self._audit.record_confirm("issued", tool, token_digest(request.token))
            raise ScopeError(
                ErrorCode.USER_CONFIRMATION_REQUIRED,
                f"{description}. {risk}",
                {
                    "confirm_token": request.token,
                    "description": request.description,
                    "risk": request.risk,
                    "instruction": request.instruction,
                    "expires_in_s": request.expires_in_s,
                },
            )

        digest = token_digest(confirm_token)
        try:
            self._confirm.consume(confirm_token, tool, args, generation)
        except ScopeError:
            self._audit.record_confirm("rejected", tool, digest)
            raise
        self._audit.record_confirm("consumed", tool, digest)

    # -- 監査(Requirements.md 7.6)---------------------------------------

    def _audited(self, tool: str, requested: dict) -> AuditScope:
        """Before / Action / After を1行の監査記録にまとめるコンテキスト。"""
        return AuditScope(self._audit, tool, requested)
