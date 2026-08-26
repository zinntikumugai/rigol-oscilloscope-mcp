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
    def measure(channel: str, measurements: list[str]) -> dict:
        """Measure the given channel.

        Choose measurements from frequency / period / vpp / vmax / vmin / vavg /
        rms / duty / rise_time / fall_time. Returned values use SI-suffixed keys
        (frequency_hz, vpp_v, ...); do not trust a value whose quality is not valid.
        """
        return service.measure(manager.require_scope(), channel, measurements)

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

        When there are many points the data is written to a CSV file and its
        path is returned in data_file. Screen data may be decimated, so read the
        effective sample rate as the reciprocal of sample_interval_s.
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
        - parallel: clk_source, clk_slope, bus_width, endian, polarity.
          Example: {"clk_source": "CH1", "bus_width": 8, "endian": "msb"}

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
