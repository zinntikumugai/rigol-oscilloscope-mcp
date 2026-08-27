"""transport/usb.py(PyVISA/USBTMC)のテスト。

実USB機器も実PyVISAドライバも使わない。`sys.modules` へフェイクの `pyvisa`
モジュール(ResourceManager / instrumentスタブ / errors.VisaIOError)を注入し、
実装が「pyvisaモジュール経由でシンボルを参照する」ことを利用して差し替える。

実機での疎通確認は本テストの対象外(device マーカー付きの実機テストで行う)。
"""

from __future__ import annotations

import sys
from dataclasses import dataclass, field
from types import ModuleType
from typing import Any

import pytest

from rigol_oscilloscope_mcp.config import Config
from rigol_oscilloscope_mcp.errors import ErrorCode, ScopeError
from rigol_oscilloscope_mcp.service.connection import (
    ConnectionManager,
    _default_transport_factory,
)
from rigol_oscilloscope_mcp.testing import FakeScope, FakeTransport
from rigol_oscilloscope_mcp.transport import Transport, UsbTransport
from rigol_oscilloscope_mcp.transport import usb as usb_module

RESOURCE = "USB0::0x1AB1::0x0515::MHO9XXXXXXXXX::INSTR"

# 実 pyvisa の pyvisa.constants.VI_ERROR_TMO と同値
VI_ERROR_TMO = -1073807339
VI_ERROR_CONN_LOST = -1073807194


# --- フェイク pyvisa --------------------------------------------------------


class FakeVisaIOError(Exception):
    """`pyvisa.errors.VisaIOError` の最小再現(error_code を持つ例外)。"""

    def __init__(self, error_code: int) -> None:
        super().__init__(f"VI_ERROR {error_code}")
        self.error_code = error_code


class FakeInstrument:
    """`rm.open_resource()` が返す計器スタブ。"""

    def __init__(self) -> None:
        self.timeout: float | None = None
        self.read_termination: str | None = "SENTINEL"
        self.write_termination: str | None = "SENTINEL"
        self.written: list[str] = []
        self.responses: list[str] = []
        self.raw_responses: list[bytes] = []
        self.write_error: Exception | None = None
        self.read_error: Exception | None = None
        self.closed = False
        # 読み出しの瞬間に見えていた設定値(一時変更の検証用)
        self.timeout_at_read: list[float | None] = []
        self.read_termination_at_read: list[str | None] = []

    def write(self, command: str) -> int:
        if self.write_error is not None:
            raise self.write_error
        self.written.append(command)
        return len(command)

    def _snapshot(self) -> None:
        self.timeout_at_read.append(self.timeout)
        self.read_termination_at_read.append(self.read_termination)

    def read(self) -> str:
        self._snapshot()
        if self.read_error is not None:
            raise self.read_error
        return self.responses.pop(0)

    def read_raw(self) -> bytes:
        self._snapshot()
        if self.read_error is not None:
            raise self.read_error
        return self.raw_responses.pop(0)

    def close(self) -> None:
        self.closed = True


class FakeInvalidSession(Exception):
    """`pyvisa.errors.InvalidSession` 相当(VisaIOErrorではない別系統の例外)。"""


class StrictSessionInstrument(FakeInstrument):
    """close後に属性を書き戻すと InvalidSession を投げる計器スタブ。

    実 pyvisa は close 済みセッションへの `instrument.timeout = ...` で
    InvalidSession を上げる。意図した ScopeError がこれに隠蔽されないことを検証する。
    """

    closed = False  # 基底の __init__ が self.timeout を触る時点で参照される

    @property
    def timeout(self) -> float | None:
        return self._timeout

    @timeout.setter
    def timeout(self, value: float | None) -> None:
        if self.closed:
            raise FakeInvalidSession("session is closed")
        self._timeout = value


class FakeResourceManager:
    """`pyvisa.ResourceManager` の代役。呼び出し可能かつインスタンスを兼ねる。"""

    def __init__(
        self,
        instrument: FakeInstrument,
        construct_error: Exception | None = None,
        open_error: Exception | None = None,
    ) -> None:
        self.instrument = instrument
        self.construct_error = construct_error
        self.open_error = open_error
        self.backends: list[str] = []
        self.opened: list[str] = []
        self.closed = False

    def __call__(self, backend: str = "") -> FakeResourceManager:
        if self.construct_error is not None:
            raise self.construct_error
        self.backends.append(backend)
        return self

    def open_resource(self, resource: str, **kwargs: Any) -> FakeInstrument:
        self.opened.append(resource)
        if self.open_error is not None:
            raise self.open_error
        return self.instrument

    def close(self) -> None:
        self.closed = True


@dataclass
class FakeVisa:
    """テストから触るフェイク一式。"""

    module: ModuleType
    rm: FakeResourceManager
    instrument: FakeInstrument
    submodules: dict[str, ModuleType] = field(default_factory=dict)


def _install_pyvisa(
    monkeypatch: pytest.MonkeyPatch, rm: FakeResourceManager
) -> ModuleType:
    """フェイク pyvisa を sys.modules に注入する。"""
    pyvisa = ModuleType("pyvisa")
    errors = ModuleType("pyvisa.errors")
    constants = ModuleType("pyvisa.constants")

    errors.VisaIOError = FakeVisaIOError  # type: ignore[attr-defined]
    constants.VI_ERROR_TMO = VI_ERROR_TMO  # type: ignore[attr-defined]
    pyvisa.errors = errors  # type: ignore[attr-defined]
    pyvisa.constants = constants  # type: ignore[attr-defined]
    pyvisa.ResourceManager = rm  # type: ignore[attr-defined]
    pyvisa.VisaIOError = FakeVisaIOError  # type: ignore[attr-defined]

    monkeypatch.setitem(sys.modules, "pyvisa", pyvisa)
    monkeypatch.setitem(sys.modules, "pyvisa.errors", errors)
    monkeypatch.setitem(sys.modules, "pyvisa.constants", constants)
    return pyvisa


@pytest.fixture
def visa(monkeypatch: pytest.MonkeyPatch) -> FakeVisa:
    instrument = FakeInstrument()
    rm = FakeResourceManager(instrument)
    module = _install_pyvisa(monkeypatch, rm)
    return FakeVisa(module=module, rm=rm, instrument=instrument)


def opened(visa: FakeVisa, timeout_s: float = 5.0) -> UsbTransport:
    link = UsbTransport(RESOURCE, timeout_s)
    link.open()
    return link


# --------------------------------------------------------------------------
# open / close
# --------------------------------------------------------------------------


def test_open_configures_instrument(visa: FakeVisa) -> None:
    link = opened(visa)

    assert link.is_open is True
    assert visa.rm.backends == ["@py"]  # pyvisa-py バックエンド
    assert visa.rm.opened == [RESOURCE]
    assert visa.instrument.timeout == 5000  # ms
    assert visa.instrument.read_termination == "\n"
    assert visa.instrument.write_termination == "\n"


def test_open_uses_configured_timeout(visa: FakeVisa) -> None:
    opened(visa, timeout_s=2.5)

    assert visa.instrument.timeout == 2500


def test_open_is_idempotent(visa: FakeVisa) -> None:
    link = opened(visa)
    link.open()

    assert visa.rm.opened == [RESOURCE]


def test_open_failure_maps_to_device_not_found(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    instrument = FakeInstrument()
    rm = FakeResourceManager(instrument, construct_error=RuntimeError("no backend"))
    _install_pyvisa(monkeypatch, rm)
    link = UsbTransport(RESOURCE)

    with pytest.raises(ScopeError) as excinfo:
        link.open()

    assert excinfo.value.code == ErrorCode.DEVICE_NOT_FOUND
    assert excinfo.value.detail["resource"] == RESOURCE
    assert link.is_open is False


def test_open_resource_failure_maps_to_device_not_found(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    instrument = FakeInstrument()
    rm = FakeResourceManager(instrument, open_error=FakeVisaIOError(VI_ERROR_TMO))
    _install_pyvisa(monkeypatch, rm)
    link = UsbTransport(RESOURCE)

    with pytest.raises(ScopeError) as excinfo:
        link.open()

    # open時はタイムアウトコードでも「見つからない」として扱う
    assert excinfo.value.code == ErrorCode.DEVICE_NOT_FOUND
    assert excinfo.value.detail["resource"] == RESOURCE
    assert rm.closed is True  # 掴んだResourceManagerは解放する


def test_missing_pyvisa_maps_to_unsupported_feature(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setitem(sys.modules, "pyvisa", None)
    link = UsbTransport(RESOURCE)

    with pytest.raises(ScopeError) as excinfo:
        link.open()

    assert excinfo.value.code == ErrorCode.UNSUPPORTED_FEATURE
    assert "PyVISA" in excinfo.value.message
    assert link.is_open is False


def test_close_is_idempotent(visa: FakeVisa) -> None:
    link = opened(visa)

    link.close()
    link.close()

    assert link.is_open is False
    assert visa.instrument.closed is True
    assert visa.rm.closed is True


def test_close_before_open_is_noop() -> None:
    UsbTransport(RESOURCE).close()  # 例外が出ないこと


def test_satisfies_transport_protocol() -> None:
    assert isinstance(UsbTransport(RESOURCE), Transport)


# --------------------------------------------------------------------------
# write / query
# --------------------------------------------------------------------------


def test_write_sends_command(visa: FakeVisa) -> None:
    link = opened(visa)

    link.write(":RUN")

    # 終端はwrite_terminationでpyvisaが付与するため、実装は付けない
    assert visa.instrument.written == [":RUN"]


def test_query_writes_and_strips_trailing_newline(visa: FakeVisa) -> None:
    link = opened(visa)
    visa.instrument.responses.append("RIGOL TECHNOLOGIES,MHO98,X,00.01\n")

    answer = link.query("*IDN?")

    assert visa.instrument.written == ["*IDN?"]
    assert answer == "RIGOL TECHNOLOGIES,MHO98,X,00.01"


def test_query_timeout_maps_to_timeout_error(visa: FakeVisa) -> None:
    link = opened(visa)
    visa.instrument.read_error = FakeVisaIOError(VI_ERROR_TMO)

    with pytest.raises(ScopeError) as excinfo:
        link.query(":MEAS:VPP? CHAN1")

    assert excinfo.value.code == ErrorCode.TIMEOUT
    assert excinfo.value.detail["command"] == ":MEAS:VPP? CHAN1"
    # 遅延応答によるdesyncを防ぐため、タイムアウトでも接続を破棄する
    assert link.is_open is False
    assert visa.instrument.closed is True


def test_query_after_timeout_requires_reconnect(visa: FakeVisa) -> None:
    """タイムアウト後は同一transportで続けられない(遅延応答を読まない)。"""
    link = opened(visa)
    visa.instrument.read_error = FakeVisaIOError(VI_ERROR_TMO)
    with pytest.raises(ScopeError):
        link.query(":MEAS:VPP? CHAN1")

    visa.instrument.read_error = None
    visa.instrument.responses.append("1.234\n")  # 前問への遅延応答

    with pytest.raises(ScopeError) as excinfo:
        link.query("*IDN?")

    assert excinfo.value.code == ErrorCode.DEVICE_DISCONNECTED
    assert visa.instrument.responses == ["1.234\n"]  # 読んでいない


def test_query_other_visa_error_maps_to_disconnected(visa: FakeVisa) -> None:
    link = opened(visa)
    visa.instrument.read_error = FakeVisaIOError(VI_ERROR_CONN_LOST)

    with pytest.raises(ScopeError) as excinfo:
        link.query("*IDN?")

    assert excinfo.value.code == ErrorCode.DEVICE_DISCONNECTED
    assert excinfo.value.detail["command"] == "*IDN?"
    assert link.is_open is False  # 以後の操作が誤動作しないよう閉じる


def test_write_visa_error_maps_to_disconnected(visa: FakeVisa) -> None:
    link = opened(visa)
    visa.instrument.write_error = FakeVisaIOError(VI_ERROR_CONN_LOST)

    with pytest.raises(ScopeError) as excinfo:
        link.write(":RUN")

    assert excinfo.value.code == ErrorCode.DEVICE_DISCONNECTED


def test_query_temporary_timeout_is_applied_and_restored(visa: FakeVisa) -> None:
    link = opened(visa)
    visa.instrument.responses.append("1\n")

    link.query(":WAV:DATA?", timeout_s=30.0)

    assert visa.instrument.timeout_at_read == [30000]
    assert visa.instrument.timeout == 5000  # finally で復元


def test_query_failure_drops_connection_instead_of_restoring(visa: FakeVisa) -> None:
    """失敗時は接続ごと破棄するので、一時timeoutの復元は行わない。"""
    link = opened(visa)
    visa.instrument.read_error = FakeVisaIOError(VI_ERROR_TMO)

    with pytest.raises(ScopeError):
        link.query(":WAV:DATA?", timeout_s=30.0)

    assert visa.instrument.timeout_at_read == [30000]
    assert link.is_open is False


@pytest.mark.parametrize(
    ("error_code", "expected"),
    [
        (VI_ERROR_TMO, ErrorCode.TIMEOUT),
        (VI_ERROR_CONN_LOST, ErrorCode.DEVICE_DISCONNECTED),
    ],
)
def test_query_failure_is_not_masked_by_timeout_restore(
    monkeypatch: pytest.MonkeyPatch, error_code: int, expected: ErrorCode
) -> None:
    """close後の timeout 書き戻し(InvalidSession)が ScopeError を隠さない。"""
    instrument = StrictSessionInstrument()
    rm = FakeResourceManager(instrument)
    _install_pyvisa(monkeypatch, rm)
    link = UsbTransport(RESOURCE)
    link.open()
    instrument.read_error = FakeVisaIOError(error_code)

    with pytest.raises(ScopeError) as excinfo:
        link.query(":WAV:DATA?", timeout_s=30.0)

    assert excinfo.value.code == expected
    assert link.is_open is False


def test_query_lines_reads_until_the_blank_line(visa: FakeVisa) -> None:
    """複数行応答(FFTピーク表)は終端の空行まで読む(LANと同じ意味論)。"""
    link = opened(visa)
    visa.instrument.responses.extend(
        ["1,9.09061kHz,-1.373dBV", "2,27.0239kHz,-20.45dBV", "", "NEXT"]
    )

    lines = link.query_lines(":MATH1:FFT:SEARch:RES?")

    assert lines == ["1,9.09061kHz,-1.373dBV", "2,27.0239kHz,-20.45dBV"]
    assert visa.instrument.written == [":MATH1:FFT:SEARch:RES?"]
    # 終端の空行までで止まるので、次の応答は残っている
    assert link.query("NEXT?") == "NEXT"


def test_query_lines_empty_table_is_just_a_blank_line(visa: FakeVisa) -> None:
    link = opened(visa)
    visa.instrument.responses.append("")

    assert link.query_lines(":MATH1:FFT:SEARch:RES?") == []


def test_query_lines_timeout_maps_to_timeout_error(visa: FakeVisa) -> None:
    link = opened(visa)
    visa.instrument.read_error = FakeVisaIOError(VI_ERROR_TMO)

    with pytest.raises(ScopeError) as excinfo:
        link.query_lines(":MATH1:FFT:SEARch:RES?")

    assert excinfo.value.code == ErrorCode.TIMEOUT
    assert link.is_open is False


def test_query_lines_without_open_is_disconnected() -> None:
    link = UsbTransport(RESOURCE)

    with pytest.raises(ScopeError) as excinfo:
        link.query_lines(":MATH1:FFT:SEARch:RES?")

    assert excinfo.value.code == ErrorCode.DEVICE_DISCONNECTED


def test_query_without_open_is_disconnected() -> None:
    link = UsbTransport(RESOURCE)

    with pytest.raises(ScopeError) as excinfo:
        link.query("*IDN?")

    assert excinfo.value.code == ErrorCode.DEVICE_DISCONNECTED


def test_write_without_open_is_disconnected() -> None:
    link = UsbTransport(RESOURCE)

    with pytest.raises(ScopeError) as excinfo:
        link.write(":RUN")

    assert excinfo.value.code == ErrorCode.DEVICE_DISCONNECTED


def test_query_binary_without_open_is_disconnected() -> None:
    link = UsbTransport(RESOURCE)

    with pytest.raises(ScopeError) as excinfo:
        link.query_binary(":WAV:DATA?")

    assert excinfo.value.code == ErrorCode.DEVICE_DISCONNECTED


# --------------------------------------------------------------------------
# query_binary
# --------------------------------------------------------------------------


def test_query_binary_returns_block_payload(visa: FakeVisa) -> None:
    link = opened(visa)
    payload = (bytes(range(256)) * 4)[:1000]  # 0x00-0xFF を含む1000バイト
    visa.instrument.raw_responses.append(b"#41000" + payload + b"\n")

    got = link.query_binary(":WAV:DATA?")

    assert visa.instrument.written == [":WAV:DATA?"]
    assert got == payload
    assert len(got) == 1000


def test_query_binary_accepts_block_without_trailing_newline(visa: FakeVisa) -> None:
    link = opened(visa)
    visa.instrument.raw_responses.append(b"#3005" + b"ABCDE")

    assert link.query_binary(":WAV:DATA?") == b"ABCDE"


def test_query_binary_rejects_extra_trailing_bytes(visa: FakeVisa) -> None:
    link = opened(visa)
    visa.instrument.raw_responses.append(b"#3005" + b"ABCDE" + b"XY\n")

    with pytest.raises(ScopeError) as excinfo:
        link.query_binary(":WAV:DATA?")

    assert excinfo.value.code == ErrorCode.WAVEFORM_TRANSFER_FAILED
    assert excinfo.value.detail["command"] == ":WAV:DATA?"


def test_query_binary_rejects_truncated_block(visa: FakeVisa) -> None:
    link = opened(visa)
    visa.instrument.raw_responses.append(b"#41000" + b"short")

    with pytest.raises(ScopeError) as excinfo:
        link.query_binary(":WAV:DATA?")

    assert excinfo.value.code == ErrorCode.WAVEFORM_TRANSFER_FAILED


def test_query_binary_suppresses_read_termination_during_read(visa: FakeVisa) -> None:
    link = opened(visa)
    visa.instrument.raw_responses.append(b"#10" + b"\n")

    link.query_binary(":DISP:DATA?")

    # read_raw中は終端文字で切られないようNoneにし、その後復元する
    assert visa.instrument.read_termination_at_read == [None]
    assert visa.instrument.read_termination == "\n"


def test_query_binary_timeout_maps_to_timeout_error(visa: FakeVisa) -> None:
    link = opened(visa)
    visa.instrument.read_error = FakeVisaIOError(VI_ERROR_TMO)

    with pytest.raises(ScopeError) as excinfo:
        link.query_binary(":DISP:DATA?", timeout_s=20.0)

    assert excinfo.value.code == ErrorCode.TIMEOUT
    assert excinfo.value.detail["command"] == ":DISP:DATA?"
    assert visa.instrument.timeout_at_read == [20000]
    assert visa.instrument.read_termination == "\n"  # read_raw前の値へ復元済み
    assert link.is_open is False  # desync防止のため接続を破棄する
    assert visa.instrument.closed is True


# --------------------------------------------------------------------------
# ConnectionManager の既定ファクトリとの結線
# --------------------------------------------------------------------------


def test_default_factory_builds_usb_transport() -> None:
    link = _default_transport_factory("usb", RESOURCE, 5555, 3.0)

    assert isinstance(link, UsbTransport)
    assert link.resource == RESOURCE
    assert link.timeout_s == 3.0
    assert link.is_open is False  # 生成だけで接続はしない


def test_connection_manager_uses_usb_transport_for_visa_resource(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    created: list[tuple[str, float]] = []

    class RecordingUsbTransport(FakeTransport):
        """実resourceを開かずに__init__引数だけ記録する差し替え。"""

        def __init__(self, resource: str, timeout_s: float = 5.0) -> None:
            super().__init__(FakeScope())
            created.append((resource, timeout_s))

    monkeypatch.setattr(usb_module, "UsbTransport", RecordingUsbTransport)
    manager = ConnectionManager(Config(timeout_s=2.0))

    status = manager.connect(address=RESOURCE)

    assert created == [(RESOURCE, 2.0)]
    assert status.transport == "usb"
    assert status.connected is True
