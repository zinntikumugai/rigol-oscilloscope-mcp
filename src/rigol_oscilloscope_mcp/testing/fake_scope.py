"""MHO98方言のインプロセス・フェイク機器。

phase0の実機検証(docs/verification/mho98-phase0.md)で観測した応答形式と
癖(quirk)を再現する。特に以下は実測に基づく:

- 不正/未知ニモニックは**無応答**(クライアントがタイムアウト)+ エラーキューに
  `-100,"Command err"` → 本実装では `SilentTimeout` を即時送出する(実スリープはしない)
- `:MEASure:ITEM? VAVerage,...` は受理されず `-222,"Data out of range"` が積まれる
  (`VAVG` は受理される)
- scale は 1-2-5 にスナップ**しない**(`snap_to_125=True` は requested≠applied の
  ストレスケース用であり、実機挙動ではない)
- NR3応答に指数1桁形式(`1.000000E+1`)が混ざる
"""

from __future__ import annotations

import io
import math
import re
from collections import deque
from collections.abc import Callable

from PIL import Image

__all__ = ["FakeScope", "SilentTimeout"]

IDN = "RIGOL TECHNOLOGIES,MHO98,FAKE0000000001,00.01.00"

NO_ERROR = '0,"No error"'
COMMAND_ERROR = '-100,"Command err"'
OUT_OF_RANGE = '-222,"Data out of range"'

# phase0実測のプリアンブル。yorigin(第9要素)だけは定数ではなく、
# チャンネルoffsetの生カウント換算(= offset / yincrement)で動的に決まる。
# 実機MHO98でも offset -0.064 V のとき yorigin=-9.0 を観測している。
PREAMBLE_HEAD = "0,0,1000,1,2.000000E-6,-1.000000E-3,0.000000,6.8267E-02"
YINCREMENT = 6.8267e-02
YREFERENCE = 128

WAVEFORM_POINTS = 1000
RAW_MIN = 127  # offset=0(yorigin=0)で volts = (127-0-128)*6.8267e-2 = -0.068 V
RAW_MAX = 174  # offset=0(yorigin=0)で volts = (174-0-128)*6.8267e-2 =  3.140 V

SRATE = "5.0000E+06"
MDEPTH = "1.0000E+04"

SCREENSHOT_SIZE = (32, 24)


class SilentTimeout(Exception):
    """機器が無応答であることを表す。

    実機は不正ニモニックに対し応答を返さず、クライアント側がタイムアウトする。
    フェイクでは待たずに即座にこれを送出する(トランスポートがTIMEOUTへ変換する)。
    """


# ---------------------------------------------------------------------------
# SCPIニモニックのユーティリティ
# ---------------------------------------------------------------------------


def _forms(spec: str) -> tuple[str, str]:
    """`'CHANnel'` → `('CHAN', 'CHANNEL')`(短形式, 長形式)。"""
    short = "".join(c for c in spec if not c.islower()).upper()
    return short, spec.upper()


def _mn(spec: str) -> str:
    """ニモニック仕様から短形式/長形式の双方を受理する正規表現断片を作る。"""
    short, long = _forms(spec)
    if short == long:
        return re.escape(short)
    return re.escape(short) + f"(?:{re.escape(long[len(short) :])})?"


def _mn_indexed(spec: str) -> str:
    """末尾の番号をニモニック本体から切り離して正規表現断片を作る。

    `SOURce1` をそのまま `_mn` に渡すと短形が `SOUR1` / 長形が `SOURCE1` となり、
    共通接頭辞が取れずに壊れたパターン(`SOUR1(?:E1)?`)になる。番号を外して
    `SOUR(?:CE)?1` を組み立てる(`LSOurce1` → `LSO(?:URCE)?1` も同様)。
    """
    body = spec.rstrip("0123456789")
    return _mn(body) + spec[len(body) :]


def _normalize(token: str, specs: tuple[str, ...]) -> str | None:
    """列挙値トークンを短形式へ正規化する。未知なら None。"""
    text = token.strip().upper()
    for spec in specs:
        short, long = _forms(spec)
        if text in (short, long):
            return short
    return None


def _nr3(value: float) -> str:
    """標準的なNR3表記(指数2桁): `3.000000E+00`。"""
    return f"{value:.6E}"


def _nr3_single_digit_exponent(value: float) -> str:
    """phase0で観測した指数1桁のNR3表記: `1.000000E+1`。"""
    return re.sub(r"E([+-])0*(\d)$", r"E\1\2", f"{value:.6E}")


def _snap_125(value: float) -> float:
    """1-2-5系列へ切り下げる(実機MHO98はスナップしない。試験用の癖)。"""
    if value <= 0:
        return value
    exponent = math.floor(math.log10(value))
    mantissa = value / 10.0**exponent
    for step in (5.0, 2.0, 1.0):
        if mantissa + 1e-12 >= step:
            return step * 10.0**exponent
    return 10.0**exponent


def _block(payload: bytes) -> bytes:
    """definite-length block(9桁長)+ 末尾改行にフレーミングする。"""
    return b"#9" + f"{len(payload):09d}".encode("ascii") + payload + b"\n"


# ---------------------------------------------------------------------------
# 固定データ
# ---------------------------------------------------------------------------


def _build_waveform() -> bytes:
    """決定的な正弦波1周期(raw最小127 / raw最大174)。"""
    center = (RAW_MAX + RAW_MIN) / 2.0
    amplitude = (RAW_MAX - RAW_MIN) / 2.0
    samples = bytearray()
    for i in range(WAVEFORM_POINTS):
        raw = round(center + amplitude * math.sin(2.0 * math.pi * i / WAVEFORM_POINTS))
        samples.append(min(RAW_MAX, max(RAW_MIN, int(raw))))
    return bytes(samples)


def _build_png() -> bytes:
    """小さな決定的PNG(先頭は `\\x89PNG\\r\\n\\x1a\\n`)。"""
    width, height = SCREENSHOT_SIZE
    image = Image.new("RGB", (width, height))
    image.putdata(
        [
            ((x * 8) % 256, (y * 10) % 256, 128)
            for y in range(height)
            for x in range(width)
        ]
    )
    buffer = io.BytesIO()
    image.save(buffer, format="PNG")
    return buffer.getvalue()


# 測定項目(短形式/長形式の双方を受理)。値は1kHz/3.268Vppプローブ補償信号相当。
_MEASUREMENTS: tuple[tuple[str, str], ...] = (
    ("FREQuency", "1.0001E+03"),
    ("PERiod", "9.999E-04"),
    ("VPP", "3.268E+00"),
    ("VMAX", "3.140E+00"),
    ("VMIN", "-6.8267E-02"),
    ("VAVG", "1.634E+00"),  # VAVerage は実機で不可(-222)
    ("VRMS", "1.836E+00"),
    ("PDUTy", "5.002E-01"),  # 単位は比率
    ("RTIMe", "1.0E-06"),
    ("FTIMe", "1.0E-06"),
)

_MEASURE_VALUES: dict[str, str] = {}
for _spec, _value in _MEASUREMENTS:
    _short, _long = _forms(_spec)
    _MEASURE_VALUES[_short] = _value
    _MEASURE_VALUES[_long] = _value

# オプション照会の `<type>`(MHO900プログラミングガイド 3.24.18/3.24.19)。
# 実機はこのリスト外のトークンにも沈黙する(docs/verification/mho98-unlicensed.md)。
_OPTION_TYPES = (
    "BND",
    "AFG100",
    "AFG50",
    "AUDIO",
    "CAN-FD",
    "FLEX",
    "AERO",
    "RLU-05",
    "BWU03T05",
    "BWU03T08",
    "BWU05T08",
)

_COUPLINGS = ("DC", "AC", "GND")
_BWLIMITS = ("OFF", "20M", "100M", "250M")
_IMPEDANCES = ("OMEG", "FIFTy")
_TRIGGER_MODES = ("EDGE",)
_SLOPES = ("POSitive", "NEGative", "RFALl")
_SWEEPS = ("AUTO", "NORMal", "SINGle")
_WAVEFORM_MODES = ("NORMal", "MAXimum", "RAW")
_WAVEFORM_FORMATS = ("BYTE", "WORD", "ASCii")

_CHANNEL = _mn("CHANnel") + r"([1-4])"
_VALUE = r"(\S+)"

# チャンネル属性: 内部キー → ニモニック仕様
_CHANNEL_PROPS: tuple[tuple[str, str], ...] = (
    ("display", "DISPlay"),
    ("scale", "SCALe"),
    ("offset", "OFFSet"),
    ("coupling", "COUPling"),
    ("probe", "PROBe"),
    ("bwlimit", "BWLimit"),
    ("impedance", "IMPedance"),
)

_TRIGGER_STATUS = {"RUN": "TD", "STOP": "STOP", "SINGLE": "WAIT"}

# ---------------------------------------------------------------------------
# シリアルデコード(:BUS<n> / docs/verification/mho98-unlicensed.md 3章)
# ---------------------------------------------------------------------------

BUS_COUNT = 4

_BUS_MODES = ("PARallel", "RS232", "SPI", "IIC", "LIN", "CAN")
_BUS_FORMATS = ("HEX", "ASCii", "DEC", "BIN")

#: 閾値の `<type>`(ライセンス必須プロトコルの種別は意図的に載せない)
_THRESHOLD_TYPES = (
    "TX", "RX", "SCL", "SDA", "CLK", "MISO", "MOSI", "CS", "CAN", "LIN", "PAL", "PALCLK",
)

#: `:BUS<n>:DATA?` ペイロード先頭のデコード種別トークン(モード短形式 → トークン)。
#: RS232 は実機実測(docs/verification/mho98-phase4.md)、PARALLEL はガイドの例。
#: それ以外は 要実機検証(ここではモード名をそのまま返す)。
_BUS_DATA_TOKENS = {"PAR": "PARALLEL", "IIC": "I2C"}

#: プロトコル別のイベントテーブル(ヘッダ行, 行...)。
#: RS232 のヘッダは実機実測。行の中身と他プロトコルの列構成は 要実機検証。
_BUS_DATA_TABLES: dict[str, tuple[str, tuple[str, ...]]] = {
    "RS232": ("Time,Tx/Rx,Data,Error,", ("-2.47us,Tx,0x55,,", "-2.444us,Tx,0xAA,,")),
    "PAR": ("Time,Data,", ("-2.47us,0,", "-2.444us,1,")),
}
#: 列構成が未確認のプロトコル用のフォールバック(要実機検証)
_BUS_DATA_DEFAULT = _BUS_DATA_TABLES["PAR"]

_SOURCES = (
    tuple(f"CHANnel{n}" for n in range(1, 5))
    + tuple(f"D{n}" for n in range(16))
    + ("OFF",)
)

#: プロトコル別プロパティ: (内部キー, ニモニック仕様, 型, 既定値)。
#: 型: "bool" / "int" / "float" / "src" / 列挙のタプル。
#: 既定値はMHO900プログラミングガイド 3.4 の初期値(BAUD 9600 等は実機実測とも一致)。
_BUS_PROTOCOL_PROPS: dict[str, tuple[tuple[str, str, object, object], ...]] = {
    "RS232": (
        ("tx", "TX", "src", "CHAN1"),
        ("rx", "RX", "src", "OFF"),
        ("polarity", "POLarity", ("POSitive", "NEGative"), "POS"),
        ("parity", "PARity", ("NONE", "ODD", "EVEN"), "NONE"),
        ("endian", "ENDian", ("MSB", "LSB"), "LSB"),
        ("baud", "BAUD", "int", 9600),
        ("dbits", "DBITs", ("5", "6", "7", "8", "9"), "8"),
        ("sbits", "SBITs", ("1", "1.5", "2"), "1"),
    ),
    "IIC": (
        ("scl", "SCLK:SOURce", "src", "CHAN1"),
        ("sda", "SDA:SOURce", "src", "CHAN2"),
        ("exchange", "EXCHange", "bool", False),
        ("addbits", "ADDBits", ("7", "8", "10"), "7"),
    ),
    "SPI": (
        ("sclk", "SCLK:SOURce", "src", "CHAN1"),
        ("slope", "SCLK:SLOPe", ("POSitive", "NEGative"), "POS"),
        ("miso", "MISO:SOURce", "src", "OFF"),
        ("mosi", "MOSI:SOURce", "src", "CHAN2"),
        ("polarity", "POLarity", ("HIGH", "LOW"), "LOW"),
        ("dbits", "DBITs", "int", 8),
        ("endian", "ENDian", ("MSB", "LSB"), "MSB"),
        ("mode", "MODE", ("CS", "TIMeout"), "CS"),
        ("timeout", "TIMeout:TIME", "float", 1.0e-6),
        ("ss", "SS:SOURce", "src", "CHAN1"),
        ("ss_polarity", "SS:POLarity", ("HIGH", "LOW"), "LOW"),
    ),
    "CAN": (
        ("source", "SOURce", "src", "CHAN1"),
        ("stype", "STYPe", ("TX", "RX", "CANH", "CANL", "DIFFerential"), "TX"),
        ("baud", "BAUD", "int", 1000000),
        ("spoint", "SPOint", "int", 50),
    ),
    "LIN": (
        ("source", "SOURce", "src", "CHAN1"),
        ("parity", "PARity", "bool", False),
        ("standard", "STANdard", ("V1X", "V2X", "MIXed"), "V2X"),
        ("baud", "BAUD", "int", 9600),
    ),
    "PARallel": (
        ("clk", "CLK", "src", "OFF"),
        ("slope", "SLOPe", ("POSitive", "NEGative"), "POS"),
        ("width", "WIDTh", "int", 8),
        ("endian", "ENDian", ("MSB", "LSB"), "MSB"),
        ("polarity", "POLarity", ("POSitive", "NEGative"), "POS"),
    ),
}


# ---------------------------------------------------------------------------
# 信号発生(:SOURce<n> / docs/verification/mho98-afg.md)
# ---------------------------------------------------------------------------

#: 信号発生チャンネル数(実機実測: :SOURce3 は沈黙する)
AFG_COUNT = 2

_AFG_FUNCTIONS = (
    "SINusoid", "SQUare", "RAMP", "NOISe", "DC", "ARB", "EXPRise", "EXPFall",
    "ECG1", "GAUSsian", "LORentz", "HAVersine", "SINC",
)
_AFG_IMPEDANCES = ("OMEG", "FIFTy")

#: 実機が返すトークンのうち、短形正規化では再現できないもの(実測: 設定後の
#: `:SOURce1:IMPedance?` は長形の `FIFTy` を返す)。
_AFG_RESPONSE_FORMS = {"FIFT": "FIFTy"}

#: 信号発生の属性: (内部キー, ニモニック仕様, 型, 既定値)。
#: 型 "nr3" は指数1桁NR3(`1.000000E+3`)、"dec6" は小数6桁(`0.000000`)で、
#: いずれも実機プローブの応答形式そのまま(mho98-afg.md 1章)。
#: 範囲外値のサイレントクランプは**再現しない**(実機固有の癖であり、
#: ドライバ側の対処は requested / applied の突合に集約されている)。
_AFG_PROPS: tuple[tuple[str, str, object, object], ...] = (
    ("output", "OUTPut:STATe", "bool", False),
    ("function", "FUNCtion", _AFG_FUNCTIONS, "SIN"),
    ("frequency", "FREQuency", "nr3", 1000.0),
    ("amplitude", "VOLTage:AMPLitude", "nr3", 5.0),
    ("offset", "VOLTage:OFFSet", "dec6", 0.0),
    ("phase", "PHASe", "dec6", 0.0),
    ("impedance", "IMPedance", _AFG_IMPEDANCES, "OMEG"),
    ("duty", "FUNCtion:SQUare:DUTY", "nr3", 50.0),
    ("symmetry", "FUNCtion:RAMP:SYMMetry", "nr3", 50.0),
)

# -- 信号発生: 変調(:SOURce<n>:MOD:* / ガイド3.25.15-25)--------------------

_AFG_MOD_TYPES = ("AM", "FM", "PM")
_AFG_MOD_WAVEFORMS = ("SINusoid", "SQUare", "TRIangle", "UPRamp", "DNRamp", "NOISe")

#: type短形式 → (深さ/偏移キー, SCPIパス)。ガイドの初期値そのまま。
_AFG_MOD_DEPTH_PATHS: dict[str, tuple[str, str]] = {
    "AM": ("depth", "DEPTh"),
    "FM": ("dev", "DEViation"),
    "PM": ("dev", "DEViation"),
}
_AFG_MOD_DEFAULTS: dict[str, dict[str, object]] = {
    "am": {"depth": 100.0, "freq": 100.0, "func": "SIN"},
    "fm": {"dev": 1000.0, "freq": 100.0, "func": "SIN"},
    "pm": {"dev": 90.0, "freq": 100.0, "func": "SIN"},
}


# ---------------------------------------------------------------------------
# MATH演算(:MATH<n> / MHO900プログラミングガイド 3.16章)
# ---------------------------------------------------------------------------

#: MATH演算チャンネル数(ガイド3.16: <n> = 1〜4)
MATH_COUNT = 4

_MATH_OPERATORS = (
    "ADD", "SUBTract", "MULTiply", "DIVision", "AND", "OR", "XOR", "NOT", "FFT",
    "INTG", "DIFF", "SQRT", "LG", "LN", "EXP", "ABS", "LPASs", "HPASs", "BPASs",
    "BSTop", "AXB",
)
#: 演算ソース(ガイド3.16.3/3.16.4)。MATH<m> は m<n のみ有効だが、
#: そのカスケード則の検証はドライバ側の責務(フェイクは形状のみ受理)。
_MATH_SOURCES = (
    tuple(f"CHANnel{n}" for n in range(1, 5))
    + tuple(f"REF{n}" for n in range(1, 11))
    + tuple(f"MATH{n}" for n in range(1, MATH_COUNT))
)
#: 論理演算ソース(ガイド3.16.5/3.16.6)
_MATH_LSOURCES = tuple(f"D{n}" for n in range(16)) + tuple(
    f"CHANnel{n}" for n in range(1, 5)
)
_MATH_FFT_WINDOWS = (
    "RECTangle", "BLACkman", "HANNing", "HAMMing", "FLATtop", "TRIangle",
)
_MATH_FFT_UNITS = ("VRMS", "DB")
_MATH_FFT_MODES = ("NORMal", "AVERage", "MAXHold")
_MATH_FFT_SEARCH_ORDERS = ("AMPorder", "FREQorder")
_MATH_FILTER_TYPES = ("LPASs", "HPASs", "BPASs", "BSTop")

#: MATH演算の属性: (内部キー, ニモニック仕様, 型, 既定値)。
#: 型 "nr3" は指数1桁NR3(ガイドの返却例 `2.000000E-1` と一致)。
#: 既定値はガイド3.16章の Default 列。ただし SCALe / FILTer:W1 / W2 は設定依存の
#: 動的値(Default欄が "Refer to Remarks")、SEARch:NUM / THReshold は逐語抽出が
#: ページ跨ぎで欠落しているため、いずれも代表値を置いている(実機未検証)。
#: HSCale / HCENter は意図的に非対応(FREQuency:STARt/END で表現する)。
_MATH_PROPS: tuple[tuple[str, str, object, object], ...] = (
    ("display", "DISPlay", "bool", False),
    ("operator", "OPERator", _MATH_OPERATORS, "ADD"),
    ("source1", "SOURce1", _MATH_SOURCES, "CHAN1"),
    ("source2", "SOURce2", _MATH_SOURCES, "CHAN1"),
    ("lsource1", "LSOurce1", _MATH_LSOURCES, "CHAN1"),
    ("lsource2", "LSOurce2", _MATH_LSOURCES, "CHAN1"),
    ("scale", "SCALe", "nr3", 1.0),
    ("offset", "OFFSet", "nr3", 0.0),
    ("invert", "INVert", "bool", False),
    ("fft_source", "FFT:SOURce", _MATH_SOURCES, "CHAN1"),
    ("fft_window", "FFT:WINDow", _MATH_FFT_WINDOWS, "HANN"),
    ("fft_unit", "FFT:UNIT", _MATH_FFT_UNITS, "DB"),
    ("fft_mode", "FFT:MODE", _MATH_FFT_MODES, "NORM"),
    ("fft_avcnt", "FFT:AVCNt", "int", 10),
    ("fft_scale", "FFT:SCALe", "nr3", 2.0),
    ("fft_offset", "FFT:OFFSet", "nr3", 0.0),
    ("fft_freq_start", "FFT:FREQuency:STARt", "nr3", 1.0),
    ("fft_freq_end", "FFT:FREQuency:END", "nr3", 1.0e7),
    ("fft_search", "FFT:SEARch:ENABle", "bool", False),
    ("fft_search_num", "FFT:SEARch:NUM", "int", 5),
    ("fft_search_threshold", "FFT:SEARch:THReshold", "nr3", -40.0),
    ("fft_search_excursion", "FFT:SEARch:EXCursion", "nr3", 1.8),
    ("fft_search_order", "FFT:SEARch:ORDer", _MATH_FFT_SEARCH_ORDERS, "AMP"),
    ("filter_type", "FILTer:TYPE", _MATH_FILTER_TYPES, "LPAS"),
    ("filter_w1", "FILTer:W1", "nr3", 1.0e6),
    ("filter_w2", "FILTer:W2", "nr3", 1.0e7),
)

#: `:MATH<n>:FFT:SEARch:RES?` の定型ピーク表(ガイド3.16.30の返却例そのまま)
_MATH_FFT_PEAKS = (
    "1,2.50000MHz,-24.98dBV",
    "2,3.50000MHz,-27.84dBV",
    "3,4.50000MHz,-30.04dBV",
    "4,5.50125MHz,-31.5dBV",
    "5,6.50125MHz,-32.34dBV",
)

_MATH_SOURCE = _mn("MATH") + rf"([1-{MATH_COUNT}])"


class FakeScope:
    """MHO98方言のフェイク機器(SCPIコマンド1件単位で応答する)。"""

    def __init__(
        self,
        stale_error_queue: bool = False,
        snap_to_125: bool = False,
        options: dict[str, bool] | None = None,
    ) -> None:
        self.snap_to_125 = snap_to_125
        # オプションのライセンス状態(`<type>` トークン → 導入済みか)。
        # 既定は全導入済み。明示した場合、挙げなかったトークンは未導入とする。
        supplied = {t.strip().upper(): bool(v) for t, v in (options or {}).items()}
        self.options: dict[str, bool] = {
            token: supplied.get(token, options is None) for token in _OPTION_TYPES
        }
        self.error_queue: deque[str] = deque()
        if stale_error_queue:
            # 前セッションの残留(phase0実測: 接続直後に -222 が残っていた)
            self.error_queue.append(OUT_OF_RANGE)
        self.command_log: list[str] = []

        self.channels: dict[int, dict] = {
            n: {
                "display": n == 1,
                # probe=10 の既定状態。Query応答は指数1桁の `1.000000E+1`。
                "scale": 10.0,
                "offset": 0.0,
                "coupling": "DC",
                "probe": 10.0,
                "bwlimit": "OFF",
                "impedance": "OMEG",
            }
            for n in range(1, 5)
        }
        # デコードバス(4本)。モード切替では各プロトコルの設定を消さない
        # (実機でも設定は保持される)。
        self.buses: dict[int, dict] = {
            n: {
                "mode": "PAR",
                "display": False,
                "format": "HEX",
                "event": False,
                "label": True,
                "position": 0,
                "thresholds": dict.fromkeys(_THRESHOLD_TYPES, 0.0),
                **{
                    protocol: {key: default for key, _, _, default in props}
                    for protocol, props in _BUS_PROTOCOL_PROPS.items()
                },
            }
            for n in range(1, BUS_COUNT + 1)
        }
        # 信号発生(2ch)。既定はガイドの初期値 = 実機プローブの実測値
        self.afg: dict[int, dict] = {
            n: {
                **{key: default for key, _, _, default in _AFG_PROPS},
                "mod_state": False,
                "mod_type": "AM",
                "am": dict(_AFG_MOD_DEFAULTS["am"]),
                "fm": dict(_AFG_MOD_DEFAULTS["fm"]),
                "pm": dict(_AFG_MOD_DEFAULTS["pm"]),
                "arb_path": "",
            }
            for n in range(1, AFG_COUNT + 1)
        }
        # MATH演算(4ch)。既定はガイド3.16章の初期値
        self.math: dict[int, dict] = {
            n: {key: default for key, _, _, default in _MATH_PROPS}
            for n in range(1, MATH_COUNT + 1)
        }
        # Resultビューの有効化済み測定項目(:MEASure:ITEM? でも追加される — issue #16)
        self.measurement_items: list[str] = []
        self.timebase: dict[str, float] = {"scale": 2.0e-4, "offset": 0.0}
        self.trigger: dict[str, object] = {
            "mode": "EDGE",
            "source": "CHAN1",
            "level": 0.0,
            "slope": "POS",
            "sweep": "AUTO",
        }
        self.acquisition = "RUN"
        self.waveform: dict[str, object] = {
            "source": "CHAN1",
            "mode": "NORM",
            "format": "BYTE",
            "start": 1,
            "stop": WAVEFORM_POINTS,
        }

        self._waveform_payload = _build_waveform()
        self._screenshot_png = _build_png()
        self._table = self._build_table()

    # -- 公開API ----------------------------------------------------------

    def handle(self, command: str) -> bytes | None:
        """SCPIコマンド1件を処理する。

        Queryは応答バイト列(改行なし)、書き込みは None を返す。
        未知・不正なコマンドはエラーキューを汚染して `SilentTimeout` を送出する。
        """
        self.command_log.append(command)
        text = command.strip()
        for pattern, handler in self._table:
            match = pattern.fullmatch(text)
            if match is not None:
                return handler(match)
        raise self._silent(COMMAND_ERROR)

    # -- 内部: エラー -----------------------------------------------------

    def _silent(self, error: str) -> SilentTimeout:
        self.error_queue.append(error)
        return SilentTimeout(error)

    def _float(self, token: str) -> float:
        try:
            return float(token)
        except ValueError:
            raise self._silent(OUT_OF_RANGE) from None

    def _int(self, token: str) -> int:
        try:
            return int(token)
        except ValueError:
            raise self._silent(OUT_OF_RANGE) from None

    def _enum(self, token: str, specs: tuple[str, ...]) -> str:
        value = _normalize(token, specs)
        if value is None:
            raise self._silent(OUT_OF_RANGE)
        return value

    # -- 内部: ディスパッチテーブル ---------------------------------------

    def _build_table(self) -> tuple[tuple[re.Pattern[str], Callable], ...]:
        entries: list[tuple[str, Callable]] = [
            (r"\*IDN\?", lambda m: IDN.encode("ascii")),
            (rf":?{_mn('SYSTem')}:{_mn('ERRor')}\?", self._system_error),
            # オプション照会。`:STATus?`(推奨)と `:VALid?`(後方互換)は同一応答。
            # `*OPT?` は実装しない(Rigol全シリーズで未定義ヘッダ = 沈黙)。
            (
                rf":?{_mn('SYSTem')}:{_mn('OPTion')}:"
                rf"(?:{_mn('STATus')}|{_mn('VALid')})\?\s+{_VALUE}",
                self._option_status,
            ),
        ]

        for key, spec in _CHANNEL_PROPS:
            entries.append(
                (
                    rf":?{_CHANNEL}:{_mn(spec)}\?",
                    lambda m, k=key: self._channel_query(k, int(m.group(1))),
                )
            )
            entries.append(
                (
                    rf":?{_CHANNEL}:{_mn(spec)}\s+{_VALUE}",
                    lambda m, k=key: self._channel_write(
                        k, int(m.group(1)), m.group(2)
                    ),
                )
            )

        timebase = rf":?{_mn('TIMebase')}:{_mn('MAIN')}"
        trigger = rf":?{_mn('TRIGger')}"
        edge = rf"{trigger}:{_mn('EDGE')}"
        waveform = rf":?{_mn('WAVeform')}"

        entries += [
            # 水平軸
            (rf"{timebase}:{_mn('SCALe')}\?", self._timebase_scale_query),
            (rf"{timebase}:{_mn('SCALe')}\s+{_VALUE}", self._timebase_scale_write),
            (
                rf"{timebase}:{_mn('OFFSet')}\?",
                lambda m: _nr3(self.timebase["offset"]).encode("ascii"),
            ),
            (
                rf"{timebase}:{_mn('OFFSet')}\s+{_VALUE}",
                lambda m: self._set_timebase("offset", self._float(m.group(1))),
            ),
            (rf":?{_mn('ACQuire')}:{_mn('SRATe')}\?", lambda m: SRATE.encode("ascii")),
            (
                rf":?{_mn('ACQuire')}:{_mn('MDEPth')}\?",
                lambda m: MDEPTH.encode("ascii"),
            ),
            # トリガ
            (
                rf"{trigger}:{_mn('MODE')}\?",
                lambda m: str(self.trigger["mode"]).encode("ascii"),
            ),
            (
                rf"{trigger}:{_mn('MODE')}\s+{_VALUE}",
                lambda m: self._set_trigger(
                    "mode", self._enum(m.group(1), _TRIGGER_MODES)
                ),
            ),
            (
                rf"{edge}:{_mn('SOURce')}\?",
                lambda m: str(self.trigger["source"]).encode("ascii"),
            ),
            (
                rf"{edge}:{_mn('SOURce')}\s+{_VALUE}",
                lambda m: self._set_trigger("source", self._channel_token(m.group(1))),
            ),
            (
                rf"{edge}:{_mn('LEVel')}\?",
                lambda m: _nr3(float(self.trigger["level"])).encode("ascii"),
            ),
            (
                rf"{edge}:{_mn('LEVel')}\s+{_VALUE}",
                lambda m: self._set_trigger("level", self._float(m.group(1))),
            ),
            (
                rf"{edge}:{_mn('SLOPe')}\?",
                lambda m: str(self.trigger["slope"]).encode("ascii"),
            ),
            (
                rf"{edge}:{_mn('SLOPe')}\s+{_VALUE}",
                lambda m: self._set_trigger("slope", self._enum(m.group(1), _SLOPES)),
            ),
            (
                rf"{trigger}:{_mn('SWEep')}\?",
                lambda m: str(self.trigger["sweep"]).encode("ascii"),
            ),
            (
                rf"{trigger}:{_mn('SWEep')}\s+{_VALUE}",
                lambda m: self._set_trigger("sweep", self._enum(m.group(1), _SWEEPS)),
            ),
            (
                rf"{trigger}:{_mn('STATus')}\?",
                lambda m: _TRIGGER_STATUS[self.acquisition].encode("ascii"),
            ),
            # Acquisition
            (r":?RUN", lambda m: self._set_acquisition("RUN")),
            (r":?STOP", lambda m: self._set_acquisition("STOP")),
            (rf":?{_mn('SINGle')}", lambda m: self._set_acquisition("SINGLE")),
            # 実機の正式ニモニックは :AUToset(:AUToscale は未定義ヘッダ=沈黙)
            (rf":?{_mn('AUToset')}", lambda m: self._set_acquisition("RUN")),
            # 測定
            (
                rf":?{_mn('MEASure')}:{_mn('ITEM')}\?\s+(\w+)\s*,\s*{_VALUE}",
                self._measure_item,
            ),
            # 実機仕様: 有効化済みの全測定項目をResultビューから消す(引数なし)。
            # ニモニックはファミリで分岐する(MHO900: DELete / DHO800系: CLEar)
            (
                rf":?{_mn('MEASure')}:(?:{_mn('DELete')}|{_mn('CLEar')})",
                self._measure_delete,
            ),
            # 波形
            (
                rf"{waveform}:{_mn('SOURce')}\?",
                lambda m: str(self.waveform["source"]).encode("ascii"),
            ),
            (
                rf"{waveform}:{_mn('SOURce')}\s+{_VALUE}",
                lambda m: self._set_waveform(
                    "source", self._waveform_source_token(m.group(1))
                ),
            ),
            (
                rf"{waveform}:{_mn('MODE')}\?",
                lambda m: str(self.waveform["mode"]).encode("ascii"),
            ),
            (
                rf"{waveform}:{_mn('MODE')}\s+{_VALUE}",
                lambda m: self._set_waveform(
                    "mode", self._enum(m.group(1), _WAVEFORM_MODES)
                ),
            ),
            (
                rf"{waveform}:{_mn('FORMat')}\?",
                lambda m: str(self.waveform["format"]).encode("ascii"),
            ),
            (
                rf"{waveform}:{_mn('FORMat')}\s+{_VALUE}",
                lambda m: self._set_waveform(
                    "format", self._enum(m.group(1), _WAVEFORM_FORMATS)
                ),
            ),
            (
                rf"{waveform}:{_mn('STARt')}\?",
                lambda m: str(self.waveform["start"]).encode("ascii"),
            ),
            (
                rf"{waveform}:{_mn('STARt')}\s+{_VALUE}",
                lambda m: self._set_waveform("start", self._int(m.group(1))),
            ),
            (
                rf"{waveform}:STOP\?",
                lambda m: str(self.waveform["stop"]).encode("ascii"),
            ),
            (
                rf"{waveform}:STOP\s+{_VALUE}",
                lambda m: self._set_waveform("stop", self._int(m.group(1))),
            ),
            (
                rf"{waveform}:{_mn('PREamble')}\?",
                lambda m: self._preamble(),
            ),
            (
                rf"{waveform}:{_mn('DATA')}\?",
                lambda m: _block(self._waveform_payload),
            ),
            # スクリーンショット。DHO800/900は形式引数を取る(既定BMP、ガイド3.9.7)
            (
                rf":?{_mn('DISPlay')}:{_mn('DATA')}\?(?:\s+(?:BMP|PNG|JPG))?",
                lambda m: _block(self._screenshot_png),
            ),
        ]
        entries += self._bus_entries()
        entries += self._afg_entries()
        entries += self._afg_mod_entries()
        entries += self._math_entries()
        return tuple(
            (re.compile(pattern, re.IGNORECASE), handler)
            for pattern, handler in entries
        )

    def _bus_entries(self) -> list[tuple[str, Callable]]:
        """デコードバスのディスパッチ表。

        `:BUS5` や未実装のプロトコル配下(IIS / FLEXray / M1553 / CAN:FDBaud)は
        どのパターンにも一致せず、実機同様に沈黙する。
        """
        bus = rf":?{_mn('BUS')}([1-{BUS_COUNT}])"
        entries: list[tuple[str, Callable]] = [
            (rf"{bus}:{_mn('MODE')}\?", lambda m: self._bus(m)["mode"].encode("ascii")),
            (
                rf"{bus}:{_mn('MODE')}\s+{_VALUE}",
                lambda m: self._bus_set(m, "mode", self._enum(m.group(2), _BUS_MODES)),
            ),
            (
                rf"{bus}:{_mn('FORMat')}\?",
                lambda m: self._bus(m)["format"].encode("ascii"),
            ),
            (
                rf"{bus}:{_mn('FORMat')}\s+{_VALUE}",
                lambda m: self._bus_set(
                    m, "format", self._enum(m.group(2), _BUS_FORMATS)
                ),
            ),
            (
                rf"{bus}:{_mn('LABel')}\?",
                lambda m: b"1" if self._bus(m)["label"] else b"0",
            ),
            (
                rf"{bus}:{_mn('POSition')}\?",
                lambda m: str(self._bus(m)["position"]).encode("ascii"),
            ),
            (
                rf"{bus}:{_mn('POSition')}\s+{_VALUE}",
                lambda m: self._bus_set(m, "position", self._int(m.group(2))),
            ),
            (
                rf"{bus}:{_mn('DISPlay')}\?",
                lambda m: b"1" if self._bus(m)["display"] else b"0",
            ),
            (
                rf"{bus}:{_mn('DISPlay')}\s+{_VALUE}",
                lambda m: self._bus_set(m, "display", self._on_off(m.group(2))),
            ),
            (rf"{bus}:{_mn('EVENt')}\?", lambda m: b"1" if self._bus(m)["event"] else b"0"),
            (rf"{bus}:{_mn('EVENt')}\s+{_VALUE}", self._bus_event_write),
            (rf"{bus}:{_mn('DATA')}\?", self._bus_data),
            (rf"{bus}:{_mn('THReshold')}\?\s+{_VALUE}", self._bus_threshold_query),
            (
                rf"{bus}:{_mn('THReshold')}\s+(\S+)\s*,\s*(\S+)",
                self._bus_threshold_write,
            ),
        ]

        for protocol, props in _BUS_PROTOCOL_PROPS.items():
            head = rf"{bus}:{_mn(protocol)}"
            for key, spec, kind, _default in props:
                path = ":".join(_mn(part) for part in spec.split(":"))
                entries.append(
                    (
                        rf"{head}:{path}\?",
                        lambda m, p=protocol, k=key, t=kind: self._bus_prop_query(
                            m, p, k, t
                        ),
                    )
                )
                entries.append(
                    (
                        rf"{head}:{path}\s+{_VALUE}",
                        lambda m, p=protocol, k=key, t=kind: self._bus_prop_write(
                            m, p, k, t, m.group(2)
                        ),
                    )
                )
        return entries

    def _afg_entries(self) -> list[tuple[str, Callable]]:
        """信号発生のディスパッチ表(`:SOURce1` / `:SOURce2` のみ)。

        `:SOURce3` はどのパターンにも一致せず、実機同様に沈黙する
        (docs/verification/mho98-afg.md 1章)。
        """
        source = rf":?{_mn('SOURce')}([1-{AFG_COUNT}])"
        entries: list[tuple[str, Callable]] = []
        for key, spec, kind, _default in _AFG_PROPS:
            path = ":".join(_mn(part) for part in spec.split(":"))
            entries.append(
                (
                    rf"{source}:{path}\?",
                    lambda m, k=key, t=kind: self._afg_query(m, k, t),
                )
            )
            entries.append(
                (
                    rf"{source}:{path}\s+{_VALUE}",
                    lambda m, k=key, t=kind: self._afg_write(m, k, t, m.group(2)),
                )
            )
        return entries

    def _afg_mod_entries(self) -> list[tuple[str, Callable]]:
        """変調(`:SOURce<n>:MOD:*`)+ ARB選択 + 位相同期のディスパッチ表。

        `:SOURce3` 配下と同様、`:SOURce<n>` の範囲外は既存の `_afg_entries` 同様
        どのパターンにも一致せず沈黙する。
        """
        source = rf":?{_mn('SOURce')}([1-{AFG_COUNT}])"
        mod = rf"{source}:{_mn('MOD')}"
        entries: list[tuple[str, Callable]] = [
            (
                rf"{mod}:{_mn('STATe')}\?",
                lambda m: b"1" if self._afg(m)["mod_state"] else b"0",
            ),
            (rf"{mod}:{_mn('STATe')}\s+{_VALUE}", self._afg_mod_state_write),
            (
                rf"{mod}:{_mn('TYPe')}\?",
                lambda m: str(self._afg(m)["mod_type"]).encode("ascii"),
            ),
            (rf"{mod}:{_mn('TYPe')}\s+{_VALUE}", self._afg_mod_type_write),
        ]
        for token in ("AM", "FM", "PM"):
            low = token.lower()
            depth_key, depth_spec = _AFG_MOD_DEPTH_PATHS[token]
            entries += [
                (
                    rf"{mod}:{token}:{_mn(depth_spec)}\?",
                    lambda m, t=low, k=depth_key: _nr3_single_digit_exponent(
                        self._afg(m)[t][k]
                    ).encode("ascii"),
                ),
                (
                    rf"{mod}:{token}:{_mn(depth_spec)}\s+{_VALUE}",
                    lambda m, t=low, k=depth_key: self._afg_mod_set(
                        m, t, k, self._float(m.group(2))
                    ),
                ),
                (
                    rf"{mod}:{token}:{_mn('INTernal')}:{_mn('FREQuency')}\?",
                    lambda m, t=low: _nr3_single_digit_exponent(
                        self._afg(m)[t]["freq"]
                    ).encode("ascii"),
                ),
                (
                    rf"{mod}:{token}:{_mn('INTernal')}:{_mn('FREQuency')}\s+{_VALUE}",
                    lambda m, t=low: self._afg_mod_set(
                        m, t, "freq", self._float(m.group(2))
                    ),
                ),
                (
                    rf"{mod}:{token}:{_mn('INTernal')}:{_mn('FUNCtion')}\?",
                    lambda m, t=low: str(self._afg(m)[t]["func"]).encode("ascii"),
                ),
                (
                    rf"{mod}:{token}:{_mn('INTernal')}:{_mn('FUNCtion')}\s+{_VALUE}",
                    lambda m, t=low: self._afg_mod_set(
                        m, t, "func", self._enum(m.group(2), _AFG_MOD_WAVEFORMS)
                    ),
                ),
            ]
        entries += [
            (
                rf"{source}:{_mn('LOAD')}:{_mn('ARBitrary')}\?",
                lambda m: str(self._afg(m)["arb_path"]).encode("ascii"),
            ),
            (
                rf"{source}:{_mn('LOAD')}:{_mn('ARBitrary')}\s+{_VALUE}",
                self._afg_arb_write,
            ),
            # 引数無し・応答無しのwrite-only命令。状態は変えない(no-opで十分)。
            (rf"{source}:{_mn('PHASe')}:{_mn('SYNChronize')}", lambda m: None),
        ]
        return entries

    def _math_entries(self) -> list[tuple[str, Callable]]:
        """MATH演算のディスパッチ表(`:MATH1`〜`:MATH4` のみ)。

        `:MATH5` は `[1-4]` に一致せず、実機同様に沈黙する。スコープ外のサブツリー
        (GRID / EXPand / RESet / WAVetype / SENSitivity / DISTance / THReshold /
        WINDow:TITLe? / LABel:SHOW / DISMode / FFT:HSCale / FFT:HCENter)も同様。
        """
        math = rf":?{_MATH_SOURCE}"
        entries: list[tuple[str, Callable]] = []
        for key, spec, kind, _default in _MATH_PROPS:
            path = ":".join(_mn_indexed(part) for part in spec.split(":"))
            entries.append(
                (
                    rf"{math}:{path}\?",
                    lambda m, k=key, t=kind: self._math_query(m, k, t),
                )
            )
            entries.append(
                (
                    rf"{math}:{path}\s+{_VALUE}",
                    lambda m, k=key, t=kind: self._math_write(m, k, t, m.group(2)),
                )
            )
        entries.append((rf"{math}:{_mn('FFT')}:{_mn('SEARch')}:RES\?", self._math_peaks))
        return entries

    # -- 内部: ハンドラ ---------------------------------------------------

    def _system_error(self, match: re.Match[str]) -> bytes:
        error = self.error_queue.popleft() if self.error_queue else NO_ERROR
        return error.encode("ascii")

    def _option_status(self, match: re.Match[str]) -> bytes:
        installed = self.options.get(match.group(1).strip().upper())
        if installed is None:
            # 実機実測: リスト外トークン(例 AUTOA)でもSCPIサーバーが沈黙する
            raise self._silent(OUT_OF_RANGE)
        return b"1" if installed else b"0"

    def _channel_token(self, token: str) -> str:
        """`CHANnel2` / `chan2` → `CHAN2`(短形式)。"""
        match = re.fullmatch(_CHANNEL, token.strip(), re.IGNORECASE)
        if match is None:
            raise self._silent(OUT_OF_RANGE)
        return f"CHAN{match.group(1)}"

    def _waveform_source_token(self, token: str) -> str:
        """波形ソースは `{CHANnel1-4|MATH1-4}` を受理する(ガイド3.28.1)。

        トリガソースや測定ソースは MATH を取らないため、`_channel_token` 自体は
        広げない。
        """
        match = re.fullmatch(_MATH_SOURCE, token.strip(), re.IGNORECASE)
        if match is not None:
            return f"MATH{match.group(1)}"
        return self._channel_token(token)

    def _channel_query(self, key: str, number: int) -> bytes:
        state = self.channels[number]
        if key == "display":
            return b"1" if state["display"] else b"0"
        if key == "scale":
            # phase0で観測した指数1桁形式(既定 probe=10 では `1.000000E+1`)
            return _nr3_single_digit_exponent(state["scale"]).encode("ascii")
        if key in ("offset", "probe"):
            return _nr3(state[key]).encode("ascii")
        return str(state[key]).encode("ascii")

    def _channel_write(self, key: str, number: int, token: str) -> None:
        state = self.channels[number]
        if key == "display":
            value = _normalize(token, ("ON", "OFF"))
            if value is None:
                if token.strip() not in ("0", "1"):
                    raise self._silent(OUT_OF_RANGE)
                value = "ON" if token.strip() == "1" else "OFF"
            state["display"] = value == "ON"
        elif key == "scale":
            scale = self._float(token)
            state["scale"] = _snap_125(scale) if self.snap_to_125 else scale
        elif key in ("offset", "probe"):
            state[key] = self._float(token)
        elif key == "coupling":
            state["coupling"] = self._enum(token, _COUPLINGS)
        elif key == "bwlimit":
            state["bwlimit"] = self._enum(token, _BWLIMITS)
        elif key == "impedance":
            state["impedance"] = self._enum(token, _IMPEDANCES)
        return None

    def _preamble(self) -> bytes:
        """プリアンブルを組み立てる。yorigin は波形ソースのoffset依存。

        yorigin は「垂直リファレンス位置からのずれ」を生カウントで表した動的値で、
        `offset / yincrement` に等しい(実機実測: offset -0.064 V → yorigin -9.0)。
        生波形データ自体は offset を変えても変化しない。

        MATHソースでは垂直状態をMATHチャンネル側から取る(yincrement はアナログch
        と同じ定数のまま — FFTトレースのプリアンブル解釈は要実機検証)。
        """
        source = str(self.waveform["source"])
        if source.startswith("MATH"):
            offset = float(self.math[int(source.removeprefix("MATH"))]["offset"])
        else:
            offset = float(self.channels[int(source.removeprefix("CHAN"))]["offset"])
        yorigin = round(offset / YINCREMENT)
        return f"{PREAMBLE_HEAD},{yorigin},{YREFERENCE}".encode("ascii")

    def _timebase_scale_query(self, match: re.Match[str]) -> bytes:
        return _nr3(self.timebase["scale"]).encode("ascii")

    def _timebase_scale_write(self, match: re.Match[str]) -> None:
        scale = self._float(match.group(1))
        if self.snap_to_125:
            scale = _snap_125(scale)
        return self._set_timebase("scale", scale)

    def _set_timebase(self, key: str, value: float) -> None:
        self.timebase[key] = value
        return None

    def _set_trigger(self, key: str, value: object) -> None:
        self.trigger[key] = value
        return None

    def _set_waveform(self, key: str, value: object) -> None:
        self.waveform[key] = value
        return None

    def _set_acquisition(self, state: str) -> None:
        self.acquisition = state
        return None

    # -- 内部: デコードバス -----------------------------------------------

    def _bus(self, match: re.Match[str]) -> dict:
        return self.buses[int(match.group(1))]

    def _bus_set(self, match: re.Match[str], key: str, value: object) -> None:
        self._bus(match)[key] = value
        return None

    def _on_off(self, token: str) -> bool:
        value = _normalize(token, ("ON", "OFF"))
        if value is None:
            if token.strip() not in ("0", "1"):
                raise self._silent(OUT_OF_RANGE)
            value = "ON" if token.strip() == "1" else "OFF"
        return value == "ON"

    def _bus_event_write(self, match: re.Match[str]) -> None:
        """イベントテーブルはバスの表示がONでなければ有効化できない(ガイド)。"""
        enabled = self._on_off(match.group(2))
        bus = self._bus(match)
        if enabled and not bus["display"]:
            raise self._silent(OUT_OF_RANGE)
        bus["event"] = enabled
        return None

    def _threshold_type(self, token: str) -> str:
        value = _normalize(token, _THRESHOLD_TYPES)
        if value is None:
            raise self._silent(OUT_OF_RANGE)
        return value

    def _bus_data(self, match: re.Match[str]) -> bytes:
        """イベントテーブルをTMCブロックで返す。

        表示・イベントテーブルが無効なときの実機挙動は未確認(要実機検証)。
        ここでは空ペイロードを返す(ドライバは送信前に早期returnする)。
        """
        bus = self._bus(match)
        if not (bus["display"] and bus["event"]):
            return _block(b"")
        mode = bus["mode"]
        header, rows = _BUS_DATA_TABLES.get(mode, _BUS_DATA_DEFAULT)
        lines = [_BUS_DATA_TOKENS.get(mode, mode), header, *rows]
        return _block(("\n".join(lines) + "\n").encode("ascii"))

    def _bus_threshold_query(self, match: re.Match[str]) -> bytes:
        thresholds = self._bus(match)["thresholds"]
        # 実機実測: `:BUS1:THReshold? TX` → `0.000000`(NR3ではなく小数6桁)
        return f"{thresholds[self._threshold_type(match.group(2))]:.6f}".encode("ascii")

    def _bus_threshold_write(self, match: re.Match[str]) -> None:
        value = self._float(match.group(2))
        self._bus(match)["thresholds"][self._threshold_type(match.group(3))] = value
        return None

    def _bus_prop_query(
        self, match: re.Match[str], protocol: str, key: str, kind: object
    ) -> bytes:
        value = self._bus(match)[protocol][key]
        if kind == "bool":
            return b"1" if value else b"0"
        if kind == "float":
            return _nr3(float(value)).encode("ascii")
        return str(value).encode("ascii")

    def _bus_prop_write(
        self, match: re.Match[str], protocol: str, key: str, kind: object, token: str
    ) -> None:
        if kind == "bool":
            value: object = self._on_off(token)
        elif kind == "int":
            value = self._int(token)
        elif kind == "float":
            value = self._float(token)
        elif kind == "src":
            value = self._enum(token, _SOURCES)
        else:
            value = self._enum(token, kind)  # 列挙(仕様タプル)
        self._bus(match)[protocol][key] = value
        return None

    # -- 内部: 信号発生 ---------------------------------------------------

    def _afg(self, match: re.Match[str]) -> dict:
        return self.afg[int(match.group(1))]

    def _afg_query(self, match: re.Match[str], key: str, kind: object) -> bytes:
        value = self._afg(match)[key]
        if kind == "bool":
            return b"1" if value else b"0"
        if kind == "nr3":
            return _nr3_single_digit_exponent(float(value)).encode("ascii")
        if kind == "dec6":
            return f"{float(value):.6f}".encode("ascii")
        return _AFG_RESPONSE_FORMS.get(str(value), str(value)).encode("ascii")

    def _afg_write(
        self, match: re.Match[str], key: str, kind: object, token: str
    ) -> None:
        if kind == "bool":
            value: object = self._on_off(token)
        elif kind in ("nr3", "dec6"):
            value = self._float(token)
        else:
            value = self._enum(token, kind)  # 列挙(仕様タプル)
        self._afg(match)[key] = value
        return None

    def _afg_mod_state_write(self, match: re.Match[str]) -> None:
        self._afg(match)["mod_state"] = self._on_off(match.group(2))
        return None

    def _afg_mod_type_write(self, match: re.Match[str]) -> None:
        self._afg(match)["mod_type"] = self._enum(match.group(2), _AFG_MOD_TYPES)
        return None

    def _afg_mod_set(
        self, match: re.Match[str], mod_type: str, key: str, value: object
    ) -> None:
        # 実機quirk(2026-08-27実測): MOD:STATe OFF中のパラメータ書き込みは
        # エラーなしで無視される(表示OFFチャンネルへの書き込み無視と同族)
        if not self._afg(match)["mod_state"]:
            return None
        self._afg(match)[mod_type][key] = value
        return None

    def _afg_arb_write(self, match: re.Match[str]) -> None:
        """ARBファイルパスを裸文字列のまま保存する(引用符無し・往復のみ)。"""
        self._afg(match)["arb_path"] = match.group(2)
        return None

    # -- 内部: MATH演算 ---------------------------------------------------

    def _math(self, match: re.Match[str]) -> dict:
        return self.math[int(match.group(1))]

    def _math_query(self, match: re.Match[str], key: str, kind: object) -> bytes:
        value = self._math(match)[key]
        if kind == "bool":
            return b"1" if value else b"0"
        if kind == "nr3":
            return _nr3_single_digit_exponent(float(value)).encode("ascii")
        return str(value).encode("ascii")

    def _math_write(
        self, match: re.Match[str], key: str, kind: object, token: str
    ) -> None:
        if kind == "bool":
            value: object = self._on_off(token)
        elif kind == "nr3":
            value = self._float(token)
        elif kind == "int":
            value = self._int(token)
        else:
            value = self._enum(token, kind)  # 列挙(仕様タプル)
        self._math(match)[key] = value
        return None

    def _math_peaks(self, match: re.Match[str]) -> bytes:
        """ピーク探索結果テーブル(ガイド3.16.30)。

        探索が無効なときの実機挙動は未確認(要実機検証)。`_bus_data` の
        イベントテーブル無効時と同じ流儀で、ここでは空応答を返す。
        """
        if not self._math(match)["fft_search"]:
            return b""
        return "\n".join(_MATH_FFT_PEAKS).encode("ascii")

    def _measure_item(self, match: re.Match[str]) -> bytes:
        item = match.group(1).strip().upper()
        self._channel_token(match.group(2))
        value = _MEASURE_VALUES.get(item)
        if value is None:
            # phase0実測: VAVerage は受理されず -222 が積まれる
            raise self._silent(OUT_OF_RANGE)
        # 実機仕様: クエリ形でも項目がResultビューへ追加される(issue #16)
        self.measurement_items.append(item)
        return value.encode("ascii")

    def _measure_delete(self, match: re.Match[str]) -> None:
        self.measurement_items.clear()
        return None
