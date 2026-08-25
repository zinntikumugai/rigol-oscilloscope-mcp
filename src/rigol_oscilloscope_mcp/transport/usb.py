"""USBトランスポート(PyVISA / USBTMC)。

USBTMCはメッセージ境界を持つため、LANのraw socketと違い「1回のread_rawで
1メッセージが揃う」ことを前提にできる。よってデッドライン管理は不要で、
タイムアウトはPyVISAの `instrument.timeout`(ミリ秒)に委ねる。

pyvisa は **メソッド内で遅延import** する。未導入の環境でもこのモジュール自体は
importできる(ScopeError(UNSUPPORTED_FEATURE) を返すのは `open()` 時)。
例外クラスやエラーコードも pyvisa モジュール経由で参照するため、テストでは
`sys.modules` へフェイクを注入するだけで差し替えられる。
"""

from __future__ import annotations

import contextlib
from collections.abc import Iterator
from typing import Any

from ..errors import ErrorCode, ScopeError
from .blocks import parse_block

# pyvisa-py(純Python)バックエンド。NI-VISA等の外部ドライバを要求しない。
BACKEND = "@py"

# VI_ERROR_TMO。pyvisa側から取れなかった場合のフォールバック値。
_VI_ERROR_TMO = -1073807339

PYVISA_MISSING_MESSAGE = (
    "PyVISAが利用できません。USB接続には pyvisa と pyvisa-py が必要です"
    "(LAN接続は追加依存なしで利用できます)。"
)


class _NeverRaised(Exception):
    """open前の except 節に置くプレースホルダ(送出されない)。"""


def _import_pyvisa() -> Any:
    """pyvisa を遅延importする。未導入なら UNSUPPORTED_FEATURE。"""
    try:
        import pyvisa  # noqa: PLC0415
        import pyvisa.constants  # noqa: F401, PLC0415
        import pyvisa.errors  # noqa: F401, PLC0415
    except ImportError as exc:
        raise ScopeError(
            ErrorCode.UNSUPPORTED_FEATURE,
            PYVISA_MISSING_MESSAGE,
            {"dependency": "pyvisa"},
        ) from exc
    return pyvisa


def _timeout_codes(pyvisa: Any) -> frozenset[int]:
    """タイムアウトを表す VISA ステータスコードの集合。"""
    codes = {_VI_ERROR_TMO}
    constants = getattr(pyvisa, "constants", None)
    for value in (
        getattr(constants, "VI_ERROR_TMO", None),
        getattr(getattr(constants, "StatusCode", None), "error_timeout", None),
    ):
        if isinstance(value, int):
            codes.add(int(value))
    return frozenset(codes)


class UsbTransport:
    """PyVISA(USBTMC)によるSCPIトランスポート。"""

    def __init__(self, resource: str, timeout_s: float = 5.0) -> None:
        """`resource` はVISAリソース文字列(例 `USB0::0x1AB1::0x0515::SN::INSTR`)。"""
        self.resource = resource
        self.timeout_s = timeout_s
        self._rm: Any | None = None
        self._instrument: Any | None = None
        # open時に pyvisa から取得する(フェイク注入にも追随するため)
        self._visa_error: type[BaseException] = _NeverRaised
        self._timeout_codes: frozenset[int] = frozenset({_VI_ERROR_TMO})

    # --- 接続管理 ---------------------------------------------------------

    def open(self) -> None:
        """リソースを開く。開いていれば何もしない。"""
        if self._instrument is not None:
            return

        pyvisa = _import_pyvisa()
        self._visa_error = pyvisa.errors.VisaIOError
        self._timeout_codes = _timeout_codes(pyvisa)

        rm: Any | None = None
        try:
            rm = pyvisa.ResourceManager(BACKEND)
            instrument = rm.open_resource(self.resource)
            instrument.timeout = self.timeout_s * 1000  # PyVISAはミリ秒
            instrument.read_termination = "\n"
            instrument.write_termination = "\n"
        except Exception as exc:  # VisaIOError / バックエンド初期化失敗など
            if rm is not None:
                _close_quietly(rm)
            raise ScopeError(
                ErrorCode.DEVICE_NOT_FOUND,
                f"VISAリソースを開けません: {self.resource} ({exc})",
                {"resource": self.resource},
            ) from exc

        self._rm = rm
        self._instrument = instrument

    def close(self) -> None:
        """切断する(冪等)。"""
        instrument, self._instrument = self._instrument, None
        rm, self._rm = self._rm, None
        for closable in (instrument, rm):
            if closable is not None:
                _close_quietly(closable)

    @property
    def is_open(self) -> bool:
        return self._instrument is not None

    # --- SCPI操作 ---------------------------------------------------------

    def write(self, command: str) -> None:
        instrument = self._require_open(command)
        try:
            instrument.write(command)  # 終端はwrite_terminationが付与する
        except self._visa_error as exc:
            raise self._failure(command, exc) from exc

    def query(self, command: str, timeout_s: float | None = None) -> str:
        instrument = self._require_open(command)
        with self._timeout_override(instrument, timeout_s):
            try:
                instrument.write(command)
                response = instrument.read()
            except self._visa_error as exc:
                raise self._failure(command, exc) from exc
        return response.rstrip("\r\n")

    def query_binary(self, command: str, timeout_s: float | None = None) -> bytes:
        instrument = self._require_open(command)
        with self._timeout_override(instrument, timeout_s):
            try:
                instrument.write(command)
                raw = self._read_raw(instrument)
            except self._visa_error as exc:
                raise self._failure(command, exc) from exc
        return _parse_block_message(raw, command)

    # --- 内部 -------------------------------------------------------------

    @staticmethod
    def _read_raw(instrument: Any) -> bytes:
        """read_termination を無効化して1メッセージを生バイトで読む。

        バイナリブロックには終端文字と同じバイトが現れるため、
        read_raw の前に終端判定を止めるのがPyVISAでの定石。
        """
        previous = instrument.read_termination
        instrument.read_termination = None
        try:
            return instrument.read_raw()
        finally:
            instrument.read_termination = previous

    @contextlib.contextmanager
    def _timeout_override(
        self, instrument: Any, timeout_s: float | None
    ) -> Iterator[None]:
        """この操作の間だけ instrument.timeout を差し替える。"""
        if timeout_s is None:
            yield
            return
        previous = instrument.timeout
        instrument.timeout = timeout_s * 1000
        try:
            yield
        finally:
            # 失敗経路では `_failure` が既にセッションを閉じている。閉じた
            # セッションへの書き戻しは pyvisa の InvalidSession を招き、
            # 意図した ScopeError を生のpyvisa例外で隠蔽してしまう。
            # よって復元はbest-effort(未closeのときだけ、失敗は無視)。
            if self._instrument is not None:
                with contextlib.suppress(Exception):
                    instrument.timeout = previous

    def _require_open(self, command: str) -> Any:
        if self._instrument is None:
            raise ScopeError(
                ErrorCode.DEVICE_DISCONNECTED,
                "接続されていません(open() が必要です)",
                {"command": command, "resource": self.resource},
            )
        return self._instrument

    def _failure(self, command: str, exc: BaseException) -> ScopeError:
        """VisaIOError を ScopeError へ変換する(いずれの場合も接続を破棄する)。"""
        if self._is_timeout(exc):
            return self._timeout(command)
        # タイムアウト以外のVISAエラーは接続断とみなし、内部状態も閉じる。
        self.close()
        return ScopeError(
            ErrorCode.DEVICE_DISCONNECTED,
            f"USB接続が切断されました({exc}): {command}",
            {"command": command, "resource": self.resource},
        )

    def _timeout(self, command: str) -> ScopeError:
        """タイムアウト。セッションを破棄する(LanTransportと同趣旨)。

        機器が遅延して応答を返すと(実機MHO98では負荷時に0.9〜3.0秒の遅延応答を
        実測)、次のqueryが前問の応答を自分の応答として読むdesyncが起きる。
        USBTMCもメッセージがデバイス側に残るため事情は同じで、セッションごと
        捨てるのが確実。次回操作は `ConnectionManager.require_scope()` の
        自動再接続(=接続時のdrain)からやり直せる。
        """
        self.close()
        return ScopeError(
            ErrorCode.TIMEOUT,
            f"応答がタイムアウトしました: {command}",
            {"command": command, "resource": self.resource},
        )

    def _is_timeout(self, exc: BaseException) -> bool:
        code = getattr(exc, "error_code", None)
        return isinstance(code, int) and int(code) in self._timeout_codes


def _close_quietly(closable: Any) -> None:
    """close時の失敗は無視する(既に切断済みでも冪等に閉じるため)。"""
    try:
        closable.close()
    except Exception:  # noqa: BLE001 - 切断済み/二重closeを許容する
        pass


def _parse_block_message(raw: bytes, command: str) -> bytes:
    """`#N<len><payload>[\\n]` の1メッセージから payload を取り出す。"""
    position = 0

    def reader(n: int) -> bytes:
        nonlocal position
        chunk = raw[position : position + n]
        position += len(chunk)
        return chunk

    payload = parse_block(reader)
    trailing = raw[position:]
    if trailing not in (b"", b"\n"):
        raise ScopeError(
            ErrorCode.WAVEFORM_TRANSFER_FAILED,
            f"バイナリブロックの後ろに余剰バイトがあります: {trailing!r}",
            {"command": command, "trailing": trailing.decode("latin-1")},
        )
    return payload
