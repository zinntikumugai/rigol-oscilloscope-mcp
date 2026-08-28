"""MCPサーバー(Phase 1: Read Only / Phase 2: 書き込み系 / Phase 4: デコード・信号発生 / tools.md 10章)。

MCP SDK(FastMCP)への依存は本モジュールに閉じ込め、下位層(service / driver)
はSDKを知らないまま保つ。

Tool実装の規約:

- 本体は全て**同期関数**とし、登録ラッパー(`_checked_tool`)が `manager.lock`
  で機器アクセス全体を囲んでSCPI送受信を直列化する(Requirements.md 6.5)。
  各Tool本体はロックを意識しない
- `ScopeError` はMCPのエラー応答にせず、`{"error": true, "code": ...}` の
  **正常返却**へ変換する(同じく登録ラッパーが担う)。LLMがコードを機械的に
  読めるようにするため(tools.md 0.3)
- 返却は `dict`(JSONプリミティブのみ)。SDKはこれを1つのtext contentとして
  JSON整形する。スクリーンショットのみ `[メタデータdict, Image]` を返し、
  text + image の2 contentになる
"""

from __future__ import annotations

import inspect
import json
import logging
import os
import sys
from collections.abc import Callable, Mapping
from contextlib import AbstractContextManager
from dataclasses import asdict
from functools import wraps
from typing import Any

from mcp.server.fastmcp import FastMCP, Image

from . import service
from .config import Config, load_config
from .errors import ErrorCode, ScopeError
from .safety import AuditLogger, ConfirmTokenStore, OperationClass, classify
from .service import ConnectionManager, ConnectionStatus, ControlService
from .service.connection import DISCONNECTED_MESSAGE

SERVER_NAME = "rigol-oscilloscope-mcp"

INSTRUCTIONS = (
    "Server for controlling Rigol oscilloscopes over SCPI. "
    "Connect first with connect (ask the user for the device address), "
    "then check the current settings with get_state before operating. "
    "For numeric readings, prefer measure over the capture_screenshot image."
)

# errors.ErrorCode は機器由来のコード集合。想定外の例外はそれと区別する。
INTERNAL_ERROR = "INTERNAL_ERROR"

# 実機なしで手動E2Eを行うためのフラグ(FakeScopeを機器の代わりに使う)
FAKE_ENV_VAR = "RIGOL_MCP_FAKE"
_TRUTHY = frozenset({"1", "true", "yes", "on"})


# --------------------------------------------------------------------------
# 組み立て
# --------------------------------------------------------------------------


#: config.log_level(9章)→ logging のレベル定数
_LOG_LEVELS = {
    "error": logging.ERROR,
    "warn": logging.WARNING,
    "info": logging.INFO,
    "debug": logging.DEBUG,
}

PACKAGE_LOGGER = __name__.rsplit(".", 1)[0]


def _configure_logging(level: str) -> None:
    """パッケージロガーだけを設定する(Requirements.md 8.3)。

    MCPは**stdoutをプロトコルに使う**ため、出力先は必ずstderr。ルートロガーを
    汚さないよう basicConfig は使わず、伝播も切る。多重呼び出しで
    ハンドラが積み重ならないよう、未設定のときだけ1つ付ける。
    """
    logger = logging.getLogger(PACKAGE_LOGGER)
    logger.setLevel(_LOG_LEVELS.get(level, logging.INFO))
    logger.propagate = False
    if not logger.handlers:
        handler = logging.StreamHandler(sys.stderr)
        handler.setFormatter(logging.Formatter("%(levelname)s %(name)s: %(message)s"))
        logger.addHandler(handler)


def _fake_enabled(env: Mapping[str, str] | None = None) -> bool:
    value = (os.environ if env is None else env).get(FAKE_ENV_VAR)
    return value is not None and value.strip().lower() in _TRUTHY


def _fake_transport_factory() -> Callable[[str, str, int, float], Any]:
    """FakeScope を1台だけ持ち、再接続でも同じ状態を返すファクトリ。"""
    from .testing import FakeScope, FakeTransport  # noqa: PLC0415 - 通常経路には載せない

    scope = FakeScope()

    def factory(transport: str, address: str, port: int, timeout_s: float) -> FakeTransport:
        return FakeTransport(scope)

    return factory


def _build_manager(config: Config, audit: AuditLogger) -> ConnectionManager:
    factory = _fake_transport_factory() if _fake_enabled() else None
    return ConnectionManager(config, transport_factory=factory, audit=audit)


# --------------------------------------------------------------------------
# エラー変換・返却整形
# --------------------------------------------------------------------------


def _tool_result(fn: Callable[..., Any]) -> Callable[..., Any]:
    """例外を機械可読なエラーdictへ変換する(MCPのisErrorには頼らない)。"""

    @wraps(fn)
    def wrapper(*args: Any, **kwargs: Any) -> Any:
        try:
            return fn(*args, **kwargs)
        except ScopeError as exc:
            return {"error": True, **exc.to_dict()}
        except Exception as exc:  # noqa: BLE001 - Toolは例外を漏らさず返す
            return {
                "error": True,
                "code": INTERNAL_ERROR,
                "message": str(exc),
                "detail": {"type": type(exc).__name__},
            }

    return wrapper


#: 承認(confirmトークン)を要求する操作クラス(Requirements.md 6.1 / 6.2)
_CONFIRM_REQUIRED = (OperationClass.RESTRICTED_WRITE, OperationClass.DANGEROUS_WRITE)


def _locked(lock: AbstractContextManager[Any]) -> Callable[..., Any]:
    """Tool本体全体を機器アクセスのロックで囲む(Requirements.md 6.5)。"""

    def decorate(fn: Callable[..., Any]) -> Callable[..., Any]:
        @wraps(fn)
        def wrapper(*args: Any, **kwargs: Any) -> Any:
            with lock:
                return fn(*args, **kwargs)

        return wrapper

    return decorate


def _checked_tool(
    server: FastMCP, lock: AbstractContextManager[Any]
) -> Callable[[Callable[..., Any]], Callable[..., Any]]:
    """操作クラス表と整合するToolだけを登録するデコレータを返す。

    表(safety/classes.py)を静的な飾りにせず、起動時の不変条件にする:
    表に無いTool名は `classify` の fail-closed で、承認必須クラスなのに
    `confirm_token` を受けないToolは SAFETY_POLICY_DENIED で起動を失敗させる。

    併せて、全Toolに共通の定型(ロック取得とエラー変換)をここで一度だけ被せる。
    実効順序はエラー変換が最外・ロックが本体側(ロック解放後に変換される)。
    """

    def register(fn: Callable[..., Any]) -> Callable[..., Any]:
        if classify(fn.__name__) in _CONFIRM_REQUIRED:
            if "confirm_token" not in inspect.signature(fn).parameters:
                raise ScopeError(
                    ErrorCode.SAFETY_POLICY_DENIED,
                    f"{fn.__name__} requires confirmation for its operation class "
                    f"but has no confirm_token parameter",
                    {"tool": fn.__name__},
                )
        return server.tool()(_tool_result(_locked(lock)(fn)))

    return register


def _profile_dict(name: str | None, confidence: str | None) -> dict | None:
    return None if name is None else {"name": name, "confidence": confidence}


def _status_dict(status: ConnectionStatus) -> dict:
    """ConnectionStatus をTool返却用のJSON dictへ(tools.md 1章)。"""
    data: dict[str, Any] = {
        "connected": status.connected,
        "address": status.address,
        "transport": status.transport,
        "port": status.port,
        "idn": asdict(status.idn) if status.idn is not None else None,
        "profile": _profile_dict(status.profile_name, status.profile_confidence),
        "unsupported_vendor": status.unsupported_vendor,
    }
    if not status.connected:
        # 未接続はエラーにせず、次の一手(connect)をLLMへ示す
        data["message"] = DISCONNECTED_MESSAGE
    return data


# --------------------------------------------------------------------------
# サーバー生成
# --------------------------------------------------------------------------


def create_server(
    config: Config | None = None,
    connection_manager: ConnectionManager | None = None,
) -> FastMCP:
    """Phase 1 / Phase 2 / Phase 4 のToolを登録したサーバーを組み立てる。

    `config` 省略時は環境変数・設定ファイルから解決する。
    `connection_manager` 省略時は生成し、`RIGOL_MCP_FAKE=1` なら実機の代わりに
    FakeScope へ接続する(実機なしでの手動確認用)。
    """
    resolved_config = load_config() if config is None else config
    _configure_logging(resolved_config.log_level)
    audit = AuditLogger(resolved_config.audit_log)
    print(f"audit log: {audit.path or 'disabled'}", file=sys.stderr)
    manager = (
        _build_manager(resolved_config, audit)
        if connection_manager is None
        else connection_manager
    )
    # confirmトークンはサーバー(=セッション)寿命で共有する。世代バインドは
    # 呼び出しごとに manager.generation を渡すことで効かせる(Requirements.md 6.2)
    control = ControlService(ConfirmTokenStore(), audit)

    server = FastMCP(SERVER_NAME, instructions=INSTRUCTIONS)
    _register = _checked_tool(server, manager.lock)

    # -- 接続管理 ---------------------------------------------------------

    @_register
    def connect(
        address: str | None = None,
        transport: str | None = None,
        port: int | None = None,
    ) -> dict:
        """Connect to the oscilloscope.

        Pass the address the user gave you (IP address etc.) as address.
        If you do not know it, ask the user instead of guessing. When omitted,
        transport is inferred from the address format ("lan" / "usb") and port
        falls back to the profile default. Any existing connection is replaced.
        """
        return _status_dict(manager.connect(address=address, transport=transport, port=port))

    @_register
    def disconnect() -> dict:
        """Close the current connection (not an error if not connected)."""
        manager.disconnect()
        return _status_dict(manager.status())

    @_register
    def scope_identify() -> dict:
        """Return the connection state and device identity (*IDN?, profile).

        Not an error when disconnected; returns connected: false instead.
        """
        return _status_dict(manager.status())

    @_register
    def get_capabilities() -> dict:
        """Return the features available on the connected device (channel count, supported features).

        Profile confidence is verified, family, guide (decoded from the official
        programming guide only, not confirmed on real hardware), or generic.
        Below verified, unverified features are restricted.
        options reports the installed license options, and is null when this
        model does not support option queries.
        """
        driver = manager.require_scope()
        status = manager.status()
        try:
            options = driver.installed_options()
        except ScopeError as exc:
            if exc.code != ErrorCode.UNSUPPORTED_FEATURE:
                raise
            options = None
        return {
            "profile": _profile_dict(driver.profile.name, driver.profile.confidence),
            "capabilities": dict(driver.profile.capabilities),
            "options": options,
            "unsupported_vendor": status.unsupported_vendor,
        }

    # -- 状態取得 ---------------------------------------------------------

    @_register
    def get_state(sections: list[str] | None = None) -> dict:
        """Get the main settings (channels / timebase / trigger / acquisition) in one call.

        When you know what you need, narrowing with sections is much faster
        (a full read is about 39 queries and can take several seconds).
        Omitting sections returns every section.
        """
        return service.get_state(manager.require_scope(), sections)

    @_register
    def get_channel(channel: str) -> dict:
        """Return the state of one channel ("CH1" to "CH4")."""
        return service.get_channel_dict(manager.require_scope(), channel)

    @_register
    def get_timebase() -> dict:
        """Return the horizontal (timebase) state."""
        return service.get_timebase_dict(manager.require_scope())

    @_register
    def get_trigger() -> dict:
        """Return the trigger settings and status."""
        return service.get_trigger_dict(manager.require_scope())

    @_register
    def get_acquisition_state() -> dict:
        """Return the acquisition state (whether it is running, and the trigger status)."""
        return service.get_acquisition_dict(manager.require_scope())

    # -- 測定・データ取得 -------------------------------------------------

    @_register
    def measure(
        channel: str, measurements: list[str], channel_b: str | None = None
    ) -> dict:
        """Measure the given channel. Returned values use SI-suffixed keys
        (frequency_hz, vpp_v, ...); do not trust a value whose quality is not valid.

        Timing: frequency, period, rise_time, fall_time, pulse_width_pos,
        pulse_width_neg, duty, duty_neg, time_at_vmax, time_at_vmin.
        Amplitude: vpp, vmax, vmin, vtop, vbase, vamp, vupper, vmid, vlower,
        vavg, rms, period_rms, ac_rms, overshoot, preshoot.
        Area and slew rate: area, period_area, slew_rate_pos, slew_rate_neg.
        Counts: pulses_pos, pulses_neg, edges_pos, edges_neg.
        Two-source: delay_rise_rise, delay_rise_fall, delay_fall_rise,
        delay_fall_fall, phase_rise_rise, phase_rise_fall, phase_fall_rise,
        phase_fall_fall.

        The two-source items compare channel against channel_b, so pass
        channel_b for them and leave it out otherwise. Availability is
        model-dependent (get_capabilities measurements).
        """
        return service.measure(
            manager.require_scope(), channel, measurements, channel_b
        )

    @_register
    def clear_measurements() -> dict:
        """Remove all measurement items from the on-screen Result view.

        Reading measurements (measure) also enables each item on the
        instrument's Result view, so items accumulate on screen over time.
        This clears them all; re-measuring restores any item.
        """
        return control.clear_measurements(manager.require_scope())

    @_register
    def capture_waveform(channel: str, max_points: int | None = None) -> dict:
        """Capture waveform data and return it converted to volts (V).

        channel is "CH1"-"CH4" or a math trace "MATH1"-"MATH4" (configure one
        with configure_math). A math trace is read as the data currently
        displayed on screen, so turn its display on first.

        When there are many points the data is written to a CSV file and its
        path is returned in data_file. Screen data may be decimated, so read the
        effective sample rate as the reciprocal of sample_interval_s.

        A math trace using the fft operator has a frequency x axis: it returns
        x_unit "Hz" (sample_interval_s is then the frequency step in hertz and
        time_origin_s the start frequency) and no
        effective_sample_rate_sa_per_s. Every other source keeps the time axis
        and the usual shape.
        """
        return service.capture_waveform(
            manager.require_scope(), resolved_config, channel, max_points
        )

    @_register
    def analyze_waveform(
        channel: str = "CH1",
        analyses: list[str] | None = None,
        max_points: int | None = None,
    ) -> dict:
        """Analyze a waveform on the host and return only the summary.

        The raw samples are never returned; use capture_waveform when the data
        itself is needed. analyses is a subset of ["stats", "fft"] (all of them
        when omitted). stats gives min/max/mean/rms/std/vpp in volts; fft gives
        the dominant frequency and the strongest peaks. Frequency accuracy is
        limited by frequency_resolution_hz, so do not read more digits than that.

        channel is "CH1"-"CH4" or a math trace "MATH1"-"MATH4". A math trace
        using the fft operator is rejected: its x axis is already frequency, so
        time-domain statistics and a host-side FFT are meaningless. Read the
        instrument's own peak table with get_math_state, or fetch the spectrum
        points with capture_waveform.
        """
        return service.analyze_waveform(
            manager.require_scope(), resolved_config, channel, analyses, max_points
        )

    @_register
    def capture_screenshot(
        path: str | None = None,
        format: str | None = None,
        return_image: bool = True,
    ) -> Any:
        """Capture the screen, save it, and also return the image (for visual checks).

        path is the destination directory or file (defaults to the configured
        default directory). A relative path is resolved against the invocation
        directory (the default save location). Saving outside the allowed roots
        is rejected (add roots with RIGOL_MCP_ALLOWED_DIRS).
        format is png / jpg / jpeg / bmp / webp. With return_image=false only the
        metadata is returned, without the image (saves tokens).
        For numeric readings use measure, not this image.
        """
        shot = service.capture_screenshot(
            manager.require_scope(), resolved_config, path=path, format=format
        )
        metadata = {
            "saved_path": shot.saved_path,
            "format": shot.format,
            "size_bytes": shot.size_bytes,
            "mime": shot.mime,
        }
        if not return_image:
            return metadata
        return [metadata, Image(data=shot.image_bytes, format=shot.format)]

    # -- 設定変更(tools.md 3章)-------------------------------------------

    @_register
    def configure_channel(
        channel: str,
        enabled: bool | None = None,
        scale_v_per_div: float | None = None,
        offset_v: float | None = None,
        coupling: str | None = None,
        probe_ratio: float | None = None,
        bandwidth_limit: bool | None = None,
        impedance: str | None = None,
        confirm_token: str | None = None,
    ) -> dict:
        """Configure the vertical axis (a channel). Omitted items are left unchanged.

        channel is "CH1" to "CH4", coupling is DC / AC / GND, impedance is
        "1M" / "50". Specify at least one item to change. The device may snap
        values, so trust applied (the read-back value), not requested.

        impedance="50" risks damaging the device and needs the confirmation flow:
        the first call does not execute and returns a confirm_token, so ask the
        human user whether to proceed and then call again with the same
        arguments plus that confirm_token.
        """
        return control.configure_channel(
            manager.require_scope(),
            manager.generation,
            channel,
            enabled=enabled,
            scale_v_per_div=scale_v_per_div,
            offset_v=offset_v,
            coupling=coupling,
            probe_ratio=probe_ratio,
            bandwidth_limit=bandwidth_limit,
            impedance=impedance,
            confirm_token=confirm_token,
        )

    @_register
    def configure_timebase(
        scale_s_per_div: float | None = None,
        position_s: float | None = None,
    ) -> dict:
        """Configure the horizontal axis (timebase). Omitted items are left unchanged.

        Specify at least one item to change. The device may snap values, so trust
        applied (the read-back value).
        """
        return control.configure_timebase(
            manager.require_scope(),
            scale_s_per_div=scale_s_per_div,
            position_s=position_s,
        )

    @_register
    def configure_trigger(
        source: str | None = None,
        level_v: float | None = None,
        slope: str | None = None,
        sweep_mode: str | None = None,
    ) -> dict:
        """Configure the edge trigger. Omitted items are left unchanged.

        source is "CH1" to "CH4", slope is rising / falling / either, and
        sweep_mode is auto / normal / single. Specify at least one item to change.
        """
        return control.configure_trigger(
            manager.require_scope(),
            source=source,
            level_v=level_v,
            slope=slope,
            sweep_mode=sweep_mode,
        )

    # -- シリアルデコード(tools.md 6章)-------------------------------------

    @_register
    def configure_decode(
        protocol: str,
        bus: int = 1,
        enabled: bool | None = None,
        event_table: bool | None = None,
        data_format: str | None = None,
        settings: dict | None = None,
    ) -> dict:
        """Configure a serial protocol decode bus. Omitted items are left unchanged.

        The bus count is model-dependent (get_capabilities decode_buses; 4 on MHO98).

        protocol is uart / i2c / spi / can / lin / parallel (options such as
        I2S, FlexRay, MIL-STD-1553 and CAN-FD are not supported).
        data_format is hex / ascii / dec / bin. Source values are "CH1"-"CH4",
        "D0"-"D15" or "off".

        settings keys per protocol (all optional):
        - uart: tx_source, rx_source, baud_bps, data_bits, parity (none/odd/even),
          stop_bits (1/1.5/2), endian (msb/lsb), polarity (positive/negative),
          tx_threshold_v, rx_threshold_v. Example: {"tx_source": "CH1",
          "baud_bps": 115200, "data_bits": 8, "parity": "none", "stop_bits": 1,
          "tx_threshold_v": 1.65}
        - i2c: scl_source, sda_source, swap_sda_scl, address_bits (7/8/10),
          scl_threshold_v, sda_threshold_v. Example: {"scl_source": "CH1",
          "sda_source": "CH2", "address_bits": 7}
        - spi: clk_source, clk_slope (rising/falling), mosi_source, miso_source,
          cs_source, cs_polarity (high/low), frame_mode (cs/timeout), timeout_s,
          data_bits (4-32), endian, polarity (high/low), clk_threshold_v,
          mosi_threshold_v, miso_threshold_v, cs_threshold_v. Example:
          {"clk_source": "CH1", "mosi_source": "CH2", "data_bits": 8}
        - can: source, signal_type (tx/rx/canh/canl/differential), baud_bps,
          sample_point_percent, threshold_v. Example: {"source": "CH1",
          "signal_type": "canh", "baud_bps": 500000}
        - lin: source, baud_bps, parity_enabled, standard (v1x/v2x/mixed),
          threshold_v. Example: {"source": "CH1", "baud_bps": 19200,
          "standard": "v2x"}
        - parallel: clk_source, clk_slope, bus (d7_d0/d15_d8/d15_d0/d0_d7/
          d8_d15/d0_d15/ch1-ch4/user), bus_width (1-16), bit_sources, endian,
          polarity. bus is the data source; the digital groups list the MSB
          first. bus_width and bit_sources only work while bus is "user" (the
          device rejects them otherwise), so set bus in the same call.
          bit_sources is a list of "CH1"-"CH4" / "D0"-"D15", one per data bit
          starting at bit 0, and no longer than bus_width. Example:
          {"bus": "user", "bus_width": 2, "bit_sources": ["CH1", "CH2"]}

        Set event_table=true (together with enabled=true) before reading the
        decoded results with get_decode_result.
        This only changes what the device displays and analyses: acquisition
        settings are untouched, so configure the channels and trigger separately.
        """
        return control.configure_decode(
            manager.require_scope(),
            bus,
            protocol,
            enabled=enabled,
            event_table=event_table,
            data_format=data_format,
            settings=settings,
        )

    @_register
    def get_decode_result(bus: int = 1, max_events: int | None = None) -> dict:
        """Read the decoded event table of a decode bus (bus 1-4).

        Call configure_decode with enabled=true and event_table=true first;
        otherwise no table is read and the reason is returned in warnings.
        Stop the acquisition (stop) before reading, or the table keeps changing
        between reads and is only a snapshot.

        The column names depend on the protocol and on the device (for example
        time_s, tx_rx, data, error for UART/RS232); read columns instead of
        assuming a fixed layout. time_s is in seconds relative to the trigger,
        and the other cells are strings formatted as the data_format of
        configure_decode selects (hex / ascii / dec / bin).

        max_events returns only the first N events; event_count is always the
        total number of events on the device before truncation.
        """
        return service.get_decode_result(manager.require_scope(), bus, max_events)

    # -- 信号発生(tools.md 7章)-------------------------------------------

    @_register
    def configure_afg(
        channel: int = 1,
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
        """Configure the built-in function generator (AFG). Omitted items are left unchanged.

        This never turns the generator output on or off. The output state is not
        touched at all, so nothing new reaches the wiring: a configured
        generator only emits a signal once its output is enabled with the
        separate, confirmation-gated tool enable_afg (and disable_afg turns it
        off again). Read the current output state with get_afg_state.

        channel is the generator channel (1 or 2 on MHO98; see get_capabilities
        afg_channels). Specify at least one item to change.

        waveform is sine / square / ramp / noise / dc / arb / exp_rise /
        exp_fall / ecg / gaussian / lorentz / haversine / sinc.
        amplitude_vpp is the peak-to-peak amplitude in volts (not the peak and
        not RMS), offset_v the DC offset in volts, frequency_hz the frequency in
        hertz, phase_deg the phase in degrees (0-360), duty_percent the duty
        cycle of the square wave (1-99) and symmetry_percent the symmetry of the
        ramp (0-100). Duty and symmetry are stored independently of the current
        waveform, so they can be set at any time.

        impedance is "highz" or "50" and is the GENERATOR's own output impedance
        setting, i.e. the load the amplitude is calibrated for. It has nothing to
        do with the oscilloscope input impedance of configure_channel.

        The frequency and amplitude limits depend on the installed options and
        on impedance, and the instrument clamps an out-of-range value silently
        (no error is reported): always compare applied (the read-back value)
        against requested. Writing a frequency while the waveform is dc or noise
        is rejected by the instrument.

        arb_file selects an existing arbitrary waveform file already stored on
        the instrument (local C:/... or USB D:/...), e.g. arb_file="D:/my.csv"
        together with waveform="arb". This server never creates, uploads or
        deletes instrument files - it only selects one that is already there.

        modulation configures AM/FM/PM (internal source only; there is no
        external modulation input). Give a dict with any of: enabled (bool),
        type ("am"/"fm"/"pm"), am_depth_percent (0-120), fm_deviation_hz (>0),
        pm_deviation_deg (0-360), frequency_hz (the MODULATING frequency, not
        the carrier - 2 mHz to 1 MHz), waveform (sine/square/triangle/upramp/
        dnramp/noise, the modulating waveform). frequency_hz and waveform are
        routed to the type given in the same call, or otherwise to whatever
        type is currently set on the instrument. The instrument silently
        ignores modulation parameter writes while modulation is off, so pass
        enabled=true together with the parameters (the server sends the
        enable before the parameters); parameters alone are rejected while
        modulation is off. Enabling modulation does NOT turn the output on,
        but if the output is already on, modulation takes effect immediately.
        """
        return control.configure_afg(
            manager.require_scope(),
            channel,
            waveform=waveform,
            frequency_hz=frequency_hz,
            amplitude_vpp=amplitude_vpp,
            offset_v=offset_v,
            phase_deg=phase_deg,
            duty_percent=duty_percent,
            symmetry_percent=symmetry_percent,
            impedance=impedance,
            arb_file=arb_file,
            modulation=modulation,
        )

    @_register
    def get_afg_state(channel: int | None = None) -> dict:
        """Return the function generator settings, including whether the output is on.

        With channel given, the settings of that channel are returned flat
        (channel, output, waveform, impedance, frequency_hz, amplitude_vpp,
        offset_v, phase_deg, duty_percent, symmetry_percent). With channel
        omitted, every generator channel is returned under channels, keyed by
        the channel number as a string: {"channels": {"1": {...}, "2": {...}}}.

        output tells whether the generator is currently driving its connector.
        Reading never changes it. modulation reports the modulation settings
        (enabled, type, the effective type's depth/deviation, frequency_hz,
        waveform). This is read-only: it costs about 14 queries per channel.
        """
        driver = manager.require_scope()
        if channel is not None:
            return driver.get_afg_config(channel)
        # 非対応機では afg_channels が 0。1本だけ問い合わせて
        # UNSUPPORTED_FEATURE を返させる(空dictを「正常」に見せない)
        count = driver.afg_channels or 1
        return {
            "channels": {
                str(n): driver.get_afg_config(n) for n in range(1, count + 1)
            }
        }

    @_register
    def enable_afg(channel: int = 1, confirm_token: str | None = None) -> dict:
        """DANGEROUS: turn the function generator output on (a real signal starts coming out).

        This is the only tool that makes the instrument drive a signal into
        whatever is wired to the generator output, so it needs the confirmation
        flow: the first call does not execute and returns a confirm_token, and
        only a second call carrying that confirm_token turns the output on.
        The token is bound to this channel, is single use, and expires.

        Before asking for confirmation, read the settings back with
        get_afg_state and show the human user what is about to be driven
        (waveform, frequency_hz, amplitude_vpp, offset_v) - those values take
        effect the instant the output turns on. Then ask the human user what is
        connected to the generator output and whether it is safe to drive it.
        Never confirm on your own, and never drive a live or powered circuit.

        Returns the settings of the channel in state, with output true.
        Turn the output off again with disable_afg.
        """
        return control.enable_afg(
            manager.require_scope(), manager.generation, channel, confirm_token
        )

    @_register
    def disable_afg(channel: int = 1) -> dict:
        """Turn the function generator output off immediately (no signal comes out any more).

        No confirmation is needed by design: stopping the output is always the
        safe direction, so it must never be blocked by the confirmation flow.
        Use it as soon as the measurement is done, and whenever the user asks
        for the signal to stop. The waveform settings are kept, so enable_afg
        drives the same signal again.

        Returns the settings of the channel in state, with output false.
        """
        return control.disable_afg(manager.require_scope(), channel)

    @_register
    def sync_afg_phase(channel: int = 1) -> dict:
        """Align the phase of both function generator channels to their preset settings.

        This re-applies both AFG channels' preset frequency and phase so their
        phases line up; it only has a visible effect when the two channels'
        frequencies are identical or one is an integer multiple of the other.
        It does not touch amplitude or output state (no confirmation needed).
        channel selects which channel's SCPI prefix issues the command, but
        both generator channels are affected.
        """
        return control.sync_afg_phase(manager.require_scope(), channel)

    # -- MATH演算(tools.md、Phase M1)--------------------------------------

    @_register
    def configure_math(
        channel: int = 1,
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
        filter: dict | None = None,
    ) -> dict:
        """Configure a math (waveform arithmetic) trace. Omitted items are left unchanged.

        This computes a new trace inside the instrument from channels already
        being acquired; it changes nothing about the acquisition itself and
        drives no output. channel is the math trace 1-4 (see get_capabilities
        math_channels). Specify at least one item to change. Read the result
        back with get_math_state, and fetch the trace with
        capture_waveform(channel="MATH1").

        operator is add / subtract / multiply / divide / and / or / xor / not /
        fft / integrate / differentiate / sqrt / log10 / ln / exp / abs /
        lowpass / highpass / bandpass / bandstop / axb.

        source1 and source2 are the operands of the arithmetic operators:
        "CH1"-"CH4", "REF1"-"REF10", or another math trace "MATH1"-"MATH4".
        A math trace may only use a LOWER-numbered one (MATH2 can read MATH1,
        never MATH2 or MATH3), so cascade upwards.
        lsource1 and lsource2 are the operands of the logic operators
        (and / or / xor / not) and take "D0"-"D15" or "CH1"-"CH4" instead.
        scale is the vertical scale per division and offset_v the vertical
        offset in volts of the resulting trace; invert flips it vertically.

        fft is a dict for the fft operator, with any of: source (the input
        channel of the FFT - this is what selects it, not source1), window
        (rectangle / blackman / hanning / hamming / flattop / triangle), unit
        (vrms / db), mode (normal / average / maxhold), average_count (2-1000),
        scale and offset (vertical, in the unit above), freq_start_hz and
        freq_end_hz (the displayed span in hertz), search_enabled (bool, turns
        the instrument's peak table on), search_num (how many peaks),
        search_threshold and search_excursion (in the vertical unit), and
        search_order (amplitude / frequency). Read the peaks themselves back
        with get_math_state.

        filter is a dict for the lowpass / highpass / bandpass / bandstop
        operators, with any of: type (lowpass / highpass / bandpass /
        bandstop), w1_hz and w2_hz (the cut-off frequencies in hertz; w1 must
        be below w2 for bandpass and bandstop).

        Which parameters are valid depends on the operator, and the instrument
        enforces that: scale and offset_v do not exist for the logic operators
        or for fft (fft has its own scale and offset inside the fft dict), and
        a rejected write is reported as an error. Set the operator in the same
        call as its parameters. The device may snap values, so trust applied
        (the read-back value), not requested.
        """
        return control.configure_math(
            manager.require_scope(),
            channel,
            display=display,
            operator=operator,
            source1=source1,
            source2=source2,
            lsource1=lsource1,
            lsource2=lsource2,
            scale=scale,
            offset_v=offset_v,
            invert=invert,
            fft=fft,
            filter=filter,
        )

    @_register
    def get_math_state(channel: int | None = None) -> dict:
        """Return the math trace settings (channel, display, operator, sources).

        With channel given (1-4), that trace's settings are returned flat. With
        channel omitted, every math trace is returned under channels, keyed by
        the trace number as a string: {"channels": {"1": {...}, ..., "4": {...}}}.

        Only the keys that mean something for the current operator are read:
        scale and offset_v for the arithmetic operators, lsource1 and lsource2
        for the logic ones, an fft dict for the fft operator and a filter dict
        for the filter ones. With the fft operator and search_enabled true, the
        instrument's own peak table is returned in peaks, each entry having
        index, frequency_hz, amplitude and amplitude_unit (lines that could not
        be parsed are returned raw and noted in peak_warnings).

        This is read-only and never changes the display. It costs a few queries
        per trace (about 20 for an fft trace).
        """
        driver = manager.require_scope()
        if channel is not None:
            return driver.get_math_config(channel)
        # 非対応機では math_channels が 0。1本だけ問い合わせて
        # UNSUPPORTED_FEATURE を返させる(空dictを「正常」に見せない)
        count = driver.math_channels or 1
        return {
            "channels": {
                str(n): driver.get_math_config(n) for n in range(1, count + 1)
            }
        }

    # -- カーソル・計測器・ヒストグラム(tools.md、Phase M2)-----------------

    @_register
    def configure_cursor(
        mode: str | None = None,
        type: str | None = None,
        source: str | None = None,
        source1: str | None = None,
        source2: str | None = None,
        ax: float | None = None,
        ay: float | None = None,
        bx: float | None = None,
        by: float | None = None,
    ) -> dict:
        """Configure the on-screen measurement cursors. Omitted items are left unchanged.

        This only moves the cursors the instrument draws over the trace: the
        acquisition is untouched and no output is driven. Read what the cursors
        report with get_cursor_measurement.

        mode is off / manual / track / xy. In manual mode both cursors are
        placed freely; in track mode they follow their source waveform.
        Positions and sources belong to the subtree of the ACTIVE mode: type
        and source are manual-only, source1 and source2 are track-only, and
        giving one to the other mode is rejected. When mode is omitted, the
        mode currently set on the instrument decides which subtree is written.
        While the mode is off or xy there is nowhere to write, so positions are
        rejected: mode="xy" is accepted as a mode (it is one the device
        supports) but its own position subtree is not exposed by this server.

        type is time / amplitude and selects what the manual cursors measure.
        source, source1 and source2 are "CH1"-"CH4", "MATH1"-"MATH4" or "NONE"
        (reference waveforms and digital channels are not valid cursor sources).

        ax and bx are the X positions of cursor A and B in seconds, ay and by
        their Y positions in volts.

        Specify at least one item to change. The device may snap values, so
        trust applied (the read-back value), not requested.
        """
        return control.configure_cursor(
            manager.require_scope(),
            mode=mode,
            type=type,
            source=source,
            source1=source1,
            source2=source2,
            ax=ax,
            ay=ay,
            bx=bx,
            by=by,
        )

    @_register
    def get_cursor_measurement() -> dict:
        """Read what the cursors currently measure (positions and deltas).

        Returns mode and, from the active manual or track subtree, ax_s and
        bx_s (the X positions in seconds), ay_v and by_v (the Y positions in
        volts), xdelta_s and ydelta_v (cursor B minus cursor A) and ixdelta_hz
        (1/deltaX, the frequency that time difference corresponds to). A
        reading the instrument cannot produce (1/deltaX with deltaX = 0) is
        returned as null.

        While the cursor mode is off or xy there is nothing to read and only
        mode is returned; place the cursors with configure_cursor first.
        """
        return manager.require_scope().get_cursor_measurement()

    @_register
    def configure_meter(
        kind: str,
        enabled: bool | None = None,
        source: str | None = None,
        mode: str | None = None,
        digits: int | None = None,
        totalize_enabled: bool | None = None,
        clear_totalize: bool | None = None,
    ) -> dict:
        """Configure the frequency counter or the digital voltmeter. Omitted items are left unchanged.

        kind selects which one: "counter" or "dvm". Both only add a reading to
        the display; the acquisition is untouched and no output is driven. Read
        the value itself with get_meter_value.

        mode for the counter is frequency / period / totalize (totalize counts
        events instead of measuring a rate). mode for the dvm is ac_rms / dc /
        dc_rms (ac_rms is the RMS with the DC component removed, dc the
        average, dc_rms the RMS of the whole signal).

        source for the counter is "CH1"-"CH4" or a digital channel "D0"-"D15";
        the dvm accepts analog channels only. enabled turns the reading on.

        digits (the counter resolution, 3-6 digits) and totalize_enabled (the
        counter's totalize statistics) exist for the counter only. How they
        couple to the mode is enforced by the instrument, not host-side: digits
        is rejected while the mode is totalize, and totalize_enabled is invalid
        in totalize mode (it applies to frequency and period). A rejected write
        comes back as an error, so set the mode in the same call as the
        parameters that depend on it.

        clear_totalize=true clears the totalized count. It is sent after the
        settings, so a single call can switch to totalize and start counting
        from zero. It is a counter-only item and the instrument accepts it in
        totalize mode only.

        Specify at least one item to change. The device may snap values, so
        trust applied (the read-back value), not requested.
        """
        return control.configure_meter(
            manager.require_scope(),
            kind,
            enabled=enabled,
            source=source,
            mode=mode,
            digits=digits,
            totalize_enabled=totalize_enabled,
            clear_totalize=clear_totalize,
        )

    @_register
    def get_meter_value(kind: str) -> dict:
        """Read the current frequency counter or digital voltmeter value with its unit.

        kind is "counter" or "dvm". The unit depends on the mode, so value is
        returned together with the mode that produced it and the matching unit:
        Hz for frequency, s for period, counts for totalize, and V for every
        dvm mode. A reading the instrument cannot produce is returned as null.

        The meter's settings come back alongside the value: value is null while
        enabled is false, because a meter that is off has no reading to give,
        and source says what is being measured. Turn the meter on with
        configure_meter first.

        The counter needs a few seconds to settle after it is enabled: it reads
        0 or null for roughly the first three seconds even on a live signal,
        and only then starts returning the frequency. Wait about three seconds
        after enabling before trusting the reading, and treat a null or 0 right
        after configure_meter as "not settled yet" rather than as no signal.
        """
        return service.get_meter_value(manager.require_scope(), kind)

    @_register
    def configure_histogram(
        enabled: bool | None = None,
        type: str | None = None,
        source: str | None = None,
        height: int | None = None,
        left_s: float | None = None,
        right_s: float | None = None,
        bottom_v: float | None = None,
        top_v: float | None = None,
        reset: bool | None = None,
    ) -> dict:
        """Configure the waveform histogram. Omitted items are left unchanged.

        The histogram is a statistics display the instrument computes from the
        trace it is already acquiring: the acquisition is untouched and no
        output is driven. Read the statistics with get_histogram_result.

        type is horizontal (a histogram over time) or vertical (over voltage).
        source is an analog channel "CH1"-"CH4". height is the display height
        in divisions (1-4).

        left_s and right_s bound the histogram window in seconds, bottom_v and
        top_v in volts. left_s must be smaller than right_s, and bottom_v
        smaller than top_v. That is checked host-side only when both bounds of
        a pair are given in the same call; moving one bound alone past the
        current opposite bound is rejected by the instrument as an error, and
        the remedy is to send both bounds of the pair in one call.

        reset=true restarts the statistics. It is sent after the settings, so a
        single call can change the source and start collecting again.

        Specify at least one item to change. The device may snap values, so
        trust applied (the read-back value), not requested.
        """
        return control.configure_histogram(
            manager.require_scope(),
            enabled=enabled,
            type=type,
            source=source,
            height=height,
            left_s=left_s,
            right_s=right_s,
            bottom_v=bottom_v,
            top_v=top_v,
            reset=reset,
        )

    @_register
    def get_histogram_result() -> dict:
        """Read the histogram statistics.

        raw is always present: the response line exactly as the instrument sent
        it, e.g. "[Sum:30.37khits, Max:1.562V, Min:-999.9mV, ...]". stats holds
        the same values parsed, keyed by the instrument's own labels in
        snake_case: sum, peaks, max, min, pk_pk, mean, median, mode, bin_width,
        sigma, mean_plus_sigma, mean_plus2_sigma, mean_plus3_sigma. Every value
        is a number in base units - SI prefixes are already applied, so
        "30.37khits" comes back as 30370.0 - and the unit of a value that has
        one is in the matching <key>_unit key ("hits", "V"); the sigma-multiple
        values are unitless and have no _unit key. warnings says so when part
        of the response could not be interpreted, and when the histogram is
        disabled: nothing is read in that case and raw comes back empty.

        Enable the histogram with configure_histogram first, and stop the
        acquisition (stop) before reading if you need a stable snapshot.
        """
        return manager.require_scope().get_histogram_result()

    # -- リファレンス波形(tools.md、Phase M3)------------------------------

    @_register
    def configure_reference(
        ref: int = 1,
        source: str | None = None,
        scale: float | None = None,
        offset_v: float | None = None,
        color: str | None = None,
        label: str | None = None,
        label_display: bool | None = None,
        save: bool | None = None,
        reset: bool | None = None,
    ) -> dict:
        """Configure a reference waveform slot. Omitted items are left unchanged.

        A reference waveform is a copy of a trace stored inside the instrument
        and drawn over the live one, so a signal can be compared against a
        known-good capture. This only changes what the instrument displays and
        computes: the acquisition is untouched and no output is driven. ref is
        the slot 1-10 (see get_capabilities ref_channels). Read the result back
        with get_reference_state.

        source is what the slot shows and saves: "CH1"-"CH4", a math trace
        "MATH1"-"MATH4", or a digital channel "D0"-"D15". The programming
        guide says only a channel that is currently displayed may be selected,
        but this firmware accepts a channel whose display is off as well
        (measured), so the source is not restricted here.

        scale is the vertical scale per division and offset_v the vertical
        offset in volts, both of the stored trace. color is gray / green /
        blue / red / orange. label is the text drawn next to the trace
        (letters, digits, '_', '.', '+' and '-'; no spaces).

        label_display turns the labels on or off for EVERY reference waveform
        at once - it is a single global switch on the instrument, not a
        per-slot setting, so it is reported identically for every slot.

        save=true stores the current waveform of the source into this slot. It
        is sent last, after the settings in the same call, so the source is
        already selected. IT IS IRREVERSIBLE: whatever that slot held before is
        overwritten and lost, there is no undo, and there is no way to check
        beforehand whether the slot already holds a capture. Ask the human user
        before overwriting a slot they may still need.

        reset=true restores the slot's default vertical scale and offset. It is
        sent first, before the settings in the same call, so scale and offset_v
        given together with it survive. It does not erase a stored waveform.

        Specify at least one item to change. The device may snap values, so
        trust applied (the read-back value), not requested.

        Reference waveforms cannot be downloaded: :WAVeform:SOURce does not
        accept them. To compare numerically on the host, subtract with
        configure_math(operator="subtract", source1="CH1", source2="REF1") and
        fetch the result with capture_waveform(channel="MATH1").
        """
        return control.configure_reference(
            manager.require_scope(),
            ref,
            source=source,
            scale=scale,
            offset_v=offset_v,
            color=color,
            label=label,
            label_display=label_display,
            save=save,
            reset=reset,
        )

    @_register
    def get_reference_state(ref: int | None = None) -> dict:
        """Return the reference waveform settings (source, scale, offset, color, label).

        With ref given (1-10), that slot's settings are returned flat. With ref
        omitted, every slot is returned under channels, keyed by the slot
        number as a string: {"channels": {"1": {...}, ..., "10": {...}}}.

        label_display is the instrument's single global label switch, so it has
        the same value in every slot. Whether a slot actually holds a stored
        waveform cannot be read: the instrument has no query for it.

        This is read-only and never changes the display. It costs six queries
        per slot.
        """
        driver = manager.require_scope()
        if ref is not None:
            return driver.get_reference_config(ref)
        # 非対応機では ref_channels が 0。1枠だけ問い合わせて
        # UNSUPPORTED_FEATURE を返させる(空dictを「正常」に見せない)
        count = driver.ref_channels or 1
        return {
            "channels": {
                str(n): driver.get_reference_config(n) for n in range(1, count + 1)
            }
        }

    # -- Acquisition(tools.md 4章)-----------------------------------------

    @_register
    def run() -> dict:
        """Start waveform acquisition (continuous run)."""
        return control.run(manager.require_scope())

    @_register
    def stop() -> dict:
        """Stop waveform acquisition (freezes the waveform on screen)."""
        return control.stop(manager.require_scope())

    @_register
    def single() -> dict:
        """Perform a single-shot acquisition (triggers once, then stops)."""
        return control.single(manager.require_scope())

    @_register
    def autoset(confirm_token: str | None = None) -> dict:
        """Run Auto Setup (autoscale).

        This changes the current settings substantially (vertical scale,
        timebase and trigger are auto-adjusted and the previous settings are
        lost), so it needs the confirmation flow: the first call does not
        execute and returns a confirm_token, so ask the human user whether to
        proceed and then call again with that confirm_token.
        After execution the changed main settings are returned in state.
        """
        return control.autoset(
            manager.require_scope(), manager.generation, confirm_token
        )

    return server


# --------------------------------------------------------------------------
# エントリポイント
# --------------------------------------------------------------------------


def main() -> int:
    """stdioでサーバーを起動する。設定エラーは stderr へ出して終了する。"""
    try:
        server = create_server()
    except ScopeError as exc:
        print(json.dumps(exc.to_dict(), ensure_ascii=False), file=sys.stderr)
        return 1
    server.run()
    return 0
