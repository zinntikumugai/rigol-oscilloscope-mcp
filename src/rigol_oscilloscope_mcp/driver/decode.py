"""シリアルデコード(`:BUS<n>`)の意味的キー ⇔ SCPI 変換表。

`measurement_items`(プロファイル)と同じ発想で、**LLMが扱う意味的キー**と
機種依存のSCPIニモニックを1枚の表に閉じ込める。プロトコル名 → SCPIニモニックの
対応だけはプロファイル(`dialect.decode_protocols`)が持ち、その配下の項目名は
Rigolのデコード系で共通なので本表が持つ(宣言の不在=非対応、は据え置き:
プロファイルが宣言しないプロトコルは送信前に UNSUPPORTED_FEATURE になる)。

規範(docs/verification/mho98-unlicensed.md 3章 / MHO900プログラミングガイド 3.4):

- 値域・列挙はここで**送信前**に検証する(不正トークンは実機を沈黙させる)
- 閾値だけは形が不規則(`:BUS<n>:THReshold <value>,<type>` / 問い合わせは
  `:BUS<n>:THReshold? <type>`)なので `threshold_type` を持つ項目として表す
- 単位付きキー(`baud_bps` / `timeout_s` / `*_threshold_v`)はSI基本単位
"""

from __future__ import annotations

import re
from collections.abc import Callable
from dataclasses import dataclass

from ..errors import ErrorCode, ScopeError
from .parsers import format_number, parse_bool, parse_eng_number, parse_nr3

# 変換器の型: (値, 意味的キー) → 送信トークン / 応答文字列 → 値
ToScpi = Callable[[object, str], str]
FromScpi = Callable[[str], object]


@dataclass(frozen=True)
class DecodeItem:
    """デコード設定1項目。

    `path` は `:BUS<n>:<プロトコル>` に続く断片(閾値項目では未使用)。
    """

    path: str
    to_scpi: ToScpi
    from_scpi: FromScpi
    threshold_type: str | None = None


def _invalid(message: str, detail: dict) -> ScopeError:
    return ScopeError(ErrorCode.INVALID_PARAMETER, message, detail)


def _forms(spec: str) -> tuple[str, str]:
    """`'POSitive'` → `('POS', 'POSITIVE')`(短形式, 長形式)。"""
    short = "".join(c for c in spec if not c.islower()).upper()
    return short, spec.upper()


def _scpi_error(text: str) -> ScopeError:
    return ScopeError(
        ErrorCode.SCPI_ERROR,
        f"cannot interpret decode response: {text!r}",
        {"raw": text},
    )


def _number(value: object, key: str) -> float:
    """数値として受理する(bool は数値扱いしない)。"""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise _invalid(
            f"{key} is not a number: {value!r}", {"key": key, "value": value}
        )
    return float(value)


def _digits(value: float) -> str:
    """`1.0` → `"1"`、`1.5` → `"1.5"`(SCPIの選択肢トークン用)。"""
    return str(int(value)) if float(value).is_integer() else str(value)


def _to_number(text: str) -> int | float:
    """応答を数値へ。整数値は int で返す(`8` を `8.0` にしない)。"""
    value = parse_nr3(text)
    return int(value) if value.is_integer() else value


# -- 変換器ファクトリ(いずれも (to_scpi, from_scpi) を返す)-----------------


def _enum(pairs: tuple[tuple[str, str], ...]) -> tuple[ToScpi, FromScpi]:
    """意味的な値 ⇔ SCPIニモニック。応答は短形式〜長形式の任意の略形を受理する。

    SCPI規格上、機器は短形式以上・長形式以下の**任意の略形**で応答してよく、
    ガイドのReturn Format欄はあてにならない。実測(MHO98 firmware 00.01.00):
    ガイド3.20.7 は `:REFerence:COLor?` の緑を `GRE` と書くが、実機が返すのは
    `GREE`。工場出荷状態の枠4・枠9が緑なので、2形しか見ない実装では未操作の
    実機で `get_reference_state` が丸ごと落ちる。

    曖昧さの扱い(この表は decode / AFG / MATH / cursor / counter / meter /
    histogram / reference の全列挙が通る):

    1. 短形式・長形式との**完全一致**を最優先する(規範の2形は必ず一意に読む)
    2. 完全一致が無ければ前置一致で探し、候補が1個のときだけ受理する
    3. 候補が2個以上なら黙って片方を選ばず `SCPI_ERROR` にする — 読み値は
       そのまま書き戻される値なので、推測で確定させると誤設定になる

    現行の全テーブルを走査した限り曖昧になる組は無い(`math_fft_search_orders`
    の `AMPorder` / `FREQorder` も `counter_modes` も接頭辞が重ならない)。3.は
    将来テーブルが増えたときのための安全側の既定であり、通常は発火しない。
    """
    to_token = dict(pairs)
    # 規範の2形は「トークン → 意味的な値の集合」で持つ。別々の値の短形/長形が
    # 衝突する表(`TIMe` と `TIMeout` は短形がどちらも `TIM`)を、後勝ちで黙って
    # 潰さないため
    from_token: dict[str, set[str]] = {}
    abbreviations: list[tuple[str, int, str]] = []  # (長形式, 短形式長, 意味的な値)
    for semantic, spec in pairs:
        short, long = _forms(spec)
        from_token.setdefault(short, set()).add(semantic)
        from_token.setdefault(long, set()).add(semantic)
        abbreviations.append((long, len(short), semantic))

    def to_scpi(value: object, key: str) -> str:
        token = value.strip().lower() if isinstance(value, str) else value
        if not isinstance(token, str) or token not in to_token:
            raise _invalid(
                f"invalid {key} value: {value!r} (allowed: {sorted(to_token)})",
                {"key": key, "value": value, "allowed": sorted(to_token)},
            )
        return to_token[token]

    def from_scpi(text: str) -> str:
        token = text.strip().upper()
        matched = from_token.get(token) or {
            value
            for long, width, value in abbreviations
            if len(token) >= width and long.startswith(token)
        }
        if len(matched) != 1:
            raise _scpi_error(text)
        # `pop()` は `from_token` が持つ集合そのものを空にしてしまう(2回目以降の
        # 読みが前置一致へ落ちる)。短形が長形の前置になっていないトークン
        # (`CHANnel1` → `CHAN1`)ではそれが即エラーになるため、非破壊で取り出す
        return next(iter(matched))

    return to_scpi, from_scpi


def _choice(values: tuple[float, ...]) -> tuple[ToScpi, FromScpi]:
    """数値の選択肢(データビット数・ストップビット数など)。"""
    allowed = [_to_number(_digits(value)) for value in values]

    def to_scpi(value: object, key: str) -> str:
        number = _number(value, key)
        for candidate in values:
            if number == candidate:
                return _digits(candidate)
        raise _invalid(
            f"invalid {key} value: {value!r} (allowed: {allowed})",
            {"key": key, "value": value, "allowed": allowed},
        )

    return to_scpi, _to_number


def _int_range(low: int, high: int) -> tuple[ToScpi, FromScpi]:
    def to_scpi(value: object, key: str) -> str:
        number = _number(value, key)
        if not number.is_integer() or not low <= number <= high:
            raise _invalid(
                f"{key} must be an integer between {low} and {high}: {value!r}",
                {"key": key, "value": value, "min": low, "max": high},
            )
        return str(int(number))

    return to_scpi, lambda text: int(parse_nr3(text))


def _float_range(low: float, high: float) -> tuple[ToScpi, FromScpi]:
    def to_scpi(value: object, key: str) -> str:
        number = _number(value, key)
        if not low <= number <= high:
            raise _invalid(
                f"{key} must be between {low} and {high}: {value!r}",
                {"key": key, "value": value, "min": low, "max": high},
            )
        return format_number(number)

    return to_scpi, parse_nr3


def _bool() -> tuple[ToScpi, FromScpi]:
    def to_scpi(value: object, key: str) -> str:
        if not isinstance(value, bool):
            raise _invalid(
                f"{key} is not a boolean: {value!r}", {"key": key, "value": value}
            )
        return "ON" if value else "OFF"

    return to_scpi, parse_bool


#: デコードソース。アナログは4本固定(デコードを宣言するのは4chの検証済み機種だけ)
_SOURCE_RE = re.compile(
    r"^(?:CH(?:AN|ANNEL)?\s*([1-4])|(D(?:1[0-5]|[0-9]))|(OFF))$", re.IGNORECASE
)

#: ソースの表記例(エラーメッセージ用)
SOURCE_EXAMPLES = "'CH1'-'CH4', 'D0'-'D15'"


def _source(allow_off: bool) -> tuple[ToScpi, FromScpi]:
    """`CH1` / `D0` / `off` ⇔ `CHANnel1` / `D0` / `OFF`。"""
    allowed = SOURCE_EXAMPLES + (", 'off'" if allow_off else "")

    def to_scpi(value: object, key: str) -> str:
        match = _SOURCE_RE.match(value.strip()) if isinstance(value, str) else None
        if match is None or (match.group(3) is not None and not allow_off):
            raise _invalid(
                f"invalid {key} value: {value!r} (allowed: {allowed})",
                {"key": key, "value": value, "allowed": allowed},
            )
        if match.group(1) is not None:
            return f"CHANnel{match.group(1)}"
        return "D" + match.group(2)[1:] if match.group(2) else "OFF"

    def from_scpi(text: str) -> str:
        token = text.strip().upper()
        match = _SOURCE_RE.match(token)
        if match is None:
            raise _scpi_error(text)
        return f"CH{match.group(1)}" if match.group(1) is not None else token

    return to_scpi, from_scpi


def _item(path: str, converters: tuple[ToScpi, FromScpi]) -> DecodeItem:
    to_scpi, from_scpi = converters
    return DecodeItem(path=path, to_scpi=to_scpi, from_scpi=from_scpi)


def _threshold(type_token: str) -> DecodeItem:
    """`:BUS<n>:THReshold <value>,<type>` 形式の閾値項目。"""
    return DecodeItem(
        path="",
        to_scpi=lambda value, key: format_number(_number(value, key)),
        from_scpi=parse_nr3,
        threshold_type=type_token,
    )


# -- 共通の列挙 -------------------------------------------------------------

_ENDIAN = (("msb", "MSB"), ("lsb", "LSB"))
_POSNEG = (("positive", "POSitive"), ("negative", "NEGative"))
_SLOPE = (("rising", "POSitive"), ("falling", "NEGative"))
_HIGHLOW = (("high", "HIGH"), ("low", "LOW"))

#: パラレルのデータソース(ガイド3.4.10.1)。`d7_d0` 等はデジタルchのグループで、
#: **先に書かれた側がMSB**(`d7_d0` = D7がMSB・D0がLSBの8本)。`user` のときだけ
#: `bus_width` / `bit_sources` が有効になる(3.4.10.4-3.4.10.6 の Remark)。
_PARALLEL_BUS = (
    ("d7_d0", "D7D0"),
    ("d15_d8", "D15D8"),
    ("d15_d0", "D15D0"),
    ("d0_d7", "D0D7"),
    ("d8_d15", "D8D15"),
    ("d0_d15", "D0D15"),
    ("ch1", "CHANnel1"),
    ("ch2", "CHANnel2"),
    ("ch3", "CHANnel3"),
    ("ch4", "CHANnel4"),
    ("user", "USER"),
)


#: プロトコル → 意味的キー → SCPI項目(パスは `:BUS<n>:<プロトコル>` 相対)
DECODE_ITEMS: dict[str, dict[str, DecodeItem]] = {
    "uart": {
        "tx_source": _item(":TX", _source(allow_off=True)),
        "rx_source": _item(":RX", _source(allow_off=True)),
        "baud_bps": _item(":BAUD", _int_range(1, 20_000_000)),
        "data_bits": _item(":DBITs", _choice((5, 6, 7, 8, 9))),
        "parity": _item(
            ":PARity", _enum((("none", "NONE"), ("odd", "ODD"), ("even", "EVEN")))
        ),
        "stop_bits": _item(":SBITs", _choice((1, 1.5, 2))),
        "endian": _item(":ENDian", _enum(_ENDIAN)),
        "polarity": _item(":POLarity", _enum(_POSNEG)),
        "tx_threshold_v": _threshold("TX"),
        "rx_threshold_v": _threshold("RX"),
    },
    "i2c": {
        "scl_source": _item(":SCLK:SOURce", _source(allow_off=False)),
        "sda_source": _item(":SDA:SOURce", _source(allow_off=False)),
        "swap_sda_scl": _item(":EXCHange", _bool()),
        "address_bits": _item(":ADDBits", _choice((7, 8, 10))),
        "scl_threshold_v": _threshold("SCL"),
        "sda_threshold_v": _threshold("SDA"),
    },
    "spi": {
        "clk_source": _item(":SCLK:SOURce", _source(allow_off=False)),
        "clk_slope": _item(":SCLK:SLOPe", _enum(_SLOPE)),
        "mosi_source": _item(":MOSI:SOURce", _source(allow_off=True)),
        "miso_source": _item(":MISO:SOURce", _source(allow_off=True)),
        "cs_source": _item(":SS:SOURce", _source(allow_off=False)),
        "cs_polarity": _item(":SS:POLarity", _enum(_HIGHLOW)),
        "frame_mode": _item(":MODE", _enum((("cs", "CS"), ("timeout", "TIMeout")))),
        "timeout_s": _item(":TIMeout:TIME", _float_range(8e-9, 10.0)),
        "data_bits": _item(":DBITs", _int_range(4, 32)),
        "endian": _item(":ENDian", _enum(_ENDIAN)),
        # 非推奨の :MISO:POLarity / :MOSI:POLarity は使わない(バス共通の :POLarity)
        "polarity": _item(":POLarity", _enum(_HIGHLOW)),
        "clk_threshold_v": _threshold("CLK"),
        "mosi_threshold_v": _threshold("MOSI"),
        "miso_threshold_v": _threshold("MISO"),
        "cs_threshold_v": _threshold("CS"),
    },
    "can": {
        "source": _item(":SOURce", _source(allow_off=False)),
        "signal_type": _item(
            ":STYPe",
            _enum(
                (
                    ("tx", "TX"),
                    ("rx", "RX"),
                    ("canh", "CANH"),
                    ("canl", "CANL"),
                    ("differential", "DIFFerential"),
                )
            ),
        ),
        # CAN-FD(:FDBaud / :FDSPoint)はオプション必須のため扱わない
        "baud_bps": _item(":BAUD", _int_range(10_000, 5_000_000)),
        "sample_point_percent": _item(":SPOint", _int_range(10, 90)),
        "threshold_v": _threshold("CAN"),
    },
    "lin": {
        "source": _item(":SOURce", _source(allow_off=False)),
        "baud_bps": _item(":BAUD", _int_range(2400, 20_000_000)),
        "parity_enabled": _item(":PARity", _bool()),
        "standard": _item(
            ":STANdard", _enum((("v1x", "V1X"), ("v2x", "V2X"), ("mixed", "MIXed")))
        ),
        "threshold_v": _threshold("LIN"),
    },
    # **この並びがそのまま送信順**。`bus`(データソース)は `bus_width` の前提
    # なので必ず先に置く(ガイド3.4.10.4 の Remark。実機実測 mho98-phase4.md 5章)
    "parallel": {
        "clk_source": _item(":CLK", _source(allow_off=True)),
        "clk_slope": _item(":SLOPe", _enum(_SLOPE)),
        "bus": _item(":BUS", _enum(_PARALLEL_BUS)),
        # バス幅の上限はD0-D15相当の16。実機での上限は要確認(read-backで検出する)
        "bus_width": _item(":WIDTh", _int_range(1, 16)),
        "endian": _item(":ENDian", _enum(_ENDIAN)),
        "polarity": _item(":POLarity", _enum(_POSNEG)),
    },
}

#: プロトコル → 「ビット番号 → ソース」項目のキー(現状パラレルのみ)。
#: `:BITX <i>`(ビット選択)と `:SOURce <src>`(選択中ビットのソース)は**対**で
#: 1つの状態を成すため、1項目=1コマンドの `DecodeItem` には収まらない。値は
#: **添字がビット番号のリスト**で表し、ドライバが `bus_width` の後に走査する。
BIT_SOURCES: dict[str, str] = {"parallel": "bit_sources"}

#: 上記の対のSCPI断片(`:BUS<n>:<プロトコル>` 相対)
BIT_SELECT_PATH = ":BITX"
BIT_SOURCE_PATH = ":SOURce"

#: 同時にOFFにできないソースの組(デコード対象が無くなるため機器も受理しない)
EXCLUSIVE_SOURCES: dict[str, tuple[str, str]] = {
    "uart": ("tx_source", "rx_source"),
    "spi": ("mosi_source", "miso_source"),
}

# 共通項目(`:BUS<n>` 直下)。:MODE と :FORMat の対応表はプロファイル由来なので
# ここでは持たず、ドライバが `_enum` で組み立てる。
DISPLAY_ITEM = _item(":DISPlay", _bool())
EVENT_ITEM = _item(":EVENt", _bool())

#: プロファイルの対応表(`decode_protocols` / `decode_formats`)から
#: 共通項目の変換器を組み立てるための公開口
profile_enum = _enum


# -- イベントテーブル(`:BUS<n>:DATA?` のペイロード)-------------------------

#: 時刻列だけは意味を確定させる(秒へ変換する唯一の列)
TIME_COLUMN = "time_s"

#: エラー詳細に載せる生ペイロードの長さ上限(全文は載せない)
_RAW_PREFIX = 200

_NON_ALNUM = re.compile(r"[^a-z0-9]+")


def _column_name(text: str) -> str:
    """`Time` → `time_s`、`Tx/Rx` → `tx_rx`(列構成は機種・プロトコル依存)。"""
    name = _NON_ALNUM.sub("_", text.strip().lower()).strip("_")
    return TIME_COLUMN if name == "time" else name


def _cells(line: str) -> list[str]:
    """行をカンマで分割する。行末のカンマ1個ぶんの空セルだけ落とす。

    実機の行・ヘッダは末尾がカンマ(`Time,Tx/Rx,Data,Error,`)。空のセル自体は
    意味を持つ(エラー無しの `Error` 列)ので、まとめて落としてはならない。
    """
    cells = line.split(",")
    return cells[:-1] if line.endswith(",") else cells


def parse_event_table(payload: bytes) -> tuple[list[str], list[dict], str]:
    """イベントテーブルのペイロードを `(列名, 行, デコード種別トークン)` へ。

    構成は「種別トークン / ヘッダ行 / 行...」(MHO900・DHO800/900プログラミング
    ガイド 3.4)。**列構成はプロトコル依存でガイドに記載が無い**ため、ヘッダ行が
    与える列をそのまま採用する(スキーマを実装側に持たない)。
    """
    text = payload.decode("utf-8", "replace")
    lines = [line.strip() for line in text.splitlines()]
    lines = [line for line in lines if line]
    if not lines:
        return [], [], ""
    token = lines[0]
    if len(lines) < 2:
        return [], [], token

    header = lines[1]
    if "," not in header:
        raise ScopeError(
            ErrorCode.SCPI_ERROR,
            f"cannot interpret the event table header: {header!r}",
            {"raw": text[:_RAW_PREFIX]},
        )
    columns = [_column_name(cell) for cell in _cells(header)]

    events: list[dict] = []
    for line in lines[2:]:
        cells = _cells(line)
        row: dict[str, object] = {}
        for index, name in enumerate(columns):
            cell = cells[index] if index < len(cells) else ""
            row[name] = parse_eng_number(cell) if name == TIME_COLUMN else cell
        events.append(row)
    return columns, events, token
