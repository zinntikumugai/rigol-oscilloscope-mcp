"""意味的操作 → SCPI生成 → 応答解釈(プロファイル対応ドライバ)。

本層の責務は「LLMが扱う意味的な語彙」と「機種依存のSCPI方言」の橋渡しに限る。
安全ポリシー(承認フロー・確認トークン)は上位の control service の責務であり、
ここでは**プロファイルが宣言する能力の有無**だけを判定する。

規範(Requirements.md 7章 / device-profiles.md 4章):

- 未確認ニモニックは実機へ送らず UNSUPPORTED_FEATURE(送信コストが極めて高いため)
- 設定系は set → エラーキュー確認 → read-back を必須とし、applied値を返す
- API境界の単位はSI基本単位(V, s, Hz, Sa/s)
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from ..errors import ErrorCode, ScopeError
from ..models import ChannelState, IdnInfo, MeasurementResult, TimebaseState, TriggerState
from ..profiles import Profile
from .decode import (
    DECODE_ITEMS,
    DISPLAY_ITEM,
    EVENT_ITEM,
    EXCLUSIVE_SOURCES,
    DecodeItem,
    parse_event_table,
    profile_enum,
)
from .parsers import (
    format_number,
    from_scpi_impedance,
    from_scpi_slope,
    from_scpi_sweep,
    parse_bool,
    parse_coupling,
    parse_nr3,
    to_scpi_impedance,
    to_scpi_slope,
    to_scpi_sweep,
)
from .session import ScpiSession

# 測定の意味的名 → SI単位付きキー(Requirements.md 7.5)
MEASUREMENT_KEYS: dict[str, str] = {
    "frequency": "frequency_hz",
    "period": "period_s",
    "vpp": "vpp_v",
    "vmax": "vmax_v",
    "vmin": "vmin_v",
    "vavg": "vavg_v",
    "rms": "rms_v",
    "duty": "duty_ratio",
    "rise_time": "rise_time_s",
    "fall_time": "fall_time_s",
}

# 機器が「測定不能」を示すために返す番兵値(±9.9E37 前後)
INVALID_MEASUREMENT = 9.0e37

COUPLINGS = ("DC", "AC", "GND")
IMPEDANCES = ("1M", "50")

DEFAULT_SCREENSHOT_COMMAND = ":DISPlay:DATA?"
DEFAULT_SCREENSHOT_TIMEOUT_S = 30.0
# 帯域制限の「入」に用いる値は機種依存(MHO98は OFF/20M/100M/250M)。
# 既定値は置かない: プロファイルが宣言していない値を実機へ送らない。
BWLIMIT_OFF = "OFF"

# `:CHANnel<n>:IMPedance` 非対応機では問い合わせが無応答タイムアウトになるため、
# capability 未宣言のプロファイルでは送らず「不明」とする。
IMPEDANCE_UNKNOWN = "unknown"

DEFAULT_ANALOG_CHANNELS = 4
# :TRIGger:STATus? の生値がこれなら停止中(TD / WAIT / AUTO 等は動作中)
STOPPED_TRIGGER_STATUS = "STOP"
PREAMBLE_FIELDS = 10

_CHANNEL_RE = re.compile(r"^(?:CH|CHAN|CHANNEL)?\s*([0-9]+)$", re.IGNORECASE)
# 非チャンネルのトリガソース。読み値をそのまま書き戻せるよう表記は1つに固定する
# (`ACL` / `ACLine` はどちらも `ACLINE`)。
_NON_CHANNEL_SOURCE_RE = re.compile(r"^(?:EXT5?|ACL(?:INE)?|D(?:[0-9]|1[0-5]))$")
_NUMBER_RE = re.compile(r"^[+-]?(?:\d+\.?\d*|\.\d+)(?:[eE][+-]?\d+)?$")


def normalize_channel(value: str) -> str:
    """`CHANnel1` / `chan1` / `1` → `CH1`。

    表記ゆれの吸収のみを行う公開API。解釈できない値はそのまま返す
    (存在するチャンネルかどうかの検証は `ScopeDriver` の責務)。
    """
    match = _CHANNEL_RE.match(value.strip()) if isinstance(value, str) else None
    return f"CH{int(match.group(1))}" if match else value


def _invalid(message: str, detail: dict) -> ScopeError:
    return ScopeError(ErrorCode.INVALID_PARAMETER, message, detail)


def _unsupported(message: str, detail: dict) -> ScopeError:
    return ScopeError(ErrorCode.UNSUPPORTED_FEATURE, message, detail)


def _optional_number(text: str) -> float | None:
    """数値応答を返す。`AUTO` 等の非数値は「取得できない」として None。"""
    return parse_nr3(text) if _NUMBER_RE.match(text.strip()) else None


@dataclass(frozen=True)
class WaveformPreamble:
    """`:WAVeform:PREamble?` の10要素。生値をそのまま保持する。"""

    format: int
    type: int
    points: int
    count: int
    xincrement: float
    xorigin: float
    xreference: float
    yincrement: float
    yorigin: float
    yreference: float

    @classmethod
    def parse(cls, text: str) -> WaveformPreamble:
        parts = [part.strip() for part in text.strip().split(",")]
        if len(parts) != PREAMBLE_FIELDS:
            raise ScopeError(
                ErrorCode.SCPI_ERROR,
                f"preamble does not have {PREAMBLE_FIELDS} fields: {len(parts)}",
                {"raw": text, "count": len(parts)},
            )
        try:
            numbers = [float(part) for part in parts]
        except ValueError:
            raise ScopeError(
                ErrorCode.SCPI_ERROR,
                f"cannot interpret preamble as numbers: {text!r}",
                {"raw": text},
            ) from None
        return cls(
            format=int(numbers[0]),
            type=int(numbers[1]),
            points=int(numbers[2]),
            count=int(numbers[3]),
            xincrement=numbers[4],
            xorigin=numbers[5],
            xreference=numbers[6],
            yincrement=numbers[7],
            yorigin=numbers[8],
            yreference=numbers[9],
        )


@dataclass(frozen=True)
class WaveformRaw:
    """生波形(BYTE形式)。電圧・時間への換算は上位の責務。"""

    preamble: WaveformPreamble
    data: bytes


class ScopeDriver:
    """プロファイルに従ってSCPIを生成・解釈するドライバ。"""

    def __init__(self, session: ScpiSession, profile: Profile) -> None:
        self.session = session
        self.profile = profile
        self._options: dict[str, bool | None] | None = None

    @property
    def analog_channels(self) -> int:
        """この機種のアナログチャンネル数(プロファイル未宣言なら既定値)。"""
        count = self.profile.capabilities.get("analog_channels", DEFAULT_ANALOG_CHANNELS)
        return count if isinstance(count, int) else DEFAULT_ANALOG_CHANNELS

    # -- 内部: 検証 -------------------------------------------------------

    def _channel_number(self, channel: str) -> int:
        """`CH1` / `CHANnel1` / `1` → 1。プロファイルのチャンネル数で範囲検証する。"""
        if not isinstance(channel, str):
            raise _invalid(f"channel name is not a string: {channel!r}", {"channel": channel})
        match = _CHANNEL_RE.match(channel.strip())
        if match is None:
            raise _invalid(
                f"cannot interpret channel name: {channel!r} (e.g. 'CH1')",
                {"channel": channel},
            )
        number = int(match.group(1))
        available = self.analog_channels
        if not 1 <= number <= available:
            raise _invalid(
                f"channel {channel} does not exist (this model has CH1-CH{available})",
                {"channel": channel, "analog_channels": available},
            )
        return number

    def _require(self, capability: str, what: str) -> None:
        if not self.profile.supports(capability):
            raise _unsupported(
                f"this model's profile does not support {what}",
                {"capability": capability, "profile": self.profile.name},
            )

    def _dialect(self, key: str, default: str) -> str:
        value = self.profile.dialect.get(key, default)
        return value if isinstance(value, str) else default

    def _required_dialect(self, key: str, what: str) -> str:
        """プロファイル未宣言のニモニック/引数は実機へ送らない。"""
        value = self.profile.dialect.get(key)
        if not isinstance(value, str) or not value:
            raise _unsupported(
                f"this model's profile does not declare a value to use for {what}",
                {"dialect": key, "profile": self.profile.name},
            )
        return value

    # -- 識別 -------------------------------------------------------------

    def identify(self) -> IdnInfo:
        """`*IDN?` を4要素へ分解する。"""
        response = self.session.query("*IDN?")
        parts = [part.strip() for part in response.split(",")]
        if len(parts) != 4:
            raise ScopeError(
                ErrorCode.SCPI_ERROR,
                f"*IDN? response does not have 4 fields: {response!r}",
                {"raw": response},
            )
        return IdnInfo(
            manufacturer=parts[0], model=parts[1], serial=parts[2], firmware=parts[3]
        )

    def installed_options(self) -> dict[str, bool | None]:
        """導入済みオプションを意味的な名前で返す(不明な項目は None)。

        プロファイルが `option_query` / `option_types` を宣言していない機種では
        1コマンドも送らず UNSUPPORTED_FEATURE(`:SYSTem:OPTion:*` はMHO900専用で、
        未定義ヘッダを送るとSCPIサーバーが沈黙するため)。

        結果はインスタンスにキャッシュする。ライセンスは接続中に変わらない
        (適用は再起動を伴う)。接続ごとに新しいdriverが作られるので、再接続が
        そのままキャッシュ無効化になる。
        """
        if self._options is not None:
            return self._options

        query = self._required_dialect("option_query", "querying installed options")
        types = self.profile.dialect.get("option_types")
        if not isinstance(types, dict) or not types:
            raise _unsupported(
                "this model's profile does not declare a value to use for "
                "querying installed options",
                {"dialect": "option_types", "profile": self.profile.name},
            )

        options: dict[str, bool | None] = {}
        for name, token in types.items():
            try:
                response = self.session.query(f"{query} {token}").strip()
            except ScopeError:
                # 1件の失敗で他の結果まで失わない(不明は None)
                response = ""
            options[name] = {"1": True, "0": False}.get(response)
        self._options = options
        return options

    # -- 取得(READ_ONLY)-------------------------------------------------

    def get_channel(self, channel: str) -> ChannelState:
        number = self._channel_number(channel)
        query = self.session.query
        return ChannelState(
            channel=f"CH{number}",
            enabled=parse_bool(query(f":CHANnel{number}:DISPlay?")),
            scale_v_per_div=parse_nr3(query(f":CHANnel{number}:SCALe?")),
            offset_v=parse_nr3(query(f":CHANnel{number}:OFFSet?")),
            coupling=parse_coupling(query(f":CHANnel{number}:COUPling?")),
            impedance=(
                from_scpi_impedance(query(f":CHANnel{number}:IMPedance?"))
                if self.profile.supports("impedance_control")
                else IMPEDANCE_UNKNOWN
            ),
            probe_ratio=parse_nr3(query(f":CHANnel{number}:PROBe?")),
            bandwidth_limit=self._parse_bwlimit(query(f":CHANnel{number}:BWLimit?")),
        )

    @staticmethod
    def _parse_bwlimit(text: str) -> bool:
        """`OFF` 以外(`20M` 等)は帯域制限が有効。"""
        return text.strip().upper() not in (BWLIMIT_OFF, "0")

    def get_timebase(self) -> TimebaseState:
        query = self.session.query
        return TimebaseState(
            scale_s_per_div=parse_nr3(query(":TIMebase:MAIN:SCALe?")),
            position_s=parse_nr3(query(":TIMebase:MAIN:OFFSet?")),
            sample_rate_sa_per_s=_optional_number(query(":ACQuire:SRATe?")),
            memory_depth=_optional_number(query(":ACQuire:MDEPth?")),
        )

    def get_trigger(self) -> TriggerState:
        query = self.session.query
        return TriggerState(
            type="edge",
            source=self._normalize_source(query(":TRIGger:EDGE:SOURce?")),
            level_v=parse_nr3(query(":TRIGger:EDGE:LEVel?")),
            slope=from_scpi_slope(query(":TRIGger:EDGE:SLOPe?")),
            sweep_mode=from_scpi_sweep(query(":TRIGger:SWEep?")),
            status=self.get_trigger_status(),
        )

    def get_trigger_status(self) -> str:
        """`TD` / `WAIT` / `STOP` 等の生値をそのまま返す。"""
        return self.session.query(":TRIGger:STATus?").strip().upper()

    @staticmethod
    def _normalize_source(text: str) -> str:
        """`CHAN1` → `CH1`、`ACL` → `ACLINE`。それ以外は生値を大文字で返す。"""
        token = text.strip().upper()
        if _CHANNEL_RE.match(token):
            return normalize_channel(token)
        return "ACLINE" if token in ("ACL", "ACLINE") else token

    def _trigger_source(self, source: str) -> str:
        """トリガソース → 送信トークン。CH系のみチャンネル数で範囲検証する。

        `get_trigger` の返す正規化形(`CH1` / `EXT` / `ACLINE` / `D0`)をそのまま
        受理し、往復(読んだ設定の書き戻し)が成立する。
        """
        if not isinstance(source, str):
            raise _invalid(f"trigger source is not a string: {source!r}", {"source": source})
        token = self._normalize_source(source)
        if _CHANNEL_RE.match(token):
            return f"CHANnel{self._channel_number(token)}"
        if _NON_CHANNEL_SOURCE_RE.match(token):
            return token
        raise _invalid(
            f"cannot interpret trigger source: {source!r} (e.g. 'CH1', 'EXT', 'ACLINE', 'D0')",
            {"source": source},
        )

    # -- 測定 -------------------------------------------------------------

    def measure(self, channel: str, names: list[str]) -> list[MeasurementResult]:
        """指定の測定項目を読む。未確認ニモニックは実機へ送らない。"""
        number = self._channel_number(channel)

        # 1件でも未対応なら送信前に失敗する(部分的な送信でキューを汚さない)
        plan: list[tuple[str, str, str]] = []
        for name in names:
            mnemonic = self.profile.measurement_mnemonic(name)
            key = MEASUREMENT_KEYS.get(name)
            if mnemonic is None or key is None:
                raise _unsupported(
                    f"measurement item '{name}' is unverified in this model's profile",
                    {"measurement": name, "profile": self.profile.name},
                )
            plan.append((name, key, mnemonic))

        results: list[MeasurementResult] = []
        for name, key, mnemonic in plan:
            response = self.session.query(
                f":MEASure:ITEM? {mnemonic},CHANnel{number}"
            )
            value = parse_nr3(response)
            if abs(value) >= INVALID_MEASUREMENT:
                results.append(MeasurementResult(name, key, None, "unknown"))
            else:
                results.append(MeasurementResult(name, key, value, "valid"))
        return results

    # -- 画面・波形 -------------------------------------------------------

    def capture_screenshot_bytes(self) -> bytes:
        self._require("screenshot", "screen capture")
        command = self._dialect("screenshot_command", DEFAULT_SCREENSHOT_COMMAND)
        # 画像は約97KB。通常の問い合わせ用タイムアウトでは足りず接続破棄になる。
        timeout_s = self.profile.dialect.get("screenshot_timeout_s", DEFAULT_SCREENSHOT_TIMEOUT_S)
        return self.session.query_binary(command, timeout_s=float(timeout_s))

    def read_waveform(self, channel: str, max_points: int | None = None) -> WaveformRaw:
        self._require("waveform_download", "waveform data download")
        number = self._channel_number(channel)

        self.session.write_checked(f":WAVeform:SOURce CHANnel{number}")
        self.session.write_checked(":WAVeform:MODE NORMal")
        self.session.write_checked(":WAVeform:FORMat BYTE")

        preamble = WaveformPreamble.parse(self.session.query(":WAVeform:PREamble?"))

        if max_points is not None:
            if max_points < 1:
                raise _invalid(
                    f"max_points must be 1 or greater: {max_points}",
                    {"max_points": max_points},
                )
            stop = min(preamble.points, max_points)
            self.session.write_checked(":WAVeform:STARt 1")
            self.session.write_checked(f":WAVeform:STOP {stop}")

        return WaveformRaw(preamble=preamble, data=self.session.query_binary(":WAVeform:DATA?"))

    # -- 設定(SAFE_WRITE)------------------------------------------------

    def set_channel_enabled(self, channel: str, enabled: bool) -> bool:
        number = self._channel_number(channel)
        readback = self.session.set_and_verify(
            f":CHANnel{number}:DISPlay {'ON' if enabled else 'OFF'}",
            f":CHANnel{number}:DISPlay?",
        )
        return parse_bool(readback)

    def set_channel_scale(self, channel: str, v_per_div: float) -> float:
        number = self._channel_number(channel)
        readback = self.session.set_and_verify(
            f":CHANnel{number}:SCALe {format_number(v_per_div)}",
            f":CHANnel{number}:SCALe?",
        )
        return parse_nr3(readback)

    def set_channel_offset(self, channel: str, offset_v: float) -> float:
        number = self._channel_number(channel)
        readback = self.session.set_and_verify(
            f":CHANnel{number}:OFFSet {format_number(offset_v)}",
            f":CHANnel{number}:OFFSet?",
        )
        return parse_nr3(readback)

    def set_channel_coupling(self, channel: str, coupling: str) -> str:
        number = self._channel_number(channel)
        value = self._validate_choice(coupling, COUPLINGS, "coupling")
        readback = self.session.set_and_verify(
            f":CHANnel{number}:COUPling {value}", f":CHANnel{number}:COUPling?"
        )
        return parse_coupling(readback)

    def set_channel_probe_ratio(self, channel: str, ratio: float) -> float:
        number = self._channel_number(channel)
        self._validate_probe_ratio(ratio)
        readback = self.session.set_and_verify(
            f":CHANnel{number}:PROBe {format_number(ratio)}", f":CHANnel{number}:PROBe?"
        )
        return parse_nr3(readback)

    def _validate_probe_ratio(self, ratio: float) -> None:
        """limits に列挙があれば検証し、無ければ実機の read-back に委ねる。"""
        allowed = self.profile.limits.get("probe_ratio")
        if not isinstance(allowed, list) or not allowed:
            return
        if not any(float(item) == float(ratio) for item in allowed):
            raise _invalid(
                f"probe attenuation ratio {ratio} is not selectable on this model",
                {"probe_ratio": ratio, "allowed": allowed},
            )

    def set_channel_bwlimit(self, channel: str, enabled: bool) -> bool:
        number = self._channel_number(channel)
        # OFF は全機種共通。ON 側の値は機種依存なので宣言が無ければ送らない。
        value = self._required_dialect("bwlimit_on", "enabling the bandwidth limit") if enabled else BWLIMIT_OFF
        readback = self.session.set_and_verify(
            f":CHANnel{number}:BWLimit {value}", f":CHANnel{number}:BWLimit?"
        )
        return self._parse_bwlimit(readback)

    def set_channel_impedance(self, channel: str, impedance: str) -> str:
        number = self._channel_number(channel)
        value = self._validate_choice(impedance, IMPEDANCES, "input impedance")
        # IMPedance ニモニック自体が未確認なら "1M" でも送らない
        self._require("impedance_control", "setting the input impedance")
        if value == "50":
            self._require("impedance_50ohm", "50 ohm input")
        readback = self.session.set_and_verify(
            f":CHANnel{number}:IMPedance {to_scpi_impedance(value)}",
            f":CHANnel{number}:IMPedance?",
        )
        return from_scpi_impedance(readback)

    @staticmethod
    def _validate_choice(value: str, allowed: tuple[str, ...], what: str) -> str:
        if not isinstance(value, str):
            raise _invalid(f"{what} is not a string: {value!r}", {"value": value})
        token = value.strip().upper()
        if token not in allowed:
            raise _invalid(
                f"invalid {what} value: {value!r} (allowed: {list(allowed)})",
                {"value": value, "allowed": list(allowed)},
            )
        return token

    def set_timebase_scale(self, s_per_div: float) -> float:
        readback = self.session.set_and_verify(
            f":TIMebase:MAIN:SCALe {format_number(s_per_div)}", ":TIMebase:MAIN:SCALe?"
        )
        return parse_nr3(readback)

    def set_timebase_position(self, position_s: float) -> float:
        readback = self.session.set_and_verify(
            f":TIMebase:MAIN:OFFSet {format_number(position_s)}", ":TIMebase:MAIN:OFFSet?"
        )
        return parse_nr3(readback)

    # -- トリガ -----------------------------------------------------------

    def set_trigger_edge(
        self,
        source: str | None = None,
        level_v: float | None = None,
        slope: str | None = None,
        sweep_mode: str | None = None,
    ) -> TriggerState:
        """エッジトリガを設定する。None の項目は変更しない。

        引数の検証を全て済ませてから送信する(途中で失敗して機器が中途半端な
        状態になるのを避ける)。
        """
        commands: list[str] = [":TRIGger:MODE EDGE"]
        if source is not None:
            commands.append(f":TRIGger:EDGE:SOURce {self._trigger_source(source)}")
        if level_v is not None:
            commands.append(f":TRIGger:EDGE:LEVel {format_number(level_v)}")
        if slope is not None:
            commands.append(f":TRIGger:EDGE:SLOPe {to_scpi_slope(slope)}")
        if sweep_mode is not None:
            commands.append(f":TRIGger:SWEep {to_scpi_sweep(sweep_mode)}")

        for command in commands:
            self.session.write_checked(command)
        return self.get_trigger()

    # -- シリアルデコード(tools.md 6章)-----------------------------------

    def _decode_protocols(self) -> dict[str, str]:
        """このプロファイルが宣言する プロトコル名 → SCPIニモニック。

        宣言の不在がそのままゲート(オプション必須プロトコルは載せない)。
        変換表(decode.py)を持たないプロトコルは宣言されていても扱わない。
        """
        self._require("protocol_decode", "protocol decode")
        declared = self.profile.dialect.get("decode_protocols")
        protocols = (
            {name: value for name, value in declared.items() if name in DECODE_ITEMS}
            if isinstance(declared, dict)
            else {}
        )
        if not protocols:
            raise _unsupported(
                "this model's profile does not declare a value to use for "
                "protocol decode",
                {"dialect": "decode_protocols", "profile": self.profile.name},
            )
        return protocols

    def _decode_bus(self, bus: int) -> int:
        count = self.profile.capabilities.get("decode_buses")
        if not isinstance(count, int) or count < 1:
            raise _unsupported(
                "this model's profile does not declare how many decode buses exist",
                {"capability": "decode_buses", "profile": self.profile.name},
            )
        if isinstance(bus, bool) or not isinstance(bus, int) or not 1 <= bus <= count:
            raise _invalid(
                f"decode bus {bus!r} does not exist (this model has bus 1-{count})",
                {"bus": bus, "decode_buses": count},
            )
        return bus

    def _decode_format(self) -> tuple[object, object]:
        """`:FORMat` の 意味的な値 ⇔ トークン 変換器。"""
        formats = self.profile.dialect.get("decode_formats")
        if not isinstance(formats, dict) or not formats:
            raise _unsupported(
                "this model's profile does not declare a value to use for "
                "the decode display format",
                {"dialect": "decode_formats", "profile": self.profile.name},
            )
        return profile_enum(tuple(formats.items()))

    def _decode_command(
        self, prefix: str, mnemonic: str, key: str, item: DecodeItem, value: object
    ) -> tuple[str, str]:
        """1項目の (設定コマンド, read-back問い合わせ)。値の検証もここで行う。"""
        token = item.to_scpi(value, key)
        if item.threshold_type is not None:
            # 閾値だけは形が不規則(値と種別をカンマで並べ、問い合わせは種別を引数に取る)
            return (
                f"{prefix}:THReshold {token},{item.threshold_type}",
                f"{prefix}:THReshold? {item.threshold_type}",
            )
        return (
            f"{prefix}:{mnemonic}{item.path} {token}",
            f"{prefix}:{mnemonic}{item.path}?",
        )

    def configure_decode(
        self,
        bus: int,
        protocol: str,
        *,
        enabled: bool | None = None,
        event_table: bool | None = None,
        data_format: str | None = None,
        settings: dict | None = None,
    ) -> dict:
        """デコードバスを設定し、read-backした適用値を返す。

        **全ての検証を送信前に済ませる**(不正トークン1発で実機のSCPIサーバーが
        沈黙するため)。送信順は `:MODE` → 表示形式 → プロトコル別設定 →
        `:DISPlay` → `:EVENt` に固定する(イベントテーブルの有効化には
        バスの表示が先に必要、というガイドの制約に従う)。
        """
        protocols = self._decode_protocols()
        name = protocol.strip().lower() if isinstance(protocol, str) else protocol
        if name not in protocols:
            raise _unsupported(
                f"protocol decode '{protocol}' is not supported on this model "
                f"(supported: {sorted(protocols)})",
                {"protocol": protocol, "supported": sorted(protocols)},
            )
        number = self._decode_bus(bus)
        items = DECODE_ITEMS[name]

        if settings is None:
            settings = {}
        elif not isinstance(settings, dict):
            raise _invalid(
                f"settings is not an object: {settings!r}", {"settings": settings}
            )
        unknown = [key for key in settings if key not in items]
        if unknown:
            raise _invalid(
                f"unknown setting for protocol '{name}': {sorted(unknown)}",
                {"protocol": name, "unknown": sorted(unknown), "allowed": sorted(items)},
            )
        self._reject_all_sources_off(name, items, settings)

        prefix = f":BUS{number}"
        mnemonic = protocols[name]
        mode_to, mode_from = profile_enum(tuple(protocols.items()))

        # (返却キー, 設定コマンド, read-back問い合わせ, 応答変換, settings配下か)
        plan: list[tuple[str, str, str, object, bool]] = [
            (
                "protocol",
                f"{prefix}:MODE {mode_to(name, 'protocol')}",
                f"{prefix}:MODE?",
                mode_from,
                False,
            )
        ]
        if data_format is not None:
            format_to, format_from = self._decode_format()
            plan.append(
                (
                    "data_format",
                    f"{prefix}:FORMat {format_to(data_format, 'data_format')}",
                    f"{prefix}:FORMat?",
                    format_from,
                    False,
                )
            )
        for key, value in settings.items():
            item = items[key]
            set_cmd, query = self._decode_command(prefix, mnemonic, key, item, value)
            plan.append((key, set_cmd, query, item.from_scpi, True))
        for key, value, item in (
            ("enabled", enabled, DISPLAY_ITEM),
            ("event_table", event_table, EVENT_ITEM),
        ):
            if value is not None:
                plan.append(
                    (
                        key,
                        f"{prefix}{item.path} {item.to_scpi(value, key)}",
                        f"{prefix}{item.path}?",
                        item.from_scpi,
                        False,
                    )
                )

        applied: dict[str, object] = {"bus": number}
        applied_settings: dict[str, object] = {}
        for key, set_cmd, query, from_scpi, is_setting in plan:
            value = from_scpi(self.session.set_and_verify(set_cmd, query))
            if is_setting:
                applied_settings[key] = value
            else:
                applied[key] = value
        applied["settings"] = applied_settings
        return applied

    @staticmethod
    def _reject_all_sources_off(
        protocol: str, items: dict[str, DecodeItem], settings: dict
    ) -> None:
        """デコード対象が1本も残らない指定を拒否する(機器も受理しない)。"""
        pair = EXCLUSIVE_SOURCES.get(protocol)
        if pair is None or not all(key in settings for key in pair):
            return
        if all(items[key].to_scpi(settings[key], key) == "OFF" for key in pair):
            raise _invalid(
                f"{pair[0]} and {pair[1]} cannot both be off "
                "(there would be nothing to decode)",
                {"protocol": protocol, "sources": list(pair)},
            )

    def get_decode_config(self, bus: int) -> dict:
        """デコードバスの現在設定を意味的なキーで返す。"""
        protocols = self._decode_protocols()
        number = self._decode_bus(bus)
        prefix = f":BUS{number}"
        query = self.session.query

        _, mode_from = profile_enum(tuple(protocols.items()))
        raw_mode = query(f"{prefix}:MODE?").strip()
        try:
            protocol = mode_from(raw_mode)
        except ScopeError:
            # オプション必須プロトコル(IIS等)に設定されているバス。生の名前だけ
            # 返し、配下の項目には触れない(未確認ニモニックを送らない)
            protocol = raw_mode.lower()

        _, format_from = self._decode_format()
        config: dict[str, object] = {
            "bus": number,
            "protocol": protocol,
            "enabled": DISPLAY_ITEM.from_scpi(query(f"{prefix}{DISPLAY_ITEM.path}?")),
            "event_table": EVENT_ITEM.from_scpi(query(f"{prefix}{EVENT_ITEM.path}?")),
            "data_format": format_from(query(f"{prefix}:FORMat?")),
        }
        items = DECODE_ITEMS.get(protocol, {})
        mnemonic = protocols.get(protocol, "")
        settings: dict[str, object] = {}
        for key, item in items.items():
            if item.threshold_type is not None:
                response = query(f"{prefix}:THReshold? {item.threshold_type}")
            else:
                response = query(f"{prefix}:{mnemonic}{item.path}?")
            settings[key] = item.from_scpi(response)
        config["settings"] = settings
        return config

    def get_decode_events(self, bus: int) -> dict:
        """イベントテーブル(`:BUS<n>:DATA?`)を読む。**書き込みは一切しない**。

        応答はTMCブロックで、中身は「デコード種別トークン / ヘッダ行 / 行...」の
        改行区切りCSV(MHO900・DHO800/900プログラミングガイド 3.4)。列構成は
        プロトコル依存でガイドに記載が無いため、**列名は解釈せずそのまま返す**
        (時刻列だけは `time_s` として秒へ変換する)。
        """
        protocols = self._decode_protocols()
        number = self._decode_bus(bus)
        prefix = f":BUS{number}"
        query = self.session.query

        _, mode_from = profile_enum(tuple(protocols.items()))
        raw_mode = query(f"{prefix}:MODE?").strip()
        try:
            protocol = mode_from(raw_mode)
        except ScopeError:
            protocol = raw_mode.lower()

        empty: dict[str, object] = {
            "bus": number,
            "protocol": protocol,
            "columns": [],
            "events": [],
        }
        # 未表示・イベントテーブル未表示のときの `:DATA?` の挙動は未確認なので送らない
        if not DISPLAY_ITEM.from_scpi(query(f"{prefix}{DISPLAY_ITEM.path}?")):
            return {
                **empty,
                "warnings": [
                    f"decode bus {number} is not enabled; "
                    f"call configure_decode(bus={number}, enabled=true) first"
                ],
            }
        if not EVENT_ITEM.from_scpi(query(f"{prefix}{EVENT_ITEM.path}?")):
            return {
                **empty,
                "warnings": [
                    f"the event table of decode bus {number} is off; "
                    f"call configure_decode(bus={number}, event_table=true) first"
                ],
            }

        warnings: list[str] = []
        if self.get_trigger_status() != STOPPED_TRIGGER_STATUS:
            # 停止は上位(stop Tool)の判断。ここでは read-only を崩さない
            warnings.append(
                "acquisition is running; the event table is only a snapshot and "
                "may change between reads (stop the acquisition for a stable table)"
            )

        command = f"{prefix}:DATA?"
        payload = self.session.query_binary(command)
        # 値が返ってもエラーが積まれることがある(mho98-unlicensed.md 4章)
        self.session.check_error(command)

        columns, events, token = parse_event_table(payload)
        if token:
            try:
                protocol = mode_from(token)
            except ScopeError:
                protocol = token.lower()
                warnings.append(
                    f"the event table reports an unknown decoding type: {token!r}"
                )
        return {
            "bus": number,
            "protocol": protocol,
            "columns": columns,
            "events": events,
            "warnings": warnings,
        }

    # -- Acquisition ------------------------------------------------------

    def run(self) -> None:
        self.session.write_checked(":RUN")

    def stop(self) -> None:
        self.session.write_checked(":STOP")

    def single(self) -> None:
        self.session.write_checked(":SINGle")

    def autoset(self) -> None:
        """オートスケール。信号系統を大きく変えるため、承認は上位の責務。"""
        self.session.write_checked(":AUToscale")
