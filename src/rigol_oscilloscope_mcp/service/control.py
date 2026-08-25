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


def _invalid(message: str, detail: dict) -> ScopeError:
    return ScopeError(ErrorCode.INVALID_PARAMETER, message, detail)


def _specified(values: dict) -> dict:
    """`None` でない項目だけを、宣言順のまま取り出す。"""
    return {key: value for key, value in values.items() if value is not None}


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
            before = driver.get_decode_config(bus)
            record.before(before)
            applied = driver.configure_decode(
                bus,
                protocol,
                enabled=enabled,
                event_table=event_table,
                data_format=data_format,
                settings=settings,
            )
            after = driver.get_decode_config(bus)
            record.after(after)

        return {
            "bus": after["bus"],
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
