"""MCPサーバー(Phase 1: Read Only / Phase 2: 書き込み系 Tool群 / tools.md 8章)。

MCP SDK(FastMCP)への依存は本モジュールに閉じ込め、下位層(service / driver)
はSDKを知らないまま保つ。

Tool実装の規約:

- 本体は全て**同期関数**とし、`manager.lock` で機器アクセス全体を囲んで
  SCPI送受信を直列化する(Requirements.md 6.5)
- `ScopeError` はMCPのエラー応答にせず、`{"error": true, "code": ...}` の
  **正常返却**へ変換する。LLMがコードを機械的に読めるようにするため
  (tools.md 0.3)
- 返却は `dict`(JSONプリミティブのみ)。SDKはこれを1つのtext contentとして
  JSON整形する。スクリーンショットのみ `[メタデータdict, Image]` を返し、
  text + image の2 contentになる
"""

from __future__ import annotations

import json
import os
import sys
from collections.abc import Callable, Mapping
from dataclasses import asdict
from functools import wraps
from typing import Any

from mcp.server.fastmcp import FastMCP, Image

from . import service
from .config import Config, load_config
from .errors import ScopeError
from .safety import AuditLogger, ConfirmTokenStore
from .service import ConnectionManager, ConnectionStatus, ControlService
from .service.connection import DISCONNECTED_MESSAGE

SERVER_NAME = "rigol-oscilloscope-mcp"

INSTRUCTIONS = (
    "Rigol製オシロスコープをSCPI経由で操作するサーバーです。"
    "まず connect で接続し(接続先はユーザーに確認)、"
    "操作前に get_state で現在の設定を確認してください。"
    "数値は capture_screenshot の画像ではなく measure の結果を優先します。"
)

# errors.ErrorCode は機器由来のコード集合。想定外の例外はそれと区別する。
INTERNAL_ERROR = "INTERNAL_ERROR"

# 実機なしで手動E2Eを行うためのフラグ(FakeScopeを機器の代わりに使う)
FAKE_ENV_VAR = "RIGOL_MCP_FAKE"
_TRUTHY = frozenset({"1", "true", "yes", "on"})


# --------------------------------------------------------------------------
# 組み立て
# --------------------------------------------------------------------------


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


def _build_manager(config: Config) -> ConnectionManager:
    if _fake_enabled():
        return ConnectionManager(config, transport_factory=_fake_transport_factory())
    return ConnectionManager(config)


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
    """Phase 1 / Phase 2 のToolを登録したサーバーを組み立てる。

    `config` 省略時は環境変数・設定ファイルから解決する。
    `connection_manager` 省略時は生成し、`RIGOL_MCP_FAKE=1` なら実機の代わりに
    FakeScope へ接続する(実機なしでの手動確認用)。
    """
    resolved_config = load_config() if config is None else config
    manager = _build_manager(resolved_config) if connection_manager is None else connection_manager
    # confirmトークンはサーバー(=セッション)寿命で共有する。世代バインドは
    # 呼び出しごとに manager.generation を渡すことで効かせる(Requirements.md 6.2)
    control = ControlService(ConfirmTokenStore(), AuditLogger(resolved_config.audit_log))

    server = FastMCP(SERVER_NAME, instructions=INSTRUCTIONS)
    tool = server.tool

    # -- 接続管理 ---------------------------------------------------------

    @tool()
    @_tool_result
    def connect(
        address: str | None = None,
        transport: str | None = None,
        port: int | None = None,
    ) -> dict:
        """オシロスコープへ接続する。

        接続先はユーザーの指示(IPアドレス等)を address に渡すこと。
        分からなければ推測せずユーザーに確認する。transport は省略時に
        address の形式から推定("lan" / "usb")、port は省略時にプロファイル既定。
        既存の接続がある場合は置き換わる。
        """
        with manager.lock:
            return _status_dict(manager.connect(address=address, transport=transport, port=port))

    @tool()
    @_tool_result
    def disconnect() -> dict:
        """現在の接続を閉じる(未接続でもエラーにならない)。"""
        with manager.lock:
            manager.disconnect()
            return _status_dict(manager.status())

    @tool()
    @_tool_result
    def scope_identify() -> dict:
        """接続状態と機器の識別情報(*IDN?・プロファイル)を返す。

        未接続でもエラーにはならず connected: false を返す。
        """
        with manager.lock:
            return _status_dict(manager.status())

    @tool()
    @_tool_result
    def get_capabilities() -> dict:
        """接続中の機器で利用できる機能(チャンネル数・対応機能)を返す。

        プロファイルの信頼度が generic の場合、未検証の機能は制限される。
        """
        with manager.lock:
            driver = manager.require_scope()
            status = manager.status()
            return {
                "profile": _profile_dict(driver.profile.name, driver.profile.confidence),
                "capabilities": dict(driver.profile.capabilities),
                "unsupported_vendor": status.unsupported_vendor,
            }

    # -- 状態取得 ---------------------------------------------------------

    @tool()
    @_tool_result
    def get_state(sections: list[str] | None = None) -> dict:
        """主要設定(channels / timebase / trigger / acquisition)を一括取得する。

        目的が明確なら sections で絞ると高速(全取得は約39クエリ・数秒かかること
        がある)。省略時は全セクション。
        """
        with manager.lock:
            return service.get_state(manager.require_scope(), sections)

    @tool()
    @_tool_result
    def get_channel(channel: str) -> dict:
        """1チャンネルの状態("CH1"〜"CH4")を返す。"""
        with manager.lock:
            return service.get_channel_dict(manager.require_scope(), channel)

    @tool()
    @_tool_result
    def get_timebase() -> dict:
        """水平軸(時間軸)の状態を返す。"""
        with manager.lock:
            return service.get_timebase_dict(manager.require_scope())

    @tool()
    @_tool_result
    def get_trigger() -> dict:
        """トリガの設定と状態を返す。"""
        with manager.lock:
            return service.get_trigger_dict(manager.require_scope())

    @tool()
    @_tool_result
    def get_acquisition_state() -> dict:
        """波形取り込みの状態(実行中かどうか・トリガ状態)を返す。"""
        with manager.lock:
            return service.get_acquisition_dict(manager.require_scope())

    # -- 測定・データ取得 -------------------------------------------------

    @tool()
    @_tool_result
    def measure(channel: str, measurements: list[str]) -> dict:
        """指定チャンネルを測定する。

        measurements は frequency / period / vpp / vmax / vmin / vavg / rms /
        duty / rise_time / fall_time から選ぶ。返却値はSI単位
        (frequency_hz, vpp_v など)で、quality が valid でない値は信用しない。
        """
        with manager.lock:
            return service.measure(manager.require_scope(), channel, measurements)

    @tool()
    @_tool_result
    def capture_waveform(channel: str, max_points: int | None = None) -> dict:
        """波形データを取得し、電圧(V)へ変換して返す。

        点数が多い場合はCSVファイルへ退避し、そのパスを data_file で返す。
        画面表示データは間引きされていることがあるため、実効レートは
        sample_interval_s の逆数を見る。
        """
        with manager.lock:
            return service.capture_waveform(
                manager.require_scope(), resolved_config, channel, max_points
            )

    @tool()
    @_tool_result
    def capture_screenshot(
        path: str | None = None,
        format: str | None = None,
        return_image: bool = True,
    ) -> Any:
        """画面をキャプチャして保存し、画像も返す(波形の目視確認用)。

        path は保存先のディレクトリまたはファイル(省略時は設定の既定ディレクトリ)。
        format は png / jpg / jpeg / bmp / webp。return_image=false にすると
        画像を返さずメタデータのみになる(トークン節約)。
        数値の読み取りはこの画像ではなく measure を使うこと。
        """
        with manager.lock:
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

    @tool()
    @_tool_result
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
        """垂直軸(チャンネル)を設定する。未指定の項目は変更しない。

        channel は "CH1"〜"CH4"、coupling は DC / AC / GND、
        impedance は "1M" / "50"。変更する項目を最低1つ指定すること。
        機器が値をスナップすることがあるため、結果は requested ではなく
        applied(read-back値)を信頼する。

        impedance="50" は機器破損リスクがあるため確認フローが必要:
        1回目は実行されず confirm_token が返るので、人間の利用者に実行可否を
        確認したうえで、同じ引数に confirm_token を添えて再度呼ぶこと。
        """
        with manager.lock:
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

    @tool()
    @_tool_result
    def configure_timebase(
        scale_s_per_div: float | None = None,
        position_s: float | None = None,
    ) -> dict:
        """水平軸(時間軸)を設定する。未指定の項目は変更しない。

        変更する項目を最低1つ指定すること。機器が値をスナップすることがあるため、
        結果は applied(read-back値)を信頼する。
        """
        with manager.lock:
            return control.configure_timebase(
                manager.require_scope(),
                scale_s_per_div=scale_s_per_div,
                position_s=position_s,
            )

    @tool()
    @_tool_result
    def configure_trigger(
        source: str | None = None,
        level_v: float | None = None,
        slope: str | None = None,
        sweep_mode: str | None = None,
    ) -> dict:
        """エッジトリガを設定する。未指定の項目は変更しない。

        source は "CH1"〜"CH4"、slope は rising / falling / either、
        sweep_mode は auto / normal / single。変更する項目を最低1つ指定すること。
        """
        with manager.lock:
            return control.configure_trigger(
                manager.require_scope(),
                source=source,
                level_v=level_v,
                slope=slope,
                sweep_mode=sweep_mode,
            )

    # -- Acquisition(tools.md 4章)-----------------------------------------

    @tool()
    @_tool_result
    def run() -> dict:
        """波形取り込みを開始する(連続実行)。"""
        with manager.lock:
            return control.run(manager.require_scope())

    @tool()
    @_tool_result
    def stop() -> dict:
        """波形取り込みを停止する(画面の波形を固定する)。"""
        with manager.lock:
            return control.stop(manager.require_scope())

    @tool()
    @_tool_result
    def single() -> dict:
        """シングルショット取り込みを行う(1回トリガして停止する)。"""
        with manager.lock:
            return control.single(manager.require_scope())

    @tool()
    @_tool_result
    def autoset(confirm_token: str | None = None) -> dict:
        """Auto Setup(オートスケール)を実行する。

        現在の設定が大きく変更される(垂直感度・水平時間軸・トリガが自動調整され、
        調整前の設定は失われる)ため確認フローが必要:
        1回目は実行されず confirm_token が返るので、人間の利用者に実行可否を
        確認したうえで confirm_token を添えて再度呼ぶこと。
        実行後は変更された主要設定を state で返す。
        """
        with manager.lock:
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
