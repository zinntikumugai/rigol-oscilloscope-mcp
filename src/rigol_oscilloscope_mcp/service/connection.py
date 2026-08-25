"""接続ライフサイクルの管理(Requirements.md 4.4)。

接続シーケンス: トランスポートopen → エラーキューdrain → `*IDN?` →
プロファイル解決 → 識別情報返却。

単一アクティブ接続とし、再 `connect` は既存接続を置換する。接続先は
**会話でのユーザー指示が基本**で、設定のデフォルトは任意のフォールバック。
どちらも無い場合は、ユーザーへ接続先を確認するようLLMを誘導するエラーを返す。
"""

from __future__ import annotations

import logging
import threading
from collections.abc import Callable
from dataclasses import asdict, dataclass

from ..config import Config
from ..driver.scope import ScopeDriver
from ..driver.session import ScpiSession
from ..errors import ErrorCode, ScopeError
from ..models import IdnInfo
from ..profiles import Profile, resolve_profile
from ..safety import AuditLogger, AuditScope
from ..transport import LanTransport, Transport
from ..transport.lan import DEFAULT_PORT

logger = logging.getLogger(__name__)

TransportFactory = Callable[[str, str, int, float], Transport]

# `*IDN?` を読むまで機種は分からないため、識別専用の最小プロファイルを使う。
# この段階の操作は `*IDN?` のみで、プロファイル依存の分岐は通らない。
_BOOTSTRAP_PROFILE = Profile(name="unknown", confidence="generic")

# VISAリソース文字列(`USB0::0x1AB1::...::INSTR`)の目印
VISA_SEPARATOR = "::"

NO_ADDRESS_MESSAGE = (
    "接続先が未指定です。オシロスコープのIPアドレス"
    "(またはVISAリソース)をユーザーに確認してください。"
)
DISCONNECTED_MESSAGE = (
    "未接続です。connect Toolで接続してください(接続先が不明ならユーザーに確認)。"
)


@dataclass(frozen=True)
class ConnectionStatus:
    """接続状態のスナップショット。未接続でもエラーにせずこれを返す。"""

    connected: bool
    address: str | None
    transport: str | None
    port: int | None
    idn: IdnInfo | None
    profile_name: str | None
    profile_confidence: str | None
    unsupported_vendor: bool


DISCONNECTED_STATUS = ConnectionStatus(
    connected=False,
    address=None,
    transport=None,
    port=None,
    idn=None,
    profile_name=None,
    profile_confidence=None,
    unsupported_vendor=False,
)


def _default_transport_factory(
    transport: str, address: str, port: int, timeout_s: float
) -> Transport:
    """既定のトランスポート生成。USBは遅延importでpyvisaのロードを接続時まで遅らせる。"""
    if transport == "lan":
        return LanTransport(address, port, timeout_s)
    if transport == "usb":
        # pyvisa自体はモジュール内で遅延importしており、欠けていれば
        # UsbTransport.open() が UNSUPPORTED_FEATURE を報告する。
        from ..transport.usb import UsbTransport  # noqa: PLC0415

        return UsbTransport(address, timeout_s)
    raise ScopeError(
        ErrorCode.INVALID_PARAMETER,
        f"未知のトランスポートです: {transport!r}",
        {"transport": transport},
    )


def _infer_transport(address: str) -> str:
    """VISAリソース文字列ならUSB、それ以外はLANとみなす。"""
    return "usb" if VISA_SEPARATOR in address else "lan"


class ConnectionManager:
    """単一のアクティブ接続を保持し、その生成・破棄・再確立を担う。"""

    def __init__(
        self,
        config: Config,
        transport_factory: TransportFactory | None = None,
        audit: AuditLogger | None = None,
    ) -> None:
        self.config = config
        self._transport_factory = (
            transport_factory if transport_factory is not None else _default_transport_factory
        )
        # 接続・切断も監査対象(Requirements.md 7.6)。未注入なら no-op ロガー。
        self._audit = audit if audit is not None else AuditLogger(None)
        # server層が全Tool呼び出しを直列化するための公開ロック(Requirements 6.5)
        self.lock = threading.RLock()
        # confirmトークンを世代にバインドするための連番。connect成功ごとに+1
        self.generation = 0

        self._transport: Transport | None = None
        self._scope: ScopeDriver | None = None
        self._status: ConnectionStatus = DISCONNECTED_STATUS

    # -- 接続 -------------------------------------------------------------

    def connect(
        self,
        address: str | None = None,
        transport: str | None = None,
        port: int | None = None,
    ) -> ConnectionStatus:
        """接続を確立する(既存接続は置換される)。

        優先順位は Tool引数(=会話でのユーザー指示) > 設定デフォルト。
        """
        resolved_address = address if address is not None else self.config.address
        if not resolved_address:
            raise ScopeError(
                ErrorCode.INVALID_PARAMETER, NO_ADDRESS_MESSAGE, {"missing": "address"}
            )

        resolved_transport = (
            transport
            if transport is not None
            else (self.config.transport or _infer_transport(resolved_address))
        )
        resolved_port = (
            port if port is not None else (self.config.port or DEFAULT_PORT)
        )
        return self._establish(resolved_address, resolved_transport, resolved_port)

    def _establish(
        self, address: str, transport: str, port: int, reconnect: bool = False
    ) -> ConnectionStatus:
        """open → drain → *IDN? → プロファイル解決(Requirements.md 4.4)。

        新接続を完全に確立できた場合にのみ既存接続と差し替える。失敗時は旧接続
        (と generation・status)を無傷のまま残し、開きかけた新接続だけ閉じる。
        成否によらず1行を監査へ残す(`detail.reconnect` で自動再接続を区別する)。
        """
        timeout_s = self.config.timeout_s
        requested = {"address": address, "transport": transport, "port": port}
        with AuditScope(
            self._audit, "connect", requested, {"reconnect": reconnect}
        ) as record:
            link = self._transport_factory(transport, address, port, timeout_s)
            link.open()

            try:
                session = ScpiSession(link, timeout_s)
                session.drain_error_queue()
                idn = ScopeDriver(session, _BOOTSTRAP_PROFILE).identify()
                resolved = resolve_profile(idn)
                scope = ScopeDriver(session, resolved.profile)
            except Exception:
                link.close()
                raise

            previous, self._transport = self._transport, link
            if previous is not None:
                try:
                    previous.close()
                except Exception:  # noqa: BLE001 - 旧接続の後始末失敗で新接続を捨てない
                    pass
            self._scope = scope
            self.generation += 1
            self._status = ConnectionStatus(
                connected=True,
                address=address,
                transport=transport,
                port=port,
                idn=idn,
                profile_name=resolved.profile.name,
                profile_confidence=resolved.profile.confidence,
                unsupported_vendor=resolved.unsupported_vendor,
            )
            logger.info(
                "接続しました: %s (profile=%s/%s)",
                idn.model,
                resolved.profile.name,
                resolved.profile.confidence,
            )
            record.after(
                {
                    "profile_name": resolved.profile.name,
                    "profile_confidence": resolved.profile.confidence,
                    "unsupported_vendor": resolved.unsupported_vendor,
                    "idn": asdict(idn),
                }
            )
        return self._status

    # -- 切断・状態 -------------------------------------------------------

    def disconnect(self) -> None:
        """切断する(冪等)。

        監査に残すのは実際にリンクを閉じたときだけ(冪等呼び出しのノイズを避ける)。
        """
        link, self._transport = self._transport, None
        previous, self._status = self._status, DISCONNECTED_STATUS
        self._scope = None
        if link is None:
            return
        with AuditScope(
            self._audit,
            "disconnect",
            {"address": previous.address, "transport": previous.transport},
        ):
            link.close()
        logger.info("切断しました (%s)", previous.transport)

    def status(self) -> ConnectionStatus:
        """接続状態を返す。未接続はエラーではなく connected=False。"""
        return self._status

    # -- 利用 -------------------------------------------------------------

    def require_scope(self) -> ScopeDriver:
        """操作用のドライバを返す。切断されていれば1度だけ再接続を試みる。"""
        if self._scope is None or self._transport is None:
            raise ScopeError(
                ErrorCode.DEVICE_DISCONNECTED, DISCONNECTED_MESSAGE, {"connected": False}
            )
        if self._transport.is_open:
            return self._scope

        previous = self._status
        try:
            self._establish(
                str(previous.address),
                str(previous.transport),
                int(previous.port or DEFAULT_PORT),
                reconnect=True,
            )
        except ScopeError as exc:
            self.disconnect()
            raise ScopeError(
                ErrorCode.DEVICE_DISCONNECTED,
                f"接続が失われ、再接続にも失敗しました: {exc.message}",
                {"address": previous.address, "cause": exc.to_dict()},
            ) from exc
        if self._scope is None:  # pragma: no cover - _establish 成功時は必ず設定される
            raise ScopeError(
                ErrorCode.DEVICE_DISCONNECTED, DISCONNECTED_MESSAGE, {"connected": False}
            )
        return self._scope
