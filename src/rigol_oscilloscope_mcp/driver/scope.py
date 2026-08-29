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

import math
import re
from collections.abc import Callable
from dataclasses import dataclass

from ..errors import ErrorCode, ScopeError
from ..models import ChannelState, IdnInfo, MeasurementResult, TimebaseState, TriggerState
from ..profiles import Profile
from .decode import (
    BIT_SELECT_PATH,
    BIT_SOURCE_PATH,
    BIT_SOURCES,
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
    parse_fft_peaks,
    parse_histogram_result,
    parse_nr3,
    to_scpi_impedance,
    to_scpi_slope,
    to_scpi_sweep,
)
from .session import ScpiSession

# 測定の意味的名 → SI単位付きキー(Requirements.md 7.5)。
# ガイド3.17.2 の <item> 41トークンに1対1で対応する。SCPIニモニックは
# 機種プロファイルの `dialect.measurement_items` が持つ(綴りは機種差がある)。
MEASUREMENT_KEYS: dict[str, str] = {
    # 時間・周期系(ガイド "Horizontal Parameters")
    "frequency": "frequency_hz",
    "period": "period_s",
    "rise_time": "rise_time_s",
    "fall_time": "fall_time_s",
    "pulse_width_pos": "pulse_width_pos_s",
    "pulse_width_neg": "pulse_width_neg_s",
    "duty": "duty_ratio",
    "duty_neg": "duty_neg_ratio",
    "time_at_vmax": "time_at_vmax_s",
    "time_at_vmin": "time_at_vmin_s",
    # 振幅・電圧系(ガイド "Vertical Parameters")
    "vpp": "vpp_v",
    "vmax": "vmax_v",
    "vmin": "vmin_v",
    "vtop": "vtop_v",
    "vbase": "vbase_v",
    "vamp": "vamp_v",
    "vupper": "vupper_v",
    "vmid": "vmid_v",
    "vlower": "vlower_v",
    "vavg": "vavg_v",
    "rms": "rms_v",
    "period_rms": "period_rms_v",
    "ac_rms": "ac_rms_v",
    "overshoot": "overshoot_ratio",
    "preshoot": "preshoot_ratio",
    # 面積・スルーレート系(ガイド "Other Parameters")
    "area": "area_vs",
    "period_area": "period_area_vs",
    "slew_rate_pos": "slew_rate_pos_v_per_s",
    "slew_rate_neg": "slew_rate_neg_v_per_s",
    # カウント系
    "pulses_pos": "pulses_pos_count",
    "pulses_neg": "pulses_neg_count",
    "edges_pos": "edges_pos_count",
    "edges_neg": "edges_neg_count",
    # 位相・遅延系(**2ソース必須**。DUAL_SOURCE_MEASUREMENTS を参照)
    "delay_rise_rise": "delay_rise_rise_s",
    "delay_rise_fall": "delay_rise_fall_s",
    "delay_fall_rise": "delay_fall_rise_s",
    "delay_fall_fall": "delay_fall_fall_s",
    "phase_rise_rise": "phase_rise_rise_deg",
    "phase_rise_fall": "phase_rise_fall_deg",
    "phase_fall_rise": "phase_fall_rise_deg",
    "phase_fall_fall": "phase_fall_fall_deg",
}

#: ソースを2つ取る測定項目(ガイド3.17.2 の `<item>[,<src>[,<src>]]`)。
#: 第2ソースを省略すると機器は「最後に選んだソース」を使う(ガイドのRemark)。
#: 呼び出し側から見て結果が予測できないため、**省略は送信前に拒否する**。
DUAL_SOURCE_MEASUREMENTS: frozenset[str] = frozenset(
    name
    for name in MEASUREMENT_KEYS
    if name.startswith(("delay_", "phase_"))
)

#: 測定の前提設定(ガイド3.17)。**この並びがそのまま送信順**。
#: - `threshold_default`(既定へ戻す)を先頭に置くのは、同じ呼び出しで指定された
#:   しきい値を後から潰さないため(M3の `:REFerence:RESet` と同じ原則)
#: - `threshold_type` は `SETup:MAX/MID/MIN` より前(値域が type に依存する)
#: - `area` は `CREGion:*` より前(区間の指定先を決める)
#: - `amp_type` は `AMP:MANual:TOP/BASE` より前
#: - `statistics_reset`(履歴クリア)は末尾。設定を変えてから取り直す
_MEASURE_SETUP_ITEMS: tuple[tuple[str, str, object], ...] = (
    ("source", ":SOURce", "lsource"),
    ("threshold_source", ":THReshold:SOURce", "achannel"),
    ("threshold_type", ":THReshold:TYPE", ("enum", "measure_threshold_types", "the measurement threshold type")),
    ("threshold_max", ":SETup:MAX", "threshold"),
    ("threshold_mid", ":SETup:MID", "threshold"),
    ("threshold_min", ":SETup:MIN", "threshold"),
    ("area", ":AREA", ("enum", "measure_areas", "the measurement range")),
    ("region_ax_s", ":CREGion:CAX", "number"),
    ("region_bx_s", ":CREGion:CBX", "number"),
    ("region_linked", ":CREGion:CABX", "bool"),
    ("amp_type", ":AMP:TYPE", ("enum", "measure_amp_types", "the amplitude method")),
    ("amp_top", ":AMP:MANual:TOP", ("enum", "measure_amp_methods", "the manual amplitude top method")),
    ("amp_base", ":AMP:MANual:BASE", ("enum", "measure_amp_methods", "the manual amplitude base method")),
    ("statistics_enabled", ":STATistic:DISPlay", "bool"),
    ("statistics_count", ":STATistic:COUNt", ("int", 2, 100_000)),
)
_MEASURE_PREFIX = ":MEASure"
#: read-back を持たない一発動作(ガイド3.17.18 / 3.17.7)
_MEASURE_THRESHOLD_DEFAULT = ":MEASure:THReshold:DEFault"
_MEASURE_STATISTIC_RESET = ":MEASure:STATistic:RESet"
_MEASURE_STATISTIC_ITEM = ":MEASure:STATistic:ITEM"

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
#: トリガ種別ごとの項目表(ガイド3.27)。`(意味的キー, SCPIパス, 種別)`。
#: **キー名が `source` / `level_v` / `slope` のものは TriggerState の最上位にも出す**
#: (既存の返却形を保つため。2ソース・2レベルの種別は最上位が None になる)。
_TRIGGER_EDGE_ITEMS: tuple[tuple[str, str, object], ...] = (
    ("source", ":SOURce", "tsource"),
    ("level_v", ":LEVel", "number"),
    ("slope", ":SLOPe", ("enum", "trigger_slopes", "the edge trigger slope")),
)
_TRIGGER_PULSE_ITEMS: tuple[tuple[str, str, object], ...] = (
    ("source", ":SOURce", "tsource"),
    ("level_v", ":LEVel", "number"),
    ("polarity", ":POLarity", ("enum", "trigger_polarities", "the pulse polarity")),
    ("when", ":WHEN", ("enum", "trigger_pulse_when", "the pulse width condition")),
    ("upper_width_s", ":UWIDth", "number"),
    ("lower_width_s", ":LWIDth", "number"),
)
_TRIGGER_SLOPE_ITEMS: tuple[tuple[str, str, object], ...] = (
    ("source", ":SOURce", "tsource"),
    ("polarity", ":POLarity", ("enum", "trigger_polarities", "the slope polarity")),
    ("when", ":WHEN", ("enum", "trigger_pulse_when", "the slope time condition")),
    ("upper_time_s", ":TUPPer", "number"),
    ("lower_time_s", ":TLOWer", "number"),
    ("window", ":WINDow", ("enum", "trigger_slope_windows", "the slope level window")),
    ("level_a_v", ":ALEVel", "number"),
    ("level_b_v", ":BLEVel", "number"),
)
_TRIGGER_PATTERN_ITEMS: tuple[tuple[str, str, object], ...] = (
    ("source", ":SOURce", "tsource"),
    ("level_v", ":LEVel", "number"),
    ("pattern", ":PATTern", ("pattern", "trigger_pattern_levels", "the per-channel pattern")),
)
_TRIGGER_DURATION_ITEMS: tuple[tuple[str, str, object], ...] = (
    ("source", ":SOURce", "tsource"),
    ("level_v", ":LEVel", "number"),
    ("pattern", ":TYPE", ("pattern", "trigger_duration_types", "the per-channel pattern")),
    ("when", ":WHEN", ("enum", "trigger_duration_when", "the duration condition")),
    ("upper_time_s", ":TUPPer", "number"),
    ("lower_time_s", ":TLOWer", "number"),
)
_TRIGGER_TIMEOUT_ITEMS: tuple[tuple[str, str, object], ...] = (
    ("source", ":SOURce", "tsource"),
    ("level_v", ":LEVel", "number"),
    ("slope", ":SLOPe", ("enum", "trigger_slopes", "the timeout edge")),
    ("time_s", ":TIME", "number"),
)
_TRIGGER_RUNT_ITEMS: tuple[tuple[str, str, object], ...] = (
    ("source", ":SOURce", "tsource"),
    ("polarity", ":POLarity", ("enum", "trigger_polarities", "the runt polarity")),
    ("when", ":WHEN", ("enum", "trigger_runt_when", "the runt width condition")),
    ("upper_width_s", ":WUPPer", "number"),
    ("lower_width_s", ":WLOWer", "number"),
    ("level_a_v", ":ALEVel", "number"),
    ("level_b_v", ":BLEVel", "number"),
)
#: **サブツリーは `:WINDows`(複数)。`:TRIGger:MODE` の値は `WINDow`(単数)**
_TRIGGER_WINDOW_ITEMS: tuple[tuple[str, str, object], ...] = (
    ("source", ":SOURce", "tsource"),
    ("slope", ":SLOPe", ("enum", "trigger_window_slopes", "the window edge")),
    ("position", ":POSition", ("enum", "trigger_window_positions", "the window trigger position")),
    ("time_s", ":TIME", "number"),
    ("level_a_v", ":ALEVel", "number"),
    ("level_b_v", ":BLEVel", "number"),
)
_TRIGGER_DELAY_ITEMS: tuple[tuple[str, str, object], ...] = (
    ("source_a", ":SA", "tsource"),
    ("slope_a", ":ASLop", ("enum", "trigger_edge_slopes", "the source A edge")),
    ("source_b", ":SB", "tsource"),
    ("slope_b", ":BSLop", ("enum", "trigger_edge_slopes", "the source B edge")),
    ("when", ":TYPE", ("enum", "trigger_delay_types", "the delay condition")),
    ("upper_time_s", ":TUPPer", "number"),
    ("lower_time_s", ":TLOWer", "number"),
    ("level_a_v", ":ALEVel", "number"),
    ("level_b_v", ":BLEVel", "number"),
)
#: **サブツリーは `:SHOLd`。`:TRIGger:MODE` の値は `SETup`**
_TRIGGER_SHOLD_ITEMS: tuple[tuple[str, str, object], ...] = (
    ("data_source", ":DSRC", "tsource"),
    ("clock_source", ":CSRC", "tsource"),
    ("slope", ":SLOPe", ("enum", "trigger_edge_slopes", "the clock edge")),
    ("pattern", ":PATTern", ("enum", "trigger_shold_patterns", "the data pattern")),
    ("when", ":TYPE", ("enum", "trigger_shold_types", "which time is checked")),
    ("setup_time_s", ":STIMe", "number"),
    ("hold_time_s", ":HTIMe", "number"),
    ("data_level_v", ":DLEVel", "number"),
    ("clock_level_v", ":CLEVel", "number"),
)
_TRIGGER_NEDGE_ITEMS: tuple[tuple[str, str, object], ...] = (
    ("source", ":SOURce", "tsource"),
    ("level_v", ":LEVel", "number"),
    ("slope", ":SLOPe", ("enum", "trigger_edge_slopes", "the counted edge")),
    ("idle_time_s", ":IDLE", "number"),
    ("edge_number", ":EDGE", ("int", 1, 65535)),
)
#: --- シリアルバストリガ(ガイド3.27.20-24)------------------------------
#: デコード側(`:BUS<n>`)とは**別サブシステム**。デコードは「読んで表にする」、
#: トリガは「条件に合った瞬間に取り込む」で、綴りも値域も別に確認している。
#:
#: **CURRbit / CODE(データのビット単位マスク)は意図的に未対応。**
#: `<CURRbit で桁を選ぶ → CODE でその桁の値を 0/1/任意 に設定>` という状態を
#: 持つ2コマンドの対で、decode の `bit_sources` と同じくリスト値のキーが要る。
#: `data` で「この値のとき」は表現できるため、必要になってから足す。
_TRIGGER_RS232_ITEMS: tuple[tuple[str, str, object], ...] = (
    ("source", ":SOURce", "tsource"),
    ("level_v", ":LEVel", "number"),
    ("polarity", ":POLarity", ("enum", "trigger_polarities", "the RS232 polarity")),
    ("when", ":WHEN", ("enum", "trigger_rs232_when", "the RS232 trigger condition")),
    ("baud_bps", ":BAUD", ("int", 1, 20_000_000)),
    ("user_baud_bps", ":BUSer", ("int", 1, 20_000_000)),
    ("data_bits", ":WIDTh", ("choice", (5, 6, 7, 8))),
    ("stop_bits", ":STOP", ("choice", (1, 1.5, 2))),
    ("parity", ":PARity", ("enum", "trigger_parities", "the RS232 parity")),
    ("data", ":DATA", ("int", 0, 255)),
)
_TRIGGER_IIC_ITEMS: tuple[tuple[str, str, object], ...] = (
    ("clock_source", ":SCL", "tsource"),
    ("clock_level_v", ":CLEVel", "number"),
    ("data_source", ":SDA", "tsource"),
    ("data_level_v", ":DLEVel", "number"),
    ("when", ":WHEN", ("enum", "trigger_iic_when", "the I2C trigger condition")),
    ("address_bits", ":AWIDth", ("choice", (7, 8, 10))),
    ("address", ":ADDRess", ("int", 0, 1023)),
    ("direction", ":DIRection", ("enum", "trigger_iic_directions", "the I2C direction")),
    ("data_bytes", ":DBYTes", ("int", 1, 5)),
    ("data", ":DATA", ("int", 0, 2**40 - 1)),
)
#: SPIは `CLK`/`SCL` と `MISO`/`SDA` が**同義の別名**(ガイドの説明文が同一)。
#: 片方だけ公開する — 両方出すと同じ設定に2つの入口ができて混乱する
_TRIGGER_SPI_ITEMS: tuple[tuple[str, str, object], ...] = (
    ("clock_source", ":CLK", "tsource"),
    ("clock_level_v", ":CLEVel", "number"),
    ("clock_slope", ":SLOPe", ("enum", "trigger_edge_slopes", "the SPI clock edge")),
    ("data_source", ":MISO", "tsource"),
    ("data_level_v", ":DLEVel", "number"),
    ("cs_source", ":CS", "tsource"),
    ("cs_level_v", ":SLEVel", "number"),
    ("cs_polarity", ":MODE", ("enum", "trigger_spi_cs_modes", "the chip select polarity")),
    ("when", ":WHEN", ("enum", "trigger_spi_when", "how a frame is delimited")),
    ("timeout_s", ":TIMeout", "number"),
    ("data_bits", ":WIDTh", ("int", 4, 32)),
    ("data", ":DATA", ("int", 0, 2**32 - 1)),
)
_TRIGGER_CAN_ITEMS: tuple[tuple[str, str, object], ...] = (
    ("source", ":SOURce", "tsource"),
    ("level_v", ":LEVel", "number"),
    ("baud_bps", ":BAUD", ("int", 10_000, 5_000_000)),
    ("signal_type", ":STYPe", ("enum", "trigger_can_signal_types", "the CAN signal type")),
    ("when", ":WHEN", ("enum", "trigger_can_when", "the CAN trigger condition")),
    ("sample_point_percent", ":SPOint", ("int", 10, 90)),
    ("extended_id", ":EXTended", "bool"),
    ("define", ":DEFine", ("enum", "trigger_can_defines", "what the data condition matches")),
    ("data_bytes", ":DWIDth", ("int", 1, 8)),
    ("data", ":DATA", ("int", 0, 2**64 - 1)),
)
_TRIGGER_LIN_ITEMS: tuple[tuple[str, str, object], ...] = (
    ("source", ":SOURce", "tsource"),
    ("level_v", ":LEVel", "number"),
    ("standard", ":STANdard", ("enum", "trigger_lin_standards", "the LIN standard")),
    ("baud_bps", ":BAUD", ("int", 1_000, 20_000_000)),
    ("sample_point_percent", ":SAMPlepoint", ("int", 10, 90)),
    ("when", ":WHEN", ("enum", "trigger_lin_when", "the LIN trigger condition")),
    ("error_type", ":ERRor", ("enum", "trigger_lin_errors", "which LIN error is caught")),
    ("frame_id", ":ID", ("int", 0, 63)),
    # **実機実測**: 書き込みは整数だが読み戻しは2進マスク文字列
    # (`'01100100'` / 未設定ビットは `X`)。ガイドは「整数を返す」と書いている
    # が実機は違う。他プロトコル(CAN等)の `:DATA?` は素の整数を返す
    ("data", ":DATA", "bitmask"),
)
#: 種別 → (SCPI接頭辞, 項目表)。**この表に無い種別は扱わない**(不在がゲート)
_TRIGGER_SUBTREES: dict[str, tuple[str, tuple[tuple[str, str, object], ...]]] = {
    "edge": (":TRIGger:EDGE", _TRIGGER_EDGE_ITEMS),
    "pulse": (":TRIGger:PULSe", _TRIGGER_PULSE_ITEMS),
    "slope": (":TRIGger:SLOPe", _TRIGGER_SLOPE_ITEMS),
    "pattern": (":TRIGger:PATTern", _TRIGGER_PATTERN_ITEMS),
    "duration": (":TRIGger:DURation", _TRIGGER_DURATION_ITEMS),
    "timeout": (":TRIGger:TIMeout", _TRIGGER_TIMEOUT_ITEMS),
    "runt": (":TRIGger:RUNT", _TRIGGER_RUNT_ITEMS),
    "window": (":TRIGger:WINDows", _TRIGGER_WINDOW_ITEMS),
    "delay": (":TRIGger:DELay", _TRIGGER_DELAY_ITEMS),
    "setup_hold": (":TRIGger:SHOLd", _TRIGGER_SHOLD_ITEMS),
    "nth_edge": (":TRIGger:NEDGe", _TRIGGER_NEDGE_ITEMS),
    "uart": (":TRIGger:RS232", _TRIGGER_RS232_ITEMS),
    "i2c": (":TRIGger:IIC", _TRIGGER_IIC_ITEMS),
    "spi": (":TRIGger:SPI", _TRIGGER_SPI_ITEMS),
    "can": (":TRIGger:CAN", _TRIGGER_CAN_ITEMS),
    "lin": (":TRIGger:LIN", _TRIGGER_LIN_ITEMS),
}
#: 種別に依らない共通設定(サブツリーを持たない)
_TRIGGER_COMMON_ITEMS: tuple[tuple[str, str, object], ...] = (
    ("sweep_mode", ":SWEep", "sweep"),
    ("holdoff_s", ":HOLDoff", "number"),
)
#: TriggerState の最上位へ昇格させる項目キー
_TRIGGER_TOP_LEVEL_KEYS = ("source", "level_v", "slope")

# :TRIGger:STATus? の生値がこれなら停止中(TD / WAIT / AUTO 等は動作中)
STOPPED_TRIGGER_STATUS = "STOP"
PREAMBLE_FIELDS = 10

# -- 信号発生(AFG、tools.md 7章 / MHO900プログラミングガイド 3.25)----------
#
# 送信順は固定(下記の並び順)。インピーダンスと周波数が振幅の、振幅がオフセットの
# 許容範囲を決めるため、範囲の広い側から順に送る。
#: 意味的キー → `:SOURce<n>` 相対のSCPIパス(この並びが送信順)
_AFG_ITEMS: tuple[tuple[str, str], ...] = (
    ("waveform", ":FUNCtion"),
    ("impedance", ":IMPedance"),
    ("frequency_hz", ":FREQuency"),
    ("amplitude_vpp", ":VOLTage:AMPLitude"),
    ("offset_v", ":VOLTage:OFFSet"),
    ("phase_deg", ":PHASe"),
    ("duty_percent", ":FUNCtion:SQUare:DUTY"),
    ("symmetry_percent", ":FUNCtion:RAMP:SYMMetry"),
)

#: 出力状態。読み書きの入口は `get_afg_config` / `set_afg_output` に限る
#: (`configure_afg` は設定項目のみを扱い、ここには触れない)
_AFG_OUTPUT_PATH = ":OUTPut:STATe"

#: 数値項目の値域 (下限, 上限, 下限を除外するか)。上限 None はモデルオプション・
#: 出力インピーダンス依存で宣言できないため機器に委ねる(範囲外は**エラーキューに
#: 何も積まずクランプされる**ので、requested / applied の突合で検出する。
#: docs/verification/mho98-afg.md 2章)。offset_v は範囲を持たない。
_AFG_RANGES: dict[str, tuple[float, float | None, bool]] = {
    "frequency_hz": (0.0, None, True),
    "amplitude_vpp": (0.0, None, True),
    "phase_deg": (0.0, 360.0, False),
    "duty_percent": (1.0, 99.0, False),
    "symmetry_percent": (0.0, 100.0, False),
    "am_depth_percent": (0.0, 120.0, False),
    "fm_deviation_hz": (0.0, None, True),
    "pm_deviation_deg": (0.0, 360.0, False),
}

# -- 信号発生: 変調(ガイド3.25.15-25)------------------------------------
#
# 変調タイプ(am/fm/pm)ごとの深さ/偏移キーとSCPIパス。frequency_hz / waveform は
# 「今回指定されたtype、無ければ現在のtype」の配下(:MOD:<TYPE>:INTernal:*)へ
# ルーティングするため、type自体はここに含めない(configure_afgのルーティング
# ロジックが type→SCPIトークンの変換を都度行う)。
_AFG_MOD_DEPTH_PATHS: dict[str, str] = {
    "am_depth_percent": ":MOD:AM:DEPTh",
    "fm_deviation_hz": ":MOD:FM:DEViation",
    "pm_deviation_deg": ":MOD:PM:DEViation",
}
#: 意味的type("am"/"fm"/"pm") → その深さ/偏移キー・SCPIパス
_AFG_MOD_DEPTH_BY_TYPE: dict[str, tuple[str, str]] = {
    "am": ("am_depth_percent", ":MOD:AM:DEPTh"),
    "fm": ("fm_deviation_hz", ":MOD:FM:DEViation"),
    "pm": ("pm_deviation_deg", ":MOD:PM:DEViation"),
}
#: configure_afgのmodulation引数で受理するキー(値域検証は_afg_number/専用ロジック)
_AFG_MOD_KEYS = frozenset(
    {
        "enabled",
        "type",
        "am_depth_percent",
        "fm_deviation_hz",
        "pm_deviation_deg",
        "frequency_hz",
        "waveform",
    }
)

#: ARBファイル選択(ガイド3.25.3)。機器内蔵ストレージのパスのみ(C:/ローカル、
#: D:/USB)。**このサーバーは機器内ファイルの作成・転送・削除を一切行わない**
#: (docs/Requirements.md 3.4)。既存ファイルのパス選択のみが対象。
_ARB_FILE_PREFIXES = ("C:/", "D:/")

# -- MATH演算(tools.md / MHO900プログラミングガイド 3.16章)-----------------
#
# 送信順は `configure_math` が「display ON → 下記4表(_MATH_ITEMS → _MATH_FFT_ITEMS →
# _MATH_FILTER_ITEMS → _MATH_VERTICAL_ITEMS)→ display OFF」の順に固定する。
# 種別: "number"(NR3) / "bool" / ("int", 下限, 上限) / ("enum", 方言キー, 説明) /
# "source"(SOURce1/2 のトークン) / "lsource"(LSOurce1/2 のトークン)
_MATH_ITEMS: tuple[tuple[str, str, object], ...] = (
    ("operator", ":OPERator", ("enum", "math_operators", "the math operator")),
    ("source1", ":SOURce1", "source"),
    ("source2", ":SOURce2", "source"),
    ("lsource1", ":LSOurce1", "lsource"),
    ("lsource2", ":LSOurce2", "lsource"),
)

#: 垂直方向(論理演算・FFTでは機器側が拒否する。結合検証はホストで行わない)
_MATH_VERTICAL_ITEMS: tuple[tuple[str, str, object], ...] = (
    ("scale", ":SCALe", "number"),
    ("offset_v", ":OFFSet", "number"),
    ("invert", ":INVert", "bool"),
)

#: FFTサブツリー(ガイド3.16.14-29)。HSCale / HCENter は意図的に非対応
#: (freq_start_hz / freq_end_hz で表現する)。average_count の範囲はガイド逐語。
#: search_num の範囲はガイド抽出がページ跨ぎで欠落しているため上限を置かない。
#: `source` はFFT演算の入力ch(`:SOURce1` ではなくこちらが使われる)。トークンの
#: 規則は `:SOURce1` と同じ("source" 種別)。
_MATH_FFT_ITEMS: tuple[tuple[str, str, object], ...] = (
    ("source", ":FFT:SOURce", "source"),
    ("window", ":FFT:WINDow", ("enum", "math_fft_windows", "the FFT window")),
    ("unit", ":FFT:UNIT", ("enum", "math_fft_units", "the FFT vertical unit")),
    ("mode", ":FFT:MODE", ("enum", "math_fft_modes", "the FFT operation mode")),
    ("average_count", ":FFT:AVCNt", ("int", 2, 1000)),
    ("scale", ":FFT:SCALe", "number"),
    ("offset", ":FFT:OFFSet", "number"),
    ("freq_start_hz", ":FFT:FREQuency:STARt", "number"),
    ("freq_end_hz", ":FFT:FREQuency:END", "number"),
    ("search_enabled", ":FFT:SEARch:ENABle", "bool"),
    ("search_num", ":FFT:SEARch:NUM", ("int", 1, None)),
    ("search_threshold", ":FFT:SEARch:THReshold", "number"),
    ("search_excursion", ":FFT:SEARch:EXCursion", "number"),
    (
        "search_order",
        ":FFT:SEARch:ORDer",
        ("enum", "math_fft_search_orders", "the FFT peak search order"),
    ),
)

#: デジタルフィルタ(ガイド3.16.31-33)
_MATH_FILTER_ITEMS: tuple[tuple[str, str, object], ...] = (
    ("type", ":FILTer:TYPE", ("enum", "math_filter_types", "the digital filter type")),
    ("w1_hz", ":FILTer:W1", "number"),
    ("w2_hz", ":FILTer:W2", "number"),
)

#: SCALe / OFFSet を持たない演算子(ガイド3.16.7/3.16.8)。読み取りの分岐に使う
_MATH_LOGIC_OPERATORS = frozenset({"and", "or", "xor", "not"})
#: FILTerサブツリーを持つ演算子(ガイド3.16.31)
_MATH_FILTER_OPERATORS = frozenset({"lowpass", "highpass", "bandpass", "bandstop"})

_MATH_FFT_PEAKS_PATH = ":FFT:SEARch:RES?"

# -- カーソル / カウンタ / 電圧計 / ヒストグラム(ガイド3.7・3.8・3.10・3.11)--
#
# 項目表の形式・種別の記法は `_MATH_ITEMS` と共通(この並びが送信順)。追加した
# 種別は "csource"(カーソルのソース: CH / MATH / NONE)と "achannel"
# (アナログchのみ)。デジタルchも取る `:COUNter:SOURce` は "lsource" と同値域。

#: MANual / TRACk 共通の位置(CAX/CBXは秒、CAY/CBYはV。ガイド3.8.4-3.8.6)
_CURSOR_POSITION_ITEMS: tuple[tuple[str, str, object], ...] = (
    ("ax", ":CAX", "number"),
    ("ay", ":CAY", "number"),
    ("bx", ":CBX", "number"),
    ("by", ":CBY", "number"),
)
_CURSOR_MANUAL_ITEMS: tuple[tuple[str, str, object], ...] = (
    ("type", ":TYPE", ("enum", "cursor_types", "the cursor type")),
    ("source", ":SOURce", "csource"),
) + _CURSOR_POSITION_ITEMS
_CURSOR_TRACK_ITEMS: tuple[tuple[str, str, object], ...] = (
    ("source1", ":SOURce1", "csource"),
    ("source2", ":SOURce2", "csource"),
) + _CURSOR_POSITION_ITEMS
#: モード → (SCPI接頭辞, 項目表)。**OFF / XY は位置サブツリーを持たない**
#: (`:CURSor:XY:*` はM2スコープ外)ため、この表に無いことがそのままゲート
_CURSOR_SUBTREES: dict[str, tuple[str, tuple[tuple[str, str, object], ...]]] = {
    "manual": (":CURSor:MANual", _CURSOR_MANUAL_ITEMS),
    "track": (":CURSor:TRACk", _CURSOR_TRACK_ITEMS),
}
#: 読み取り専用の測定値(ガイド3.8.7-3.8.8)。SI単位付きキー → SCPIパス
_CURSOR_READOUTS: tuple[tuple[str, str], ...] = (
    ("ax_s", ":AXValue"),
    ("ay_v", ":AYValue"),
    ("bx_s", ":BXValue"),
    ("by_v", ":BYValue"),
    ("xdelta_s", ":XDELta"),
    ("ydelta_v", ":YDELta"),
    ("ixdelta_hz", ":IXDelta"),
)

_COUNTER_ITEMS: tuple[tuple[str, str, object], ...] = (
    ("enabled", ":ENABle", "bool"),
    ("source", ":SOURce", "lsource"),
    ("mode", ":MODE", ("enum", "counter_modes", "the frequency counter mode")),
    ("digits", ":NDIGits", ("int", 3, 6)),
    ("totalize_enabled", ":TOTalize:ENABle", "bool"),
)
_DVM_ITEMS: tuple[tuple[str, str, object], ...] = (
    ("enabled", ":ENABle", "bool"),
    ("source", ":SOURce", "achannel"),
    ("mode", ":MODE", ("enum", "dvm_modes", "the digital voltmeter mode")),
)
#: 意味的な種別 → (capability, SCPI接頭辞, 項目表, 説明)。カウンタと電圧計は
#: 「有効化 + ソース + モード + 現在値1本」で同形のため1つのAPIで扱う
_METERS: dict[str, tuple[str, str, tuple[tuple[str, str, object], ...], str]] = {
    "counter": (
        "frequency_counter",
        ":COUNter",
        _COUNTER_ITEMS,
        "the frequency counter",
    ),
    "dvm": ("dvm", ":DVM", _DVM_ITEMS, "the digital voltmeter"),
}
#: 現在値(ガイド3.7.1 / 3.10.1)。`:VALue` というニモニックは存在しない
_METER_VALUE_PATH = ":CURRent?"

_HISTOGRAM_ITEMS: tuple[tuple[str, str, object], ...] = (
    ("enabled", ":ENABle", "bool"),
    ("type", ":TYPE", ("enum", "histogram_types", "the histogram type")),
    ("source", ":SOURce", "achannel"),
    ("height", ":HEIGht", ("int", 1, 4)),
    ("left_s", ":RANGe:LEFT", "number"),
    ("right_s", ":RANGe:RIGHt", "number"),
    ("bottom_v", ":RANGe:BOTTom", "number"),
    ("top_v", ":RANGe:TOP", "number"),
)
#: ガイド明記の大小制約(3.11.5-3.11.8 のRemarks)。**同一呼び出しで両端が
#: 指定されたときだけ**検証する(片側だけでは現在値との突合が要るため機器に委ねる)
_HISTOGRAM_RANGE_PAIRS: tuple[tuple[str, str], ...] = (
    ("left_s", "right_s"),
    ("bottom_v", "top_v"),
)
_HISTOGRAM_PREFIX = ":HISTogram"
_HISTOGRAM_RESULT_PATH = ":STATistics:RESult?"

# -- リファレンス波形(ガイド3.20章)---------------------------------------
#
# **他の全サブシステムと違い、枠番号はニモニックではなくコマンド引数**で渡す
# (`:REFerence:VSCale <ref>,<scale>` / 問い合わせは `:REFerence:VSCale? <ref>`)。
# 項目表の形式・種別の記法は `_MATH_ITEMS` と共通で、この並びが送信順。追加した
# 種別は "rsource"(CH / MATH / D0-D15。REF自身も NONE も取らない)と "label"
# (引用符無しで埋め込むASCII文字列)。
_REFERENCE_PREFIX = ":REFerence"
_REFERENCE_ITEMS: tuple[tuple[str, str, object], ...] = (
    ("source", ":SOURce", "rsource"),
    ("scale", ":VSCale", "number"),
    ("offset_v", ":VOFFset", "number"),
    ("color", ":COLor", ("enum", "reference_colors", "the reference waveform color")),
    ("label", ":LABel:CONTent", "label"),
)
#: **全枠共通**のスイッチ(ガイド3.20.6)。枠引数を取らないため項目表を分ける
_REFERENCE_GLOBAL_ITEMS: tuple[tuple[str, str, object], ...] = (
    ("label_display", ":LABel:ENABle", "bool"),
)
#: ラベルに許す文字。値は引用符無しでコマンドへ埋め込むため、SCPIインジェクション
#: 対策として**ホワイトリスト**で検証する(`;` はコマンドセパレータ、空白は引数の
#: 区切りで特に危険)。ガイドは「英数字と一部記号」とだけ書き、長さ上限は記載が
#: 無いため上限は置かない
_REFERENCE_LABEL_RE = re.compile(r"^[A-Za-z0-9_.+-]+$")

_CHANNEL_RE = re.compile(r"^(?:CH|CHAN|CHANNEL)?\s*([0-9]+)$", re.IGNORECASE)
_MATH_SOURCE_RE = re.compile(r"^MATH\s*([0-9]+)$", re.IGNORECASE)
_REF_SOURCE_RE = re.compile(r"^REF\s*([0-9]+)$", re.IGNORECASE)
_DIGITAL_SOURCE_RE = re.compile(r"^D\s*([0-9]+)$", re.IGNORECASE)
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


def math_source_number(value: object) -> int | None:
    """`MATH2` / `math2` → 2。MATHソースでなければ None。

    表記の判別だけを行う公開API(存在するMATHチャンネルかどうかの検証は
    `ScopeDriver._math_prefix` の責務)。
    """
    match = _MATH_SOURCE_RE.match(value.strip()) if isinstance(value, str) else None
    return int(match.group(1)) if match else None


def _pattern_token(value: object, key: str, to_token, width: int) -> str:
    """ch別パターンのリストを `H,L,X,X` の形へ。スカラは受け付けない。"""
    if isinstance(value, str) or not isinstance(value, (list, tuple)):
        raise _invalid(
            f"{key} must be a list of per-channel levels (one per analog channel)",
            {"key": key, "value": value},
        )
    if not value or len(value) > width:
        raise _invalid(
            f"{key} must have 1 to {width} entries (one per analog channel)",
            {"key": key, "value": list(value), "analog_channels": width},
        )
    return ",".join(to_token(item, key) for item in value)


def _bitmask_readback(text: str) -> object:
    """2進マスクの読み戻し。全て0/1なら整数へ、`X`(don't care)を含むならそのまま。"""
    token = text.strip()
    if token and all(c in "01" for c in token):
        return int(token, 2)
    return token


def _threshold_token(key: str, value: object) -> str:
    """しきい値の送信形。**整数値は小数点を落とす**(実機がそれを要求する)。"""
    if not isinstance(value, (int, float)) or isinstance(value, bool):
        raise _invalid(f"{key} is not a number: {value!r}", {"key": key, "value": value})
    number = float(value)
    if not math.isfinite(number):
        raise _invalid(f"{key} is not finite: {value!r}", {"key": key, "value": value})
    return str(int(number)) if number.is_integer() else repr(number)


def _choice_token(value: object, key: str, allowed: tuple) -> str:
    """飛び飛びの数値選択肢を送信形へ。整数は小数点を落とす(`1.0` → `1`)。"""
    if not isinstance(value, (int, float)) or isinstance(value, bool):
        raise _invalid(f"{key} is not a number: {value!r}", {"key": key, "value": value})
    if not any(abs(float(value) - float(option)) < 1e-12 for option in allowed):
        raise _invalid(
            f"{key} must be one of {list(allowed)}: {value!r}",
            {"key": key, "value": value, "allowed": list(allowed)},
        )
    number = float(value)
    return str(int(number)) if number.is_integer() else str(number)


def _invalid(message: str, detail: dict) -> ScopeError:
    return ScopeError(ErrorCode.INVALID_PARAMETER, message, detail)


def _unsupported(message: str, detail: dict) -> ScopeError:
    return ScopeError(ErrorCode.UNSUPPORTED_FEATURE, message, detail)


def _afg_number(key: str, value: object) -> str:
    """AFGの数値項目を送信トークンへ。値域は**送信前**にここで検証する。"""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise _invalid(f"{key} is not a number: {value!r}", {"key": key, "value": value})
    number = float(value)
    low, high, exclusive = _AFG_RANGES.get(key, (None, None, False))
    too_low = low is not None and (number <= low if exclusive else number < low)
    if too_low or (high is not None and number > high):
        allowed = f"greater than {low}" if high is None else f"between {low} and {high}"
        raise _invalid(
            f"{key} must be {allowed}: {value!r}",
            {"key": key, "value": value, "min": low, "max": high},
        )
    return format_number(number)


def _math_source_readback(text: str) -> str:
    """`CHAN2` → `CH2`。`REF3` / `MATH1` / `D0` は大文字化してそのまま。"""
    if not isinstance(text, str):
        raise ScopeError(
            ErrorCode.SCPI_ERROR, "source response is not a string", {"raw": text}
        )
    return normalize_channel(text.strip().upper())


def _math_bool(value: object, key: str) -> str:
    if not isinstance(value, bool):
        raise _invalid(f"{key} is not a boolean: {value!r}", {"key": key, "value": value})
    return "ON" if value else "OFF"


def _math_int(value: object, key: str, low: int, high: int | None) -> str:
    """整数項目を送信トークンへ。値域は**送信前**にここで検証する。"""
    if isinstance(value, bool) or not isinstance(value, int):
        raise _invalid(f"{key} is not an integer: {value!r}", {"key": key, "value": value})
    if value < low or (high is not None and value > high):
        allowed = f"{low} or greater" if high is None else f"between {low} and {high}"
        raise _invalid(
            f"{key} must be {allowed}: {value!r}",
            {"key": key, "value": value, "min": low, "max": high},
        )
    return str(value)


def _reference_label(value: object, key: str) -> str:
    """リファレンスのラベル文字列を**送信前**に検証する(ガイド3.20.5)。

    引用符無しでそのままコマンドへ埋め込むため、`_validate_afg_arb_file` と同じく
    ホワイトリストで受理する(`;` によるコマンド注入と、空白による引数の取り違えを
    送信前に潰す)。
    """
    if not isinstance(value, str) or not _REFERENCE_LABEL_RE.match(value):
        raise _invalid(
            f"{key} must be a non-empty string of letters, digits, '_', '.', "
            f"'+' or '-' (no spaces): {value!r}",
            {"key": key, "value": value},
        )
    return value


def _optional_number(text: str) -> float | None:
    """数値応答を返す。`AUTO` 等の非数値は「取得できない」として None。"""
    return parse_nr3(text) if _NUMBER_RE.match(text.strip()) else None


def _validate_afg_arb_file(value: object) -> str:
    """ARBファイルパスを**送信前**に検証する(ガイド3.25.3)。

    値は引用符無しでそのままコマンドへ埋め込むため、SCPIインジェクション対策
    としてプレフィクス(`C:/` / `D:/`)以降を**ホワイトリスト**
    (`[A-Za-z0-9._/-]`)で検証する(`;` はSCPIのコマンドセパレータであり
    特に危険 — Copilotレビュー指摘)。機器内蔵ストレージの既存ファイルを
    選択するだけで、ファイルの作成・転送・削除は一切行わない。
    """
    if not isinstance(value, str) or not value:
        raise _invalid(
            f"arb_file is not a non-empty string: {value!r}", {"arb_file": value}
        )
    if not value.startswith(_ARB_FILE_PREFIXES):
        raise _invalid(
            f"arb_file must start with 'C:/' (local) or 'D:/' (USB): {value!r}",
            {"arb_file": value},
        )
    if not re.fullmatch(r"[A-Za-z0-9._/-]+", value[3:]):
        raise _invalid(
            "arb_file may contain only letters, digits, '.', '_', '/' and '-' "
            f"after the drive prefix (';', whitespace etc. are rejected): {value!r}",
            {"arb_file": value},
        )
    if "." not in value.rsplit("/", 1)[-1]:
        raise _invalid(
            f"arb_file must end with a filename that has a suffix: {value!r}",
            {"arb_file": value},
        )
    return value


def _afg_arb_readback(text: str) -> str:
    return text.strip()


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
        self._afg_present: bool | None = None  # afg_presence_query の結果(接続中不変)

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
            # 呼び出し側の変更がキャッシュへ波及しないようコピーを返す
            return dict(self._options)

        query = self._required_dialect("option_query", "querying installed options")
        types = self.profile.dialect.get("option_types")
        if (
            not isinstance(types, dict)
            or not types
            or not all(isinstance(t, str) and t.strip() for t in types.values())
        ):
            # 不正なトークンを機器へ送らない(送信前検証)
            raise _unsupported(
                "this model's profile does not declare a value to use for "
                "querying installed options",
                {"dialect": "option_types", "profile": self.profile.name},
            )

        options: dict[str, bool | None] = {}
        for name, token in types.items():
            try:
                response = self.session.query(f"{query} {token}").strip()
            except ScopeError as exc:
                # 想定内の個別失敗(応答timeout)だけ None に降格する。切断等は
                # 全体の失敗なので伝播させる(全Noneをキャッシュして成功に
                # 見せない)
                if exc.code != ErrorCode.TIMEOUT:
                    raise
                response = ""
            options[name] = {"1": True, "0": False}.get(response)
        self._options = options
        return dict(options)

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

    def _declared_trigger_types(self) -> dict[str, str]:
        """このプロファイルが宣言する 種別名 → `:TRIGger:MODE` の値。

        宣言の不在がそのままゲート(オプション必須の種別は載せない)。
        項目表(`_TRIGGER_SUBTREES`)を持たない種別は宣言されていても扱わない。
        **未宣言なら空dictを返す** — 読み取り(`get_trigger`)は方言なしでも
        MVPから動いており、そこを壊さないため。書き込み側だけがゲートする。
        """
        declared = self.profile.dialect.get("trigger_types")
        if not isinstance(declared, dict):
            return {}
        return {n: v for n, v in declared.items() if n in _TRIGGER_SUBTREES}

    def _writable_trigger_types(self) -> dict[str, str]:
        """書き込みに使う種別表。未宣言なら1コマンドも送らずに失敗する。"""
        types = self._declared_trigger_types()
        if not types:
            raise _unsupported(
                "this model's profile does not declare any trigger type",
                {"dialect": "trigger_types", "profile": self.profile.name},
            )
        return types

    def _trigger_type(self) -> str:
        """機器の現在のトリガ種別(`:TRIGger:MODE?` を1本読む)。

        宣言に無いトークン(オプション種別など)は**そのまま返す**。呼び出し側は
        サブツリーを引けないので配下に触れない(get_decode_config と同じ fail-safe)。
        `edge` は rigol-generic が宣言しているので全機種で解決する。
        """
        raw = self.session.query(":TRIGger:MODE?").strip()
        types = self._declared_trigger_types()
        _, from_scpi = profile_enum(tuple(types.items()))
        try:
            return from_scpi(raw)
        except ScopeError:
            return raw.upper()

    def get_trigger(self) -> TriggerState:
        """現在のトリガ種別のサブツリーだけを読む。

        **他の種別のサブツリーは1本も問い合わせない**(M2のカーソルで確立した
        「今のモードのサブツリーだけを突く」原則)。
        """
        query = self.session.query
        trigger_type = self._trigger_type()
        settings: dict[str, object] = {}
        subtree = _TRIGGER_SUBTREES.get(trigger_type)
        if subtree is not None:
            prefix, items = subtree
            for key, path, kind in items:
                _, from_scpi = self._converter(kind, 0)
                settings[key] = from_scpi(query(f"{prefix}{path}?"))
        return TriggerState(
            type=trigger_type,
            source=settings.get("source"),
            level_v=settings.get("level_v"),
            slope=settings.get("slope"),
            sweep_mode=from_scpi_sweep(query(":TRIGger:SWEep?")),
            status=self.get_trigger_status(),
            settings=settings,
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

    def measure(
        self,
        channel: str,
        names: list[str],
        channel_b: str | None = None,
    ) -> list[MeasurementResult]:
        """指定の測定項目を読む。未確認ニモニックは実機へ送らない。

        `channel_b` は遅延・位相(`DUAL_SOURCE_MEASUREMENTS`)専用の第2ソース。
        単一ソース項目には付けない(機器が余分な引数を拒否しうるため)。
        """
        number = self._channel_number(channel)
        dual = [name for name in names if name in DUAL_SOURCE_MEASUREMENTS]
        if dual and channel_b is None:
            raise _invalid(
                "channel_b is required for delay/phase measurements "
                "(the device would otherwise reuse its last selected source)",
                {"measurements": dual},
            )
        if channel_b is not None and not dual:
            raise _invalid(
                "channel_b is only used by delay/phase measurements",
                {"measurements": list(names)},
            )
        number_b = self._channel_number(channel_b) if channel_b is not None else None

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
            source = f"CHANnel{number}"
            if name in DUAL_SOURCE_MEASUREMENTS:
                source = f"{source},CHANnel{number_b}"
            response = self.session.query(f":MEASure:ITEM? {mnemonic},{source}")
            value = parse_nr3(response)
            if abs(value) >= INVALID_MEASUREMENT:
                results.append(MeasurementResult(name, key, None, "unknown"))
            else:
                results.append(MeasurementResult(name, key, value, "valid"))
        return results

    def configure_measurement(
        self,
        *,
        source: str | None = None,
        threshold_source: str | None = None,
        threshold_type: str | None = None,
        threshold_max: float | None = None,
        threshold_mid: float | None = None,
        threshold_min: float | None = None,
        threshold_default: bool | None = None,
        area: str | None = None,
        region_ax_s: float | None = None,
        region_bx_s: float | None = None,
        region_linked: bool | None = None,
        amp_type: str | None = None,
        amp_top: str | None = None,
        amp_base: str | None = None,
        statistics_enabled: bool | None = None,
        statistics_count: int | None = None,
        statistics_reset: bool | None = None,
        statistics_items: list[str] | None = None,
    ) -> dict:
        """測定の前提設定(tools.md 14章)。None の項目は変更しない。

        検証は全て送信前に済ませる。1つでも不正なら1コマンドも送らない。
        """
        values = {
            "source": source,
            "threshold_source": threshold_source,
            "threshold_type": threshold_type,
            "threshold_max": threshold_max,
            "threshold_mid": threshold_mid,
            "threshold_min": threshold_min,
            "area": area,
            "region_ax_s": region_ax_s,
            "region_bx_s": region_bx_s,
            "region_linked": region_linked,
            "amp_type": amp_type,
            "amp_top": amp_top,
            "amp_base": amp_base,
            "statistics_enabled": statistics_enabled,
            "statistics_count": statistics_count,
        }
        # 統計の項目別有効化はソースを明示させる。省略すると機器は「最後に選んだ
        # ソース」を使い、呼び出し側から結果が予測できなくなる(ガイド3.17.8)
        if statistics_items and source is None:
            raise _invalid(
                "source is required when enabling statistics_items "
                "(the device would otherwise reuse its last selected source)",
                {"statistics_items": list(statistics_items)},
            )

        plan = self._command_plan(_MEASURE_PREFIX, 0, _MEASURE_SETUP_ITEMS, values)
        stat_commands = self._statistic_enable_commands(statistics_items, source)

        if not plan and not stat_commands and not threshold_default and not statistics_reset:
            return {}

        applied: dict[str, object] = {}
        if threshold_default:
            self.session.write_checked(_MEASURE_THRESHOLD_DEFAULT)
            applied["threshold_default"] = True
        for key, set_cmd, query, from_scpi in plan:
            applied[key] = from_scpi(self.session.set_and_verify(set_cmd, query))
        if stat_commands:
            for command in stat_commands:
                self.session.write_checked(command)
            applied["statistics_items"] = list(statistics_items or [])
        if statistics_reset:
            self.session.write_checked(_MEASURE_STATISTIC_RESET)
            applied["statistics_reset"] = True
        return applied

    def _statistic_enable_commands(
        self, items: list[str] | None, source: str | None
    ) -> list[str]:
        """`:MEASure:STATistic:ITEM <item>,<src>` の送信計画(検証もここで)。"""
        if not items:
            return []
        token = self._math_lsource(source, "source")
        commands = []
        for name in items:
            mnemonic = self._measurement_mnemonic(name)
            commands.append(f"{_MEASURE_STATISTIC_ITEM} {mnemonic},{token}")
        return commands

    def _measurement_mnemonic(self, name: str) -> str:
        """測定項目名 → SCPIニモニック。未宣言なら送信前に失敗する。"""
        mnemonic = self.profile.measurement_mnemonic(name)
        if mnemonic is None or name not in MEASUREMENT_KEYS:
            raise _unsupported(
                f"measurement item '{name}' is unverified in this model's profile",
                {"measurement": name, "profile": self.profile.name},
            )
        return mnemonic

    def get_measurement_config(self) -> dict:
        """測定の前提設定を読む(一発動作は状態を持たないので含めない)。"""
        self._afg_enum("measure_areas", "the measurement range")
        config: dict[str, object] = {}
        for key, path, kind in _MEASURE_SETUP_ITEMS:
            _, from_scpi = self._converter(kind, 0)
            config[key] = from_scpi(self.session.query(f"{_MEASURE_PREFIX}{path}?"))
        return config

    def get_measurement_statistics(
        self,
        channel: str,
        names: list[str],
        types: list[str] | None = None,
        channel_b: str | None = None,
    ) -> dict:
        """測定項目ごとの統計値を読む(ガイド3.17.8 のクエリ形)。

        `<type>` は1つずつ指定する形式なので、項目×種別の回数だけクエリする。
        応答は科学表記の単一値(ガイドの Return Format / Example)。
        """
        to_scpi, _ = self._afg_enum(
            "measure_statistic_types", "the measurement statistic type"
        )
        wanted = types if types is not None else list(
            self.profile.dialect["measure_statistic_types"]
        )
        number = self._channel_number(channel)
        dual = [name for name in names if name in DUAL_SOURCE_MEASUREMENTS]
        if dual and channel_b is None:
            raise _invalid(
                "channel_b is required for delay/phase statistics "
                "(the device would otherwise reuse its last selected source)",
                {"measurements": dual},
            )
        # `measure` と対称にする(2ソース項目が無いのに渡すのは取り違え)
        if channel_b is not None and not dual:
            raise _invalid(
                "channel_b is only used by delay/phase measurements",
                {"measurements": list(names)},
            )
        number_b = self._channel_number(channel_b) if channel_b is not None else None

        # 検証を全て済ませてから送る
        plan: list[tuple[str, str, str]] = []
        for name in names:
            mnemonic = self._measurement_mnemonic(name)
            source = f"CHANnel{number}"
            if name in DUAL_SOURCE_MEASUREMENTS:
                source = f"{source},CHANnel{number_b}"
            for kind in wanted:
                token = to_scpi(kind, "type")
                plan.append((name, kind, f"{_MEASURE_STATISTIC_ITEM}? {token},{mnemonic},{source}"))

        results: dict[str, dict[str, float | None]] = {}
        for name, kind, query in plan:
            value = parse_nr3(self.session.query(query))
            results.setdefault(name, {})[kind] = (
                None if abs(value) >= INVALID_MEASUREMENT else value
            )
        return results

    def clear_measurements(self) -> None:
        """Resultビューの全測定項目を消す(issue #16)。

        `:MEASure:ITEM?`(クエリ形)でも項目が有効化されて画面に蓄積するのが
        実機仕様のため、その掃除を担う。ニモニックはファミリで分岐する
        (MHO900: DELete / DHO800系: CLEar)ためdialect宣言必須 —
        未宣言プロファイルでは1コマンドも送らず UNSUPPORTED_FEATURE。
        write-onlyでreadbackは存在しない(run/stopと同じ扱い)。
        """
        command = self._required_dialect(
            "measurement_clear", "clearing the measurement items"
        )
        self.session.write_checked(command)

    # -- 画面・波形 -------------------------------------------------------

    def capture_screenshot_bytes(self) -> bytes:
        self._require("screenshot", "screen capture")
        command = self._dialect("screenshot_command", DEFAULT_SCREENSHOT_COMMAND)
        # 画像は約97KB。通常の問い合わせ用タイムアウトでは足りず接続破棄になる。
        timeout_s = self.profile.dialect.get("screenshot_timeout_s", DEFAULT_SCREENSHOT_TIMEOUT_S)
        return self.session.query_binary(command, timeout_s=float(timeout_s))

    def _waveform_source(self, channel: str) -> str:
        """波形ソースのSCPIトークン(ガイド3.28.1 `{CHANnel1-4|MATH1-4}`)。

        MATHはここでだけ受理する。トリガ・測定のソースはMATHを取らないため、
        共有の `_channel_number` / `normalize_channel` は広げない。
        """
        math = math_source_number(channel)
        if math is None:
            return f"CHANnel{self._channel_number(channel)}"
        number, _ = self._math_prefix(math)
        return f"MATH{number}"

    def read_waveform(self, channel: str, max_points: int | None = None) -> WaveformRaw:
        self._require("waveform_download", "waveform data download")
        source = self._waveform_source(channel)

        self.session.write_checked(f":WAVeform:SOURce {source}")
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

    def configure_trigger(
        self,
        type: str | None = None,
        source: str | None = None,
        level_v: float | None = None,
        slope: str | None = None,
        sweep_mode: str | None = None,
        holdoff_s: float | None = None,
        settings: dict | None = None,
    ) -> dict:
        """トリガを設定する。None の項目は変更しない。

        `type` を省略すると `:TRIGger:MODE?` を1本読み、**今の種別のサブツリー**
        へ書く(M2のカーソルで確立した原則)。サブツリー違いのキーは送信前に拒否
        する。`type` を指定した場合は `:TRIGger:MODE` を**先頭**に送ってから配下を
        書く(切り替えてから書かないと別種別の設定を触ることになる)。

        `source` / `level_v` / `slope` はサブツリーの項目キーと同名なので、
        `settings` に入れても同じ結果になる(既存の呼び出しとの互換のため残す)。
        """
        types = self._writable_trigger_types()
        to_scpi_type, _ = profile_enum(tuple(types.items()))

        merged: dict = dict(settings or {})
        for key, value in (("source", source), ("level_v", level_v), ("slope", slope)):
            if value is not None:
                merged[key] = value

        subtree_name = type if type is not None else self._trigger_type()
        # **ゲートは「プロファイルが宣言した種別」で行う。** モジュールの項目表
        # (_TRIGGER_SUBTREES)にあるだけでは足りない — 実機未検証の機種へ
        # `:TRIGger:MODE PULSe` を送ってしまう(AGENTS.mdルール2)
        subtree = _TRIGGER_SUBTREES.get(subtree_name) if subtree_name in types else None
        if subtree is None:
            raise _unsupported(
                f"trigger type '{subtree_name}' is unverified in this model's profile",
                {
                    "trigger_type": subtree_name,
                    "profile": self.profile.name,
                    "supported": sorted(types),
                },
            )
        prefix, items = subtree
        allowed = [key for key, _, _ in items]
        unknown = sorted(key for key in merged if key not in allowed)
        if unknown:
            raise _invalid(
                f"the {subtree_name} trigger does not have these settings: {unknown}",
                {"type": subtree_name, "unknown": unknown, "allowed": allowed},
            )

        # 検証を全て済ませてから送る(1つでも不正なら1コマンドも送らない)
        mode_command = (
            f":TRIGger:MODE {to_scpi_type(type, 'type')}" if type is not None else None
        )
        plan = self._command_plan(prefix, 0, items, merged)
        common = self._command_plan(
            ":TRIGger",
            0,
            _TRIGGER_COMMON_ITEMS,
            {"sweep_mode": sweep_mode, "holdoff_s": holdoff_s},
        )

        applied: dict[str, object] = {}
        if mode_command is not None:
            self.session.write_checked(mode_command)
            applied["type"] = type
        for key, set_cmd, query, from_scpi in plan + common:
            applied[key] = from_scpi(self.session.set_and_verify(set_cmd, query))
        return applied

    def get_trigger_position(self) -> float | None:
        """取得メモリ内のトリガ発生位置(ガイド3.27.7。読み取り専用)。"""
        return self._readout(":TRIGger:POSition?")

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
        ビット別ソース → `:DISPlay` → `:EVENt` に固定する(イベントテーブルの
        有効化にはバスの表示が先に必要、というガイドの制約に従う)。

        プロトコル別設定の順は**呼び出し側のキー順ではなく `DECODE_ITEMS` の
        並び**に固定する。パラレルの `:WIDTh` は `:BUS USER` が前提(ガイド
        3.4.10.4)なので、順序が呼び出し側の辞書順に左右されてはならない。
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
        bit_key = BIT_SOURCES.get(name)
        allowed = sorted(items if bit_key is None else [*items, bit_key])
        unknown = [key for key in settings if key not in allowed]
        if unknown:
            raise _invalid(
                f"unknown setting for protocol '{name}': {sorted(unknown)}",
                {"protocol": name, "unknown": sorted(unknown), "allowed": allowed},
            )
        self._reject_all_sources_off(name, items, settings)

        prefix = f":BUS{number}"
        # イベントテーブルはバス表示が先に有効であることが前提(ガイドの制約。
        # 表示OFFのまま :EVENt ON を送ると実機が沈黙し得るため、送信前に弾く)
        if event_table:
            if enabled is False:
                raise _invalid(
                    "event_table=true requires the decode bus display to be on; "
                    "call with enabled=true",
                    {"bus": number, "enabled": enabled},
                )
            if enabled is None and DISPLAY_ITEM.from_scpi(
                self.session.query(f"{prefix}{DISPLAY_ITEM.path}?")
            ) is not True:
                raise _invalid(
                    "event_table=true requires the decode bus display to be on, "
                    "but it is currently off; call with enabled=true",
                    {"bus": number},
                )
        mnemonic = protocols[name]
        mode_to, mode_from = profile_enum(tuple(protocols.items()))
        # ビット別ソースは対のコマンドなので、値の検証だけ先に済ませておく
        bit_tokens = (
            self._decode_bit_tokens(prefix, mnemonic, items, settings, bit_key)
            if bit_key is not None and bit_key in settings
            else []
        )

        def step(set_cmd: str, query: str, from_scpi: object) -> Callable[[], object]:
            return lambda: from_scpi(self.session.set_and_verify(set_cmd, query))

        # (返却キー, settings配下か, 実行して適用値を返す関数)
        plan: list[tuple[str, bool, Callable[[], object]]] = [
            (
                "protocol",
                False,
                step(
                    f"{prefix}:MODE {mode_to(name, 'protocol')}",
                    f"{prefix}:MODE?",
                    mode_from,
                ),
            )
        ]
        if data_format is not None:
            format_to, format_from = self._decode_format()
            plan.append(
                (
                    "data_format",
                    False,
                    step(
                        f"{prefix}:FORMat {format_to(data_format, 'data_format')}",
                        f"{prefix}:FORMat?",
                        format_from,
                    ),
                )
            )
        for key, item in items.items():  # 表の並び = 送信順
            if key not in settings:
                continue
            set_cmd, query = self._decode_command(
                prefix, mnemonic, key, item, settings[key]
            )
            plan.append((key, True, step(set_cmd, query, item.from_scpi)))
        if bit_tokens and bit_key is not None:
            plan.append(
                (
                    bit_key,
                    True,
                    lambda: self._write_bit_sources(prefix, mnemonic, bit_tokens),
                )
            )
        for key, value, item in (
            ("enabled", enabled, DISPLAY_ITEM),
            ("event_table", event_table, EVENT_ITEM),
        ):
            if value is not None:
                plan.append(
                    (
                        key,
                        False,
                        step(
                            f"{prefix}{item.path} {item.to_scpi(value, key)}",
                            f"{prefix}{item.path}?",
                            item.from_scpi,
                        ),
                    )
                )

        applied: dict[str, object] = {"bus": number}
        applied_settings: dict[str, object] = {}
        for key, is_setting, run in plan:
            value = run()
            if is_setting:
                applied_settings[key] = value
            else:
                applied[key] = value
        applied["settings"] = applied_settings
        return applied

    def _decode_bit_tokens(
        self,
        prefix: str,
        mnemonic: str,
        items: dict[str, DecodeItem],
        settings: dict,
        key: str,
    ) -> list[str]:
        """`bit_sources`(添字=ビット番号)を**送信前**に検証し、送信形へ変換する。

        ソースの値域は `:MATH:LSOurce` と同じ(`CH1`-`CH<アナログch数>` /
        `D0`-`D<デジタルch数-1>`。`off` は値域に無い — ガイド3.4.10.6)。

        バス幅との整合だけはここで見る(同時指定なら要求値、無ければ現在値)。
        **`bus` が `user` であることは検証しない**: 機器自身が `-200` で拒否して
        自己申告する経路(M1/M2/M3と同じ方針)に委ねる。
        """
        value = settings[key]
        if not isinstance(value, list) or not value:
            raise _invalid(
                f"{key} must be a non-empty list of sources, "
                "one per data bit starting at bit 0 (e.g. ['CH1', 'D3'])",
                {"key": key, "value": value},
            )
        tokens = [
            self._math_lsource(source, f"{key}[{bit}]")
            for bit, source in enumerate(value)
        ]

        width_item = items["bus_width"]
        if "bus_width" in settings:
            width = int(width_item.to_scpi(settings["bus_width"], "bus_width"))
        else:
            width = int(
                width_item.from_scpi(
                    self.session.query(f"{prefix}:{mnemonic}{width_item.path}?")
                )
            )
        if len(tokens) > width:
            raise _invalid(
                f"{key} has {len(tokens)} entries but the bus width is {width} "
                "(pass bus_width as well to widen the bus)",
                {"key": key, "entries": len(tokens), "bus_width": width},
            )
        return tokens

    def _write_bit_sources(
        self, prefix: str, mnemonic: str, tokens: list[str]
    ) -> list[str]:
        """ビット0から順に `:BITX <i>` → `:SOURce <src>` を送り、read-back値を返す。

        `:SOURce` は**選択中のビット**にだけ効く(ガイド3.4.10.6)ので、1ビット
        ごとに選び直す以外の手段が無い。
        """
        head = f"{prefix}:{mnemonic}"
        applied: list[str] = []
        for bit, token in enumerate(tokens):
            self.session.write_checked(f"{head}{BIT_SELECT_PATH} {bit}")
            applied.append(
                _math_source_readback(
                    self.session.set_and_verify(
                        f"{head}{BIT_SOURCE_PATH} {token}",
                        f"{head}{BIT_SOURCE_PATH}?",
                    )
                )
            )
        return applied

    def _read_bit_sources(self, prefix: str, mnemonic: str, width: int) -> list[str]:
        """ビット別ソースを読む(`:BITX` を1本ずつ選び直す)。

        機器に一括の問い合わせが無く、`:SOURce?` は選択中ビットのぶんしか返さない
        ため、**読み取りにも `:BITX` の書き込みが要る**。選択中ビットは編集対象を
        指すだけのスクラッチ状態だが、読み終わりに元の値へ必ず戻す。
        """
        head = f"{prefix}:{mnemonic}"
        selected = int(parse_nr3(self.session.query(f"{head}{BIT_SELECT_PATH}?")))
        sources: list[str] = []
        for bit in range(width):
            self.session.write_checked(f"{head}{BIT_SELECT_PATH} {bit}")
            sources.append(
                _math_source_readback(self.session.query(f"{head}{BIT_SOURCE_PATH}?"))
            )
        self.session.write_checked(f"{head}{BIT_SELECT_PATH} {selected}")
        return sources

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

    def get_decode_config(self, bus: int, *, include_bit_sources: bool = False) -> dict:
        """デコードバスの現在設定を意味的なキーで返す。**既定では書き込みを行わない**。

        `include_bit_sources=True` のときだけ、パラレルのビット別ソースを
        `:BITX` を書きながら走査する(`_read_bit_sources` 参照)。読み取りが
        書き込みを伴う唯一の経路なので、呼び出し側の明示を要求する。
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
        bit_key = BIT_SOURCES.get(protocol)
        # ビット別ソースはデータソースが `user` のバスにしか無い(それ以外では
        # `:BITX` の書き込み自体を機器が `-200` で拒否する)
        if include_bit_sources and bit_key and settings.get("bus") == "user":
            settings[bit_key] = self._read_bit_sources(
                prefix, mnemonic, int(settings["bus_width"])
            )
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

    # -- 信号発生(tools.md 7章)-------------------------------------------

    @property
    def afg_channels(self) -> int:
        """信号発生チャンネル数(プロファイル未宣言なら 0 = 非対応)。"""
        count = self.profile.capabilities.get("afg_channels")
        return count if isinstance(count, int) and count > 0 else 0

    def _afg_prefix(self, channel: int) -> tuple[int, str]:
        """`(番号, ":SOURce<n>")`。**範囲外の番号は絶対に送らない**。

        実機MHO98は `:SOURce3` の1発でSCPIサーバー全体が沈黙する
        (docs/verification/mho98-afg.md 1章)。宣言の不在(`afg_prefix` 未宣言)は
        そのまま非対応のゲート。

        DHO900系は番号なし `:SOURce`(`{n}` を含まないテンプレート)で表現し、
        ジェネレータ搭載がS型のみのため dialect `afg_presence_query`
        (`:SYSTem:DGSTatus?`)が宣言されていれば**最初のAFG操作の前に1回だけ**
        照会する(0なら送信ゼロで UNSUPPORTED_FEATURE。結果は接続中キャッシュ)。
        """
        template = self._required_dialect("afg_prefix", "the function generator")
        count = self.afg_channels
        if count < 1:
            raise _unsupported(
                "this model's profile does not declare how many function generator "
                "channels exist",
                {"capability": "afg_channels", "profile": self.profile.name},
            )
        if (
            isinstance(channel, bool)
            or not isinstance(channel, int)
            or not 1 <= channel <= count
        ):
            raise _invalid(
                f"function generator channel {channel!r} does not exist "
                f"(this model has channels 1-{count})",
                {"channel": channel, "afg_channels": count},
            )
        presence = self.profile.dialect.get("afg_presence_query")
        if presence is not None:
            # プロファイル誤設定(空文字・非文字列)をそのまま送ると未定義ヘッダに
            # なり得るため、送信前に検証してフェイルクローズ(ルール2)
            if not isinstance(presence, str) or not presence.strip():
                raise _unsupported(
                    "this model's profile declares an invalid value for "
                    "querying the generator presence",
                    {"dialect": "afg_presence_query", "profile": self.profile.name},
                )
            if self._afg_present is None:
                self._afg_present = parse_bool(self.session.query(presence))
            if not self._afg_present:
                raise _unsupported(
                    "this model has no generator module installed "
                    "(only the S variants ship one)",
                    {"dialect": "afg_presence_query", "profile": self.profile.name},
                )
        return channel, template.replace("{n}", str(channel))

    def _afg_enum(self, dialect_key: str, what: str) -> tuple[object, object]:
        """プロファイルの対応表から (値→トークン, 応答→値) を組み立てる。"""
        mapping = self.profile.dialect.get(dialect_key)
        if not isinstance(mapping, dict) or not mapping:
            raise _unsupported(
                f"this model's profile does not declare a value to use for {what}",
                {"dialect": dialect_key, "profile": self.profile.name},
            )
        return profile_enum(tuple(mapping.items()))

    def _afg_item(self, key: str) -> tuple[object, object]:
        """項目1件の (値→トークン, 応答→値) 変換器。"""
        if key == "waveform":
            return self._afg_enum("afg_waveforms", "the function generator waveform")
        if key == "impedance":
            return self._afg_enum(
                "afg_impedances", "the function generator output impedance"
            )
        return (lambda value, k: _afg_number(k, value)), parse_nr3

    def configure_afg(
        self,
        channel: int,
        *,
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
        """信号発生器を設定し、read-backした適用値を返す。

        **出力状態(`:OUTPut:STATe`)には一切触れない。** 出力のON/OFFは別Tool
        (承認フロー付き)の責務で、本メソッドの実行で信号が外へ出ることはない。

        全ての検証を送信前に済ませる(不正なチャンネル番号1発で実機のSCPIサーバーが
        沈黙するため)。送信順は `_AFG_ITEMS` の並び順に固定し、`arb_file` は
        `waveform`(`:FUNCtion`)の直後・周波数/振幅より前に送る(ガイド3.25.3)。
        `modulation` はそれらを送った後、`_afg_modulation_plan` の順序で送る。
        """
        number, prefix = self._afg_prefix(channel)
        values = {
            "waveform": waveform,
            "impedance": impedance,
            "frequency_hz": frequency_hz,
            "amplitude_vpp": amplitude_vpp,
            "offset_v": offset_v,
            "phase_deg": phase_deg,
            "duty_percent": duty_percent,
            "symmetry_percent": symmetry_percent,
        }
        if (
            all(value is None for value in values.values())
            and arb_file is None
            and modulation is None
        ):
            raise _invalid(
                "No item to change was specified "
                f"(specify at least one of {' / '.join(values)} / arb_file / "
                "modulation)",
                {"channel": number},
            )

        # 先に全項目を検証してから送る(1項目でも不正なら1コマンドも送らない)
        plan: list[tuple[str, str, str, object]] = []
        for key, path in _AFG_ITEMS:
            value = values[key]
            if value is not None:
                to_scpi, from_scpi = self._afg_item(key)
                plan.append(
                    (
                        key,
                        f"{prefix}{path} {to_scpi(value, key)}",
                        f"{prefix}{path}?",
                        from_scpi,
                    )
                )
            if key == "waveform" and arb_file is not None:
                token = _validate_afg_arb_file(arb_file)
                plan.append(
                    (
                        "arb_file",
                        f"{prefix}:LOAD:ARBitrary {token}",
                        f"{prefix}:LOAD:ARBitrary?",
                        _afg_arb_readback,
                    )
                )

        # modulationの検証もここで完結させる(ルーティング先が今回未指定のtypeに
        # 依存する場合のみ、ここで初めて :MOD:TYPe? を1回問い合わせる)。
        mod_plan = (
            self._afg_modulation_plan(prefix, modulation)
            if modulation is not None
            else []
        )

        applied: dict[str, object] = {"channel": number}
        for key, set_cmd, query, from_scpi in plan:
            applied[key] = from_scpi(self.session.set_and_verify(set_cmd, query))
        if mod_plan:
            applied_modulation: dict[str, object] = {}
            for key, set_cmd, query, from_scpi in mod_plan:
                applied_modulation[key] = from_scpi(
                    self.session.set_and_verify(set_cmd, query)
                )
            applied["modulation"] = applied_modulation
        return applied

    def _afg_modulation_plan(
        self, prefix: str, modulation: object
    ) -> list[tuple[str, str, str, object]]:
        """変調設定の送信計画を組み立てる(ガイド3.25.15-25)。

        Python側の検証(キー・値域・トークン)を全て終えてから、必要な場合に限り
        現在の変調タイプを1回だけ問い合わせる(`frequency_hz` / `waveform` の
        ルーティング先が今回の呼び出しで `type` を指定していない場合のみ必要)。
        それ以外は機器へ一切触れない。

        **実機quirk(2026-08-27実測)**: `MOD:STATe` がOFFの間、変調パラメータの
        書き込みは**エラーなしで無視される**(表示OFFチャンネルへの書き込み無視と
        同族)。そのため送信順は状態依存とする:
        - 有効化(enabled=True)を伴う場合: `TYPe` → `STATe ON` → パラメータ
        - 無効化(enabled=False)を伴う場合: パラメータ → `STATe OFF`(最後)
        - enabled省略でパラメータを送る場合: 現在のSTATeを読み、OFFなら送信前に
          `INVALID_PARAMETER` で拒否(enabled=true の併用を促す)
        なお `MOD:STATe ON` にしても出力自体(`OUTPut:STATe`)はONにならない
        (実測確認済み)ため、この順序変更で信号が外に出ることはない。
        """
        if not isinstance(modulation, dict):
            raise _invalid(
                f"modulation is not an object: {modulation!r}",
                {"modulation": modulation},
            )
        unknown = [key for key in modulation if key not in _AFG_MOD_KEYS]
        if unknown:
            raise _invalid(
                f"unknown modulation setting: {sorted(unknown)}",
                {"unknown": sorted(unknown), "allowed": sorted(_AFG_MOD_KEYS)},
            )

        type_to, type_from = self._afg_enum(
            "afg_mod_types", "the function generator modulation type"
        )
        wave_to, wave_from = self._afg_enum(
            "afg_mod_waveforms", "the function generator modulation waveform"
        )

        mod_type = modulation.get("type")
        if mod_type is not None:
            type_to(mod_type, "type")  # 検証のみ(トークンは送信直前に再取得)
        for key in ("am_depth_percent", "fm_deviation_hz", "pm_deviation_deg"):
            if key in modulation:
                _afg_number(key, modulation[key])
        if "frequency_hz" in modulation:
            _afg_number("frequency_hz", modulation["frequency_hz"])
        if "waveform" in modulation:
            wave_to(modulation["waveform"], "waveform")
        if "enabled" in modulation and not isinstance(modulation["enabled"], bool):
            raise _invalid(
                f"enabled is not a boolean: {modulation['enabled']!r}",
                {"key": "enabled", "value": modulation["enabled"]},
            )

        # ここまでは全てPython側検証のみ(機器へは1バイトも送っていない)。
        needs_routing = "frequency_hz" in modulation or "waveform" in modulation
        effective_type = mod_type
        if effective_type is None and needs_routing:
            effective_type = type_from(self.session.query(f"{prefix}:MOD:TYPe?"))

        # 実機quirk対策: パラメータはSTATe ONの間しか適用されない
        param_keys = (
            "am_depth_percent",
            "fm_deviation_hz",
            "pm_deviation_deg",
            "frequency_hz",
            "waveform",
        )
        has_params = any(key in modulation for key in param_keys)
        enabled = modulation.get("enabled")
        if has_params and enabled is None:
            if not parse_bool(self.session.query(f"{prefix}:MOD:STATe?")):
                raise _invalid(
                    "modulation parameters are silently ignored while modulation "
                    "is off; pass modulation={'enabled': true, ...} in the same "
                    "call (enabling modulation does not turn the output on)",
                    {"keys": [k for k in param_keys if k in modulation]},
                )
        if has_params and enabled is False:
            # 無効化と同時のパラメータ設定は「パラメータ→OFF」の順で成立する
            pass

        plan: list[tuple[str, str, str, object]] = []
        if mod_type is not None:
            plan.append(
                (
                    "type",
                    f"{prefix}:MOD:TYPe {type_to(mod_type, 'type')}",
                    f"{prefix}:MOD:TYPe?",
                    type_from,
                )
            )
        for key in ("am_depth_percent", "fm_deviation_hz", "pm_deviation_deg"):
            if key in modulation:
                path = _AFG_MOD_DEPTH_PATHS[key]
                token = _afg_number(key, modulation[key])
                plan.append(
                    (key, f"{prefix}{path} {token}", f"{prefix}{path}?", parse_nr3)
                )
        if "frequency_hz" in modulation:
            sub = type_to(effective_type, "type")
            token = _afg_number("frequency_hz", modulation["frequency_hz"])
            plan.append(
                (
                    "frequency_hz",
                    f"{prefix}:MOD:{sub}:INTernal:FREQuency {token}",
                    f"{prefix}:MOD:{sub}:INTernal:FREQuency?",
                    parse_nr3,
                )
            )
        if "waveform" in modulation:
            sub = type_to(effective_type, "type")
            token = wave_to(modulation["waveform"], "waveform")
            plan.append(
                (
                    "waveform",
                    f"{prefix}:MOD:{sub}:INTernal:FUNCtion {token}",
                    f"{prefix}:MOD:{sub}:INTernal:FUNCtion?",
                    wave_from,
                )
            )
        state_entry = None
        if "enabled" in modulation:
            token = "ON" if modulation["enabled"] else "OFF"
            state_entry = (
                "enabled",
                f"{prefix}:MOD:STATe {token}",
                f"{prefix}:MOD:STATe?",
                parse_bool,
            )
        if state_entry is not None:
            if modulation["enabled"]:
                # ON はパラメータより前(TYPe直後)に置く(quirk対策)
                insert_at = 1 if mod_type is not None else 0
                plan.insert(insert_at, state_entry)
            else:
                # OFF は最後(パラメータをON中に書いてから無効化する)
                plan.append(state_entry)
        return plan

    def get_afg_config(self, channel: int) -> dict:
        """信号発生チャンネルの現在設定を意味的なキーで返す。

        出力状態は**読むだけ**(書き込みは行わない)。変調は現在有効なtype配下の
        深さ/偏移・変調周波数・変調波形のみを読む(約6問い合わせ追加)。
        """
        number, prefix = self._afg_prefix(channel)
        query = self.session.query

        config: dict[str, object] = {
            "channel": number,
            "output": parse_bool(query(f"{prefix}{_AFG_OUTPUT_PATH}?")),
        }
        for key, path in _AFG_ITEMS:
            _, from_scpi = self._afg_item(key)
            config[key] = from_scpi(query(f"{prefix}{path}?"))
        config["modulation"] = self._get_afg_modulation(prefix)
        return config

    def _get_afg_modulation(self, prefix: str) -> dict:
        """変調設定の現在値(ガイド3.25.15-25)。現在有効なtype配下のみを読む。"""
        type_to, type_from = self._afg_enum(
            "afg_mod_types", "the function generator modulation type"
        )
        _, wave_from = self._afg_enum(
            "afg_mod_waveforms", "the function generator modulation waveform"
        )
        query = self.session.query

        enabled = parse_bool(query(f"{prefix}:MOD:STATe?"))
        mod_type = type_from(query(f"{prefix}:MOD:TYPe?"))
        sub = type_to(mod_type, "type")
        depth_key, depth_path = _AFG_MOD_DEPTH_BY_TYPE[mod_type]

        return {
            "enabled": enabled,
            "type": mod_type,
            depth_key: parse_nr3(query(f"{prefix}{depth_path}?")),
            "frequency_hz": parse_nr3(
                query(f"{prefix}:MOD:{sub}:INTernal:FREQuency?")
            ),
            "waveform": wave_from(query(f"{prefix}:MOD:{sub}:INTernal:FUNCtion?")),
        }

    def set_afg_output(self, channel: int, enabled: bool) -> bool:
        """信号発生の出力をON/OFFし、read-backした状態を返す。

        **この1コマンドで実際に信号が外へ出る。** 承認(confirmトークン)と監査は
        上位(service/control.py)の責務で、本層は設定項目に一切触れない。
        """
        _, prefix = self._afg_prefix(channel)
        return parse_bool(
            self.session.set_and_verify(
                f"{prefix}{_AFG_OUTPUT_PATH} {'ON' if enabled else 'OFF'}",
                f"{prefix}{_AFG_OUTPUT_PATH}?",
            )
        )

    def sync_afg_phase(self, channel: int = 1) -> None:
        """両AFGチャンネルの位相を同期する(ガイド3.25.7)。

        引数無し・応答無しのwrite-onlyコマンド(run/stopと同型)。プリセットの
        周波数・位相へ両チャンネルを再設定し直すことで位相を揃える動作のため、
        周波数が等しいか整数比のときのみ意味を持つ(振幅・出力状態には触れない)。
        `channel` はコマンドの送信先(`:SOURce<n>`)を選ぶだけで、範囲検証は
        `_afg_prefix` に委ねる(両チャンネルとも影響を受ける)。
        """
        _, prefix = self._afg_prefix(channel)
        self.session.write_checked(f"{prefix}:PHASe:SYNChronize")

    # -- MATH演算(ガイド3.16章)-------------------------------------------

    @property
    def math_channels(self) -> int:
        """MATH演算チャンネル数(プロファイル未宣言なら 0 = 非対応)。"""
        count = self.profile.capabilities.get("math_channels")
        return count if isinstance(count, int) and count > 0 else 0

    def _math_prefix(self, channel: int) -> tuple[int, str]:
        """`(番号, ":MATH<n>")`。**範囲外の番号は絶対に送らない**。

        `:MATH<n>` にファミリ分岐の実例が無いため接頭辞はここでハードコードし、
        宣言の不在(`math_channels` 未宣言)をそのまま非対応のゲートにする
        (AFGの `_afg_prefix` と違い、方言テンプレートも実在照会も持たない)。
        """
        count = self.math_channels
        if count < 1:
            raise _unsupported(
                "this model's profile does not declare how many math channels exist",
                {"capability": "math_channels", "profile": self.profile.name},
            )
        if (
            isinstance(channel, bool)
            or not isinstance(channel, int)
            or not 1 <= channel <= count
        ):
            raise _invalid(
                f"math channel {channel!r} does not exist "
                f"(this model has channels 1-{count})",
                {"channel": channel, "math_channels": count},
            )
        return channel, f":MATH{channel}"

    @property
    def ref_channels(self) -> int:
        """リファレンス波形の本数(プロファイル未宣言なら 0 = 非対応)。"""
        count = self.profile.capabilities.get("ref_channels")
        return count if isinstance(count, int) and count > 0 else 0

    @property
    def digital_channels(self) -> int:
        """ロジックチャンネル数(プロファイル未宣言なら 0 = 非対応)。"""
        count = self.profile.capabilities.get("digital_channels")
        return count if isinstance(count, int) and count > 0 else 0

    def _math_source(self, value: object, key: str, channel: int) -> str:
        """`SOURce1/2` のトークンを検証して送信形へ(ガイド3.16.3/3.16.4)。

        受理するのは `CH1`-`CH<analog_channels>` / `REF1`-`REF<ref_channels>` /
        `MATH<m>`。**カスケードは m < n のみ**(ガイド逐語のRemarks)で、
        `:MATH1:SOURce1 MATH1` のような自己参照は送信前に拒否する。
        """
        if not isinstance(value, str):
            raise _invalid(f"{key} is not a string: {value!r}", {"key": key, "value": value})
        token = value.strip()
        match = _MATH_SOURCE_RE.match(token)
        if match is not None:
            number = int(match.group(1))
            if not 1 <= number < channel:
                raise _invalid(
                    f"{key} may only use a lower-numbered math channel "
                    f"(MATH1-MATH{channel - 1} for MATH{channel}): {value!r}",
                    {"key": key, "value": value, "channel": channel},
                )
            return f"MATH{number}"
        match = _REF_SOURCE_RE.match(token)
        if match is not None:
            number = int(match.group(1))
            available = self.ref_channels
            if available < 1:
                raise _invalid(
                    f"{key} cannot be {value!r}: this model does not support "
                    "reference waveforms",
                    {"key": key, "value": value, "ref_channels": available},
                )
            if not 1 <= number <= available:
                raise _invalid(
                    f"{key} reference waveform {value!r} does not exist "
                    f"(this model has REF1-REF{available})",
                    {"key": key, "value": value, "ref_channels": available},
                )
            return f"REF{number}"
        if _CHANNEL_RE.match(token) is None:
            raise _invalid(
                f"cannot interpret {key}: {value!r} "
                "(e.g. 'CH1', 'REF1' or a lower-numbered 'MATH1')",
                {"key": key, "value": value},
            )
        return f"CHANnel{self._channel_number(token)}"

    def _math_lsource(self, value: object, key: str) -> str:
        """`LSOurce1/2` のトークンを検証して送信形へ(ガイド3.16.5/3.16.6)。

        受理するのは `D0`-`D<digital_channels-1>` と `CH1`-`CH<analog_channels>`。
        """
        if not isinstance(value, str):
            raise _invalid(f"{key} is not a string: {value!r}", {"key": key, "value": value})
        token = value.strip()
        match = _DIGITAL_SOURCE_RE.match(token)
        if match is not None:
            number = int(match.group(1))
            available = self.digital_channels
            if available < 1:
                raise _invalid(
                    f"{key} cannot be {value!r}: this model does not support "
                    "digital channels",
                    {"key": key, "value": value, "digital_channels": available},
                )
            if not 0 <= number < available:
                raise _invalid(
                    f"{key} digital channel {value!r} does not exist "
                    f"(this model has D0-D{available - 1})",
                    {"key": key, "value": value, "digital_channels": available},
                )
            return f"D{number}"
        if _CHANNEL_RE.match(token) is None:
            raise _invalid(
                f"cannot interpret {key}: {value!r} (e.g. 'D0' or 'CH1')",
                {"key": key, "value": value},
            )
        return f"CHANnel{self._channel_number(token)}"

    def _converter(self, kind: object, channel: int) -> tuple[object, object]:
        """項目1件の (値→トークン, 応答→値) 変換器(種別は `_MATH_ITEMS` 参照)。

        `channel` はMATHのカスケード則("source" 種別)の検証にだけ使う。
        番号を持たないサブシステム(カーソル等)からは 0 を渡す。
        """
        if kind == "number":
            return (lambda value, key: _afg_number(key, value)), parse_nr3
        if kind == "bool":
            return _math_bool, parse_bool
        if kind == "tsource":
            # トリガのソースは CH / EXT / EXT5 / ACLINE / D0-D15(ガイド3.27.8.1)
            return (
                (lambda value, key: self._trigger_source(value)),
                (lambda text: self._normalize_source(text)),
            )
        if kind == "sweep":
            return (
                (lambda value, key: to_scpi_sweep(value)),
                from_scpi_sweep,
            )
        if kind == "csource":
            return (
                (lambda value, key: self._cursor_source(value, key)),
                _math_source_readback,
            )
        if kind == "achannel":
            return (
                (lambda value, key: self._analog_source(value, key)),
                _math_source_readback,
            )
        if kind == "source":
            return (
                (lambda value, key: self._math_source(value, key, channel)),
                _math_source_readback,
            )
        if kind == "lsource":
            return (
                (lambda value, key: self._math_lsource(value, key)),
                _math_source_readback,
            )
        if kind == "rsource":
            return (
                (lambda value, key: self._reference_source(value, key)),
                _math_source_readback,
            )
        if kind == "label":
            # 実測(firmware 00.01.00): `:REFerence:LABel:CONTent 1,TESTLBL` の
            # 読み戻しは引用符無しの `TESTLBL`。つまりこの strip は**現状不要**
            # だが、他機種・他ファームで引用符が付く可能性に対して無害なので残す
            return _reference_label, (lambda text: text.strip().strip('"'))
        if isinstance(kind, tuple) and kind[0] == "pattern":
            # **実機実測**: `:TRIGger:PATTern:PATTern` / `:DURation:TYPE` は
            # ch別のリストを取る(ガイド3.27.12.1 / 3.27.13.2 の
            # `<pch1>[,<pch2>[,<pch3>[,<pch4>]]]`)。単一の列挙値ではない。
            # 読み戻しは機器が24個返すことがある(デジタル/MATHの分)が、
            # 書けるのはアナログch分だけなのでそこまでに切る
            to_token, from_token = self._afg_enum(kind[1], kind[2])
            width = self.analog_channels
            return (
                (lambda value, key: _pattern_token(value, key, to_token, width)),
                (
                    lambda text: [
                        from_token(part.strip())
                        for part in text.split(",")[:width]
                        if part.strip()
                    ]
                ),
            )
        if kind == "bitmask":
            # **実機実測**: `:TRIGger:LIN:DATA?` は2進のマスク文字列を返す
            # (`'01100100'` / 未設定ビットは `X` で `'XXXXXXXX'`)。ガイドは
            # 「整数を返す」と書いているが実機は違う。書き込みは整数のまま
            return (
                (lambda value, key: _math_int(value, key, 0, 2**64 - 1)),
                _bitmask_readback,
            )
        if kind == "threshold":
            # **実機実測**: `:MEASure:SETup:MAX 88.0` は黙って上限へ張り付く。
            # ガイド3.17.9-11 の型は Integer で、percentモードは小数形を誤解釈
            # する(エラーは積まれない)。absoluteモードは小数が要るが整数形も
            # 受理するので、「整数値のときだけ小数点を落とす」で両立する
            return (
                (lambda value, key: _threshold_token(key, value)),
                parse_nr3,
            )
        if isinstance(kind, tuple) and kind[0] == "choice":
            # 飛び飛びの数値選択肢(RS232のストップビット 1/1.5/2 など)。
            # decode.py の `_choice` と同じ考え方を項目表からも使えるようにする
            allowed = kind[1]
            return (
                (lambda value, key: _choice_token(value, key, allowed)),
                parse_nr3,
            )
        if isinstance(kind, tuple) and kind[0] == "int":
            _, low, high = kind
            return (
                (lambda value, key: _math_int(value, key, low, high)),
                (lambda text: int(parse_nr3(text))),
            )
        _, dialect_key, what = kind  # ("enum", 方言キー, 説明)
        return self._afg_enum(dialect_key, what)

    def _command_plan(
        self,
        prefix: str,
        channel: int,
        items: tuple[tuple[str, str, object], ...],
        values: dict,
        argument: str | None = None,
    ) -> list[tuple[str, str, str, object]]:
        """指定された項目だけの送信計画(検証はここで全て済ませる)。

        `argument` は**枠番号をコマンド引数で取る**サブシステム(`:REFerence`)
        のためのもの。指定すると `<接頭辞><パス> <argument>,<値>` /
        `<接頭辞><パス>? <argument>` の形になる。
        """
        plan: list[tuple[str, str, str, object]] = []
        head = "" if argument is None else f"{argument},"
        tail = "" if argument is None else f" {argument}"
        for key, path, kind in items:
            value = values.get(key)
            if value is None:
                continue
            to_scpi, from_scpi = self._converter(kind, channel)
            plan.append(
                (
                    key,
                    f"{prefix}{path} {head}{to_scpi(value, key)}",
                    f"{prefix}{path}?{tail}",
                    from_scpi,
                )
            )
        return plan

    def _math_sub_plan(
        self,
        prefix: str,
        channel: int,
        name: str,
        values: object,
        items: tuple[tuple[str, str, object], ...],
    ) -> list[tuple[str, str, str, object]]:
        """`fft` / `filter` サブ辞書の送信計画(未知キーは送信前に拒否)。"""
        if not isinstance(values, dict):
            raise _invalid(f"{name} is not an object: {values!r}", {name: values})
        allowed = [key for key, _, _ in items]
        unknown = sorted(key for key in values if key not in allowed)
        if unknown:
            raise _invalid(
                f"unknown {name} setting: {unknown}",
                {"unknown": unknown, "allowed": allowed},
            )
        return self._command_plan(prefix, channel, items, values)

    def configure_math(
        self,
        channel: int = 1,
        *,
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
        # `filter` は組込み名と重なるが、Tool引数名(ガイドの :FILTer)を優先する
        filter: dict | None = None,
    ) -> dict:
        """MATH演算を設定し、read-backした適用値を返す。

        全ての検証を送信前に済ませる(不正なチャンネル番号1発で実機のSCPIサーバーが
        沈黙するため)。送信順は表示ONを**最初**、表示OFFを**最後**に置き、その間は
        OPERator → SOURce1/2 → LSOurce1/2 → FFT → FILTer → SCALe → OFFSet →
        INVert。**この順序を単純化してはならない**: 実機は表示を OFF → ON に戻した
        瞬間に縦軸を再計算して SCALe / OFFSet の書き込みを捨てるため、表示ONを先に
        送ることで再計算を書き込みより前に起こす必要がある(MHO98実測 —
        docs/verification/mho98-math.md (e)。なお表示OFF中の書き込み無視quirk
        — AFGの変調STATeと同族 — はMATHには存在しないと実測で確定した)。

        **演算子と引数の結合制約はホストで検証しない。** 論理演算での `scale` や
        FFTでの `offset_v` のように演算子によって無効になる項目(ガイド3.16.7/
        3.16.8)は機器のエラーキューに委ね、`applied` と `requested` の突合で
        呼び出し側が検出する。
        """
        number, prefix = self._math_prefix(channel)
        values = {
            "operator": operator,
            "source1": source1,
            "source2": source2,
            "lsource1": lsource1,
            "lsource2": lsource2,
            "scale": scale,
            "offset_v": offset_v,
            "invert": invert,
        }
        if (
            display is None
            and all(value is None for value in values.values())
            and fft is None
            and filter is None
        ):
            raise _invalid(
                "No item to change was specified "
                f"(specify at least one of display / {' / '.join(values)} / fft / "
                "filter)",
                {"channel": number},
            )

        # 先に全項目を検証してから送る(1項目でも不正なら1コマンドも送らない)
        display_entry = None
        if display is not None:
            display_entry = (
                "display",
                f"{prefix}:DISPlay {_math_bool(display, 'display')}",
                f"{prefix}:DISPlay?",
                parse_bool,
            )
        # (返却先のサブ辞書名 or None, 送信エントリ)を送信順に並べる
        plan: list[tuple[str | None, tuple[str, str, str, object]]] = [
            (None, entry) for entry in self._command_plan(prefix, number, _MATH_ITEMS, values)
        ]
        if fft is not None:
            plan += [
                ("fft", entry)
                for entry in self._math_sub_plan(
                    prefix, number, "fft", fft, _MATH_FFT_ITEMS
                )
            ]
        if filter is not None:
            plan += [
                ("filter", entry)
                for entry in self._math_sub_plan(
                    prefix, number, "filter", filter, _MATH_FILTER_ITEMS
                )
            ]
        plan += [
            (None, entry)
            for entry in self._command_plan(prefix, number, _MATH_VERTICAL_ITEMS, values)
        ]
        if display_entry is not None:
            # 表示ONは先頭、OFFは末尾(表示OFF中の書き込み無視quirk対策)
            if display:
                plan.insert(0, (None, display_entry))
            else:
                plan.append((None, display_entry))

        applied: dict[str, object] = {"channel": number}
        sub: dict[str, dict] = {"fft": {}, "filter": {}}
        for dest, (key, set_cmd, query, from_scpi) in plan:
            value = from_scpi(self.session.set_and_verify(set_cmd, query))
            if dest is None:
                applied[key] = value
            else:
                sub[dest][key] = value
        for name, values_applied in sub.items():
            if values_applied:
                applied[name] = values_applied
        return applied

    def get_math_config(self, channel: int) -> dict:
        """MATH演算の現在設定を意味的なキーで返す(演算子に応じた条件付き読み取り)。

        クエリ数を抑えると同時に、未検証のサブツリーを不用意に突かないため、
        読むのは「その演算子で意味を持つ項目」だけに絞る:

        - 常に: display(**最初**に読む)/ operator / source1 / source2 / invert
        - scale・offset_v: 論理演算・FFT以外(ガイド3.16.7/3.16.8)
        - lsource1・lsource2: 論理演算のみ
        - fft サブツリー: operator=fft のみ。`peaks` は探索が有効なときのみ
        - filter サブツリー: フィルタ演算子のみ
        """
        number, prefix = self._math_prefix(channel)
        items = {
            key: (path, kind) for key, path, kind in _MATH_ITEMS + _MATH_VERTICAL_ITEMS
        }

        def read(key: str) -> object:
            path, kind = items[key]
            return self._math_read(prefix, number, path, kind)

        config: dict[str, object] = {
            "channel": number,
            "display": parse_bool(self.session.query(f"{prefix}:DISPlay?")),
        }
        for key in ("operator", "source1", "source2", "invert"):
            config[key] = read(key)

        operator = config["operator"]
        if operator in _MATH_LOGIC_OPERATORS:
            conditional = ("lsource1", "lsource2")
        elif operator == "fft":
            conditional = ()  # SCALe / OFFSet はFFT配下(:FFT:SCALe)にある
        else:
            conditional = ("scale", "offset_v")
        for key in conditional:
            config[key] = read(key)

        if operator == "fft":
            fft = {
                key: self._math_read(prefix, number, path, kind)
                for key, path, kind in _MATH_FFT_ITEMS
            }
            config["fft"] = fft
            if fft["search_enabled"]:
                # ピーク表だけは複数行応答(実機実測: 改行区切り + 終端の空行)。
                # 1行読みでは残りが受信バッファに居座り、以降のqueryがdesyncする。
                peaks, warnings = parse_fft_peaks(
                    "\n".join(
                        self.session.query_lines(f"{prefix}{_MATH_FFT_PEAKS_PATH}")
                    )
                )
                config["peaks"] = peaks
                if warnings:
                    config["peak_warnings"] = warnings
        if operator in _MATH_FILTER_OPERATORS:
            config["filter"] = {
                key: self._math_read(prefix, number, path, kind)
                for key, path, kind in _MATH_FILTER_ITEMS
            }
        return config

    def _math_read(self, prefix: str, channel: int, path: str, kind: object) -> object:
        """MATH項目1件を読んで意味的な値へ変換する。"""
        _, from_scpi = self._converter(kind, channel)
        return from_scpi(self.session.query(f"{prefix}{path}?"))

    def get_math_operator(self, channel: int) -> str:
        """MATHチャンネルの演算子だけを意味的な名前で返す(問い合わせ1本)。

        波形取得の経路が「FFTトレースかどうか」を判定するための最小の読み取り。
        """
        _, prefix = self._math_prefix(channel)
        _, from_scpi = self._afg_enum("math_operators", "the math operator")
        return from_scpi(self.session.query(f"{prefix}:OPERator?"))

    def get_math_fft_start_hz(self, channel: int) -> float:
        """FFTトレースの開始周波数だけを返す(問い合わせ1本)。

        プリアンブルの xorigin は**時間軸の値が残る**(実機実測: FFT表示中でも
        -2.5e-3 s)。周波数軸の原点はこのフィールドからしか得られない。
        """
        number, prefix = self._math_prefix(channel)
        return float(self._math_read(prefix, number, ":FFT:FREQuency:STARt", "number"))

    # -- カーソル測定(ガイド3.8章)----------------------------------------

    def _cursor_source(self, value: object, key: str) -> str:
        """カーソルのソーストークンを検証して送信形へ(ガイド3.8.3/3.8.9/3.8.10)。

        受理するのは `CH1`-`CH<analog_channels>` / `MATH1`-`MATH<math_channels>` /
        `NONE`。REF波形・デジタルchはこのコマンドの値域に無い。
        """
        if not isinstance(value, str):
            raise _invalid(f"{key} is not a string: {value!r}", {"key": key, "value": value})
        token = value.strip()
        if token.upper() == "NONE":
            return "NONE"
        match = _MATH_SOURCE_RE.match(token)
        if match is not None:
            number = int(match.group(1))
            available = self.math_channels
            if not 1 <= number <= available:
                raise _invalid(
                    f"{key} math channel {value!r} does not exist "
                    f"(this model has MATH1-MATH{available})",
                    {"key": key, "value": value, "math_channels": available},
                )
            return f"MATH{number}"
        if _CHANNEL_RE.match(token) is None:
            raise _invalid(
                f"cannot interpret {key}: {value!r} (e.g. 'CH1', 'MATH1' or 'NONE')",
                {"key": key, "value": value},
            )
        return f"CHANnel{self._channel_number(token)}"

    def _analog_source(self, value: object, key: str) -> str:
        """アナログchのみを受理する(`:DVM:SOURce` / `:HISTogram:SOURce`)。

        ガイドが値域をアナログchに限っているため、デジタルchは**送信前**に拒否する
        (カウンタの `:COUNter:SOURce` は D0-D15 も取るので "lsource" 側)。
        """
        if isinstance(value, str) and _DIGITAL_SOURCE_RE.match(value.strip()):
            raise _invalid(
                f"{key} must be an analog channel: {value!r} "
                "(this subsystem does not accept digital channels)",
                {"key": key, "value": value},
            )
        return f"CHANnel{self._channel_number(value)}"

    def _cursor_mode(self) -> str:
        """現在の `:CURSor:MODE` を意味的な名前で返す(問い合わせ1本)。"""
        _, from_scpi = self._afg_enum("cursor_modes", "the cursor mode")
        return from_scpi(self.session.query(":CURSor:MODE?"))

    def _cursor_subtree(
        self, mode: str
    ) -> tuple[str, tuple[tuple[str, str, object], ...]]:
        entry = _CURSOR_SUBTREES.get(mode)
        if entry is None:
            raise _invalid(
                "cursor positions and sources can only be set while the cursor mode "
                f"is 'manual' or 'track' (the mode is {mode!r})",
                {"mode": mode, "allowed": sorted(_CURSOR_SUBTREES)},
            )
        return entry

    def configure_cursor(
        self,
        *,
        mode: str | None = None,
        # `type` は組込み名と重なるが、Tool引数名(ガイドの :TYPE)を優先する
        type: str | None = None,
        source: str | None = None,
        source1: str | None = None,
        source2: str | None = None,
        ax: float | None = None,
        ay: float | None = None,
        bx: float | None = None,
        by: float | None = None,
    ) -> dict:
        """カーソルを設定し、read-backした適用値を返す(ガイド3.8章)。

        位置・ソースの書き込み先は **MANual / TRACk のどちらのサブツリーか**で
        決まる。`mode` を指定すればそれ、省略すれば現在の `:CURSor:MODE?` を
        1本読んで決める。OFF / XY では書き込み先が定まらないため送信前に拒否する
        (`:CURSor:XY:*` はM2スコープ外)。`mode` は先頭に送る。

        `type` / `source` はMANual、`source1` / `source2` はTRACk専用の項目で、
        サブツリー違いの指定は送信前に拒否する(取り違えを黙って無視しない)。
        """
        self._require("cursor", "cursor measurements")
        values = {
            "type": type,
            "source": source,
            "source1": source1,
            "source2": source2,
            "ax": ax,
            "ay": ay,
            "bx": bx,
            "by": by,
        }
        if mode is None and all(value is None for value in values.values()):
            raise _invalid(
                "No item to change was specified "
                f"(specify at least one of mode / {' / '.join(values)})",
                {},
            )

        # 先に全項目を検証してから送る(1項目でも不正なら1コマンドも送らない)
        plan: list[tuple[str, str, str, object]] = []
        if mode is not None:
            to_scpi, from_scpi = self._afg_enum("cursor_modes", "the cursor mode")
            plan.append(
                (
                    "mode",
                    f":CURSor:MODE {to_scpi(mode, 'mode')}",
                    ":CURSor:MODE?",
                    from_scpi,
                )
            )
        if any(value is not None for value in values.values()):
            subtree = mode if mode is not None else self._cursor_mode()
            prefix, items = self._cursor_subtree(subtree)
            allowed = [key for key, _, _ in items]
            unknown = sorted(
                key for key, value in values.items()
                if value is not None and key not in allowed
            )
            if unknown:
                raise _invalid(
                    f"the {subtree} cursor does not have these settings: {unknown}",
                    {"mode": subtree, "unknown": unknown, "allowed": allowed},
                )
            plan += self._command_plan(prefix, 0, items, values)

        applied: dict[str, object] = {}
        for key, set_cmd, query, from_scpi in plan:
            applied[key] = from_scpi(self.session.set_and_verify(set_cmd, query))
        return applied

    def get_cursor_config(self) -> dict:
        """カーソルの現在設定を返す。読むのは**現在のモードのサブツリーだけ**。

        OFF / XY では `{"mode": ...}` のみを返す(位置サブツリーを持たないため)。
        """
        self._require("cursor", "cursor measurements")
        mode = self._cursor_mode()
        config: dict[str, object] = {"mode": mode}
        entry = _CURSOR_SUBTREES.get(mode)
        if entry is not None:
            prefix, items = entry
            for key, path, kind in items:
                _, from_scpi = self._converter(kind, 0)
                config[key] = from_scpi(self.session.query(f"{prefix}{path}?"))
        return config

    def get_cursor_measurement(self) -> dict:
        """カーソルの読み値(A/B位置・ΔX・ΔY・1/ΔX)を返す。

        値は現在のモードのサブツリー(MANual / TRACk)から読む。**OFF / XY では
        読める値が無いため `{"mode": ...}` だけを返し、値のキーは付けない**
        (非活性のサブツリーを問い合わせない — 未検証ニモニックを突かない方針)。
        測定不能を示す番兵値(±9.9E37。例: ΔX=0 のときの 1/ΔX)は `None` にする。
        """
        self._require("cursor", "cursor measurements")
        mode = self._cursor_mode()
        result: dict[str, object] = {"mode": mode}
        entry = _CURSOR_SUBTREES.get(mode)
        if entry is None:
            return result
        prefix, _ = entry
        for key, path in _CURSOR_READOUTS:
            result[key] = self._readout(f"{prefix}{path}?")
        return result

    def _readout(self, query: str) -> float | None:
        """読み値クエリ1本。**空応答**と測定不能の番兵値(±9.9E37)は `None`。

        空応答はM2実機実測の癖(無効化中の `:DVM:CURRent?`)。非活性の測定系に
        値が無いのは正常であって機器故障ではないため、ここで吸収する。
        `parse_nr3` 側を緩めないのは、他の全経路(設定値のread-back等)では
        解釈できない応答が本当に異常であり、例外のままが正しいため。
        """
        response = self.session.query(query)
        if not response.strip():
            return None
        value = parse_nr3(response)
        return None if abs(value) >= INVALID_MEASUREMENT else value

    # -- 周波数カウンタ・電圧計(ガイド3.7 / 3.10)-------------------------

    def _meter(self, kind: object) -> tuple[str, tuple[tuple[str, str, object], ...]]:
        """種別名を (SCPI接頭辞, 項目表) へ。能力ゲートもここで通す。"""
        entry = _METERS.get(kind) if isinstance(kind, str) else None
        if entry is None:
            raise _invalid(
                f"unknown meter kind: {kind!r} (allowed: {sorted(_METERS)})",
                {"kind": kind, "allowed": sorted(_METERS)},
            )
        capability, prefix, items, what = entry
        self._require(capability, what)
        return prefix, items

    def configure_meter(
        self,
        kind: str,
        *,
        enabled: bool | None = None,
        source: str | None = None,
        mode: str | None = None,
        digits: int | None = None,
        totalize_enabled: bool | None = None,
    ) -> dict:
        """周波数カウンタ(`kind="counter"`)/ 電圧計(`kind="dvm"`)を設定する。

        `digits`(`:COUNter:NDIGits`)と `totalize_enabled`
        (`:COUNter:TOTalize:ENABle`)はカウンタ専用で、電圧計に指定すれば
        送信前に拒否する。ソースの値域も種別で異なる(カウンタは D0-D15 も可、
        電圧計はアナログchのみ — ガイド3.10.3)。

        **モードとの結合制約はホストで検証しない。** Totalizeモードでの `digits`
        や `totalize_enabled` のようにモードによって無効になる項目(ガイド3.7.5/
        3.7.6)は機器のエラーキューに委ね、`applied` と `requested` の突合で
        呼び出し側が検出する(MATHの演算子/引数と同じ方針)。
        """
        prefix, items = self._meter(kind)
        values = {
            "enabled": enabled,
            "source": source,
            "mode": mode,
            "digits": digits,
            "totalize_enabled": totalize_enabled,
        }
        allowed = [key for key, _, _ in items]
        unknown = sorted(
            key for key, value in values.items()
            if value is not None and key not in allowed
        )
        if unknown:
            raise _invalid(
                f"the {kind} meter does not have these settings: {unknown}",
                {"kind": kind, "unknown": unknown, "allowed": allowed},
            )
        if all(value is None for value in values.values()):
            raise _invalid(
                f"No item to change was specified (specify at least one of "
                f"{' / '.join(allowed)})",
                {"kind": kind},
            )

        applied: dict[str, object] = {"kind": kind}
        for key, set_cmd, query, from_scpi in self._command_plan(
            prefix, 0, items, values
        ):
            applied[key] = from_scpi(self.session.set_and_verify(set_cmd, query))
        return applied

    def get_meter_config(self, kind: str) -> dict:
        """周波数カウンタ / 電圧計の現在設定を意味的なキーで返す。"""
        prefix, items = self._meter(kind)
        config: dict[str, object] = {"kind": kind}
        for key, path, item_kind in items:
            _, from_scpi = self._converter(item_kind, 0)
            config[key] = from_scpi(self.session.query(f"{prefix}{path}?"))
        return config

    def get_meter_value(self, kind: str) -> float | None:
        """現在値を返す。計が**無効なら現在値を問い合わせずに** `None`。

        M2実機実測: 無効な電圧計の `:DVM:CURRent?` は空応答を返し、そのまま
        パースすると `SCPI_ERROR`(機器故障に見える)になる。無効な計を読むのは
        普通の操作なので、`get_math_config` と同じ条件付き読み取りにして
        `:ENABle?` を先に1本読む(有効なら合計2本)。

        単位はモード依存(カウンタ: Hz / s / 件数、電圧計: V)なので、必要なら
        呼び出し側が `get_meter_config` のモードと組み合わせる。測定不能の
        番兵値(±9.9E37)も `None`。
        """
        prefix, _ = self._meter(kind)
        if not parse_bool(self.session.query(f"{prefix}:ENABle?")):
            return None
        return self._readout(f"{prefix}{_METER_VALUE_PATH}")

    def clear_counter_totalize(self) -> None:
        """総カウントをクリアする(ガイド3.7.7。Totalizeモード時のみ有効)。

        引数もread-backも無い動作コマンド(run/stopと同じ扱い)。モードとの
        結合制約は機器のエラーキューに委ねる。
        """
        self._require("frequency_counter", "the frequency counter")
        self.session.write_checked(":COUNter:TOTalize:CLEar")

    # -- ヒストグラム(ガイド3.11章)----------------------------------------

    def configure_histogram(
        self,
        *,
        enabled: bool | None = None,
        # `type` は組込み名と重なるが、Tool引数名(ガイドの :TYPE)を優先する
        type: str | None = None,
        source: str | None = None,
        height: int | None = None,
        left_s: float | None = None,
        right_s: float | None = None,
        bottom_v: float | None = None,
        top_v: float | None = None,
    ) -> dict:
        """ヒストグラムを設定し、read-backした適用値を返す(ガイド3.11章)。

        `left_s < right_s` / `bottom_v < top_v` はガイド明記の制約だが、検証は
        **同一呼び出しで両端を指定したときだけ**行う(片側だけの指定は機器側の
        現在値との突合が必要なため機器に委ねる)。そのため片側だけを動かして
        現在の反対側を追い越すと、機器がエラーを返して `SCPI_ERROR` になる —
        その場合は両端を同時に指定して呼び直すこと。
        """
        self._require("histogram", "the histogram")
        values = {
            "enabled": enabled,
            "type": type,
            "source": source,
            "height": height,
            "left_s": left_s,
            "right_s": right_s,
            "bottom_v": bottom_v,
            "top_v": top_v,
        }
        if all(value is None for value in values.values()):
            raise _invalid(
                f"No item to change was specified (specify at least one of "
                f"{' / '.join(values)})",
                {},
            )
        # 先に全項目を検証してから送る(型・値域の検証は計画の組み立てが担う)
        plan = self._command_plan(_HISTOGRAM_PREFIX, 0, _HISTOGRAM_ITEMS, values)
        for low_key, high_key in _HISTOGRAM_RANGE_PAIRS:
            low, high = values[low_key], values[high_key]
            if low is not None and high is not None and float(low) >= float(high):
                raise _invalid(
                    f"{low_key} must be smaller than {high_key}: {low!r} >= {high!r}",
                    {low_key: low, high_key: high},
                )

        applied: dict[str, object] = {}
        for key, set_cmd, query, from_scpi in plan:
            applied[key] = from_scpi(self.session.set_and_verify(set_cmd, query))
        return applied

    def get_histogram_config(self) -> dict:
        """ヒストグラムの現在設定を意味的なキーで返す。"""
        self._require("histogram", "the histogram")
        config: dict[str, object] = {}
        for key, path, kind in _HISTOGRAM_ITEMS:
            _, from_scpi = self._converter(kind, 0)
            config[key] = from_scpi(
                self.session.query(f"{_HISTOGRAM_PREFIX}{path}?")
            )
        return config

    def get_histogram_result(self) -> dict:
        """統計結果を返す(ガイド3.11.9)。**生応答を必ず残す**。

        ヒストグラムが無効なら `raw` は空文字で `warnings` に理由を積む(下記)。

        M2実機実測の応答は `[Sum:30.37khits, ..., Max:1.562V, ...]` の**1行**で、
        **機器自身が列ラベルを持つ**。FFTピーク表と違い終端の空行が無いため
        `query_lines`(空行まで読む)は使えない — 使うと実機ではタイムアウトまで
        固まる。`query` で1行読み、`parse_histogram_result` が正規化キーの
        `stats`(数値 + 必要なら `<キー>_unit`)へ変換する。解釈は fail-open で、
        読めなかった項目は `warnings` に積むだけ(`raw` は常に返る)。
        """
        self._require("histogram", "the histogram")
        # 無効時は統計クエリ自体を送らない。M2実機実測では `[]` を返しつつ
        # **エラーキューに -200 を積む**(沈黙はしない)ため、送ってしまうと
        # 共有状態が汚れ、次の無関係な書き込みの検査に化けて出る。
        if not parse_bool(self.session.query(f"{_HISTOGRAM_PREFIX}:ENABle?")):
            return {
                "raw": "",
                "warnings": [
                    "the histogram is disabled, so no statistics were read: "
                    "enable it with configure_histogram first"
                ],
            }
        raw = self.session.query(f"{_HISTOGRAM_PREFIX}{_HISTOGRAM_RESULT_PATH}")
        stats, warnings = parse_histogram_result(raw)
        result: dict[str, object] = {"raw": raw}
        if stats:
            result["stats"] = stats
        if warnings:
            result["warnings"] = warnings
        return result

    def reset_histogram(self) -> None:
        """ヒストグラムの統計をリセットする(ガイド3.11.10)。

        引数もread-backも無い動作コマンド(run/stopと同じ扱い)。
        """
        self._require("histogram", "the histogram")
        self.session.write_checked(f"{_HISTOGRAM_PREFIX}:RESet")

    # -- リファレンス波形(ガイド3.20章)------------------------------------

    def _reference_slot(self, ref: object) -> int:
        """枠番号を検証して返す。能力ゲートもここで通す(`_math_prefix` と同形)。"""
        count = self.ref_channels
        if count < 1:
            raise _unsupported(
                "this model's profile does not support reference waveforms",
                {"capability": "ref_channels", "profile": self.profile.name},
            )
        if isinstance(ref, bool) or not isinstance(ref, int) or not 1 <= ref <= count:
            raise _invalid(
                f"reference waveform {ref!r} does not exist "
                f"(this model has 1-{count})",
                {"ref": ref, "ref_channels": count},
            )
        return ref

    def _reference_source(self, value: object, key: str) -> str:
        """`:REFerence:SOURce` のトークンを検証して送信形へ(ガイド3.20.2)。

        受理するのは `D0`-`D<digital_channels-1>` / `CH1`-`CH<analog_channels>` /
        `MATH1`-`MATH<math_channels>`。REF自身も `NONE` も値域に無い。存在検証は
        デジタル/MATHそれぞれ既存のヘルパへ委ねる。

        **ガイドのRemark「現在有効なチャンネルのみソースに選べる」は、この
        ファームでは成り立たない**(実測 firmware 00.01.00: CH4の表示をOFFに
        してから `:REFerence:SOURce 1,CHANnel4` を送るとエラー無しで受理され、
        `CHAN4` が読み戻った)。したがってホスト側では表示状態を検証しない。
        """
        if not isinstance(value, str):
            raise _invalid(f"{key} is not a string: {value!r}", {"key": key, "value": value})
        token = value.strip()
        if _DIGITAL_SOURCE_RE.match(token) is not None:
            return self._math_lsource(token, key)
        if _MATH_SOURCE_RE.match(token) is not None:
            return self._cursor_source(token, key)
        if _CHANNEL_RE.match(token) is None:
            raise _invalid(
                f"cannot interpret {key}: {value!r} (e.g. 'CH1', 'MATH1' or 'D0')",
                {"key": key, "value": value},
            )
        return f"CHANnel{self._channel_number(token)}"

    def configure_reference(
        self,
        ref: int,
        *,
        source: str | None = None,
        scale: float | None = None,
        offset_v: float | None = None,
        color: str | None = None,
        label: str | None = None,
        label_display: bool | None = None,
    ) -> dict:
        """リファレンス波形の1枠を設定し、read-backした適用値を返す。

        枠番号は**コマンド引数**で渡す(`:REFerence:VSCale <ref>,<scale>`、
        問い合わせは `:REFerence:VSCale? <ref>`)。`:MATH<n>` のようにニモニックへ
        埋め込む形ではない。送信順は SOURce → VSCale → VOFFset → COLor →
        LABel:CONTent → LABel:ENABle。

        `label_display` だけは**全枠共通のスイッチ**(ガイド3.20.6)で枠引数を
        取らない。この呼び出しの枠だけでなく全リファレンス波形のラベル表示が
        切り替わる。
        """
        number = self._reference_slot(ref)
        values = {
            "source": source,
            "scale": scale,
            "offset_v": offset_v,
            "color": color,
            "label": label,
            "label_display": label_display,
        }
        if all(value is None for value in values.values()):
            raise _invalid(
                f"No item to change was specified (specify at least one of "
                f"{' / '.join(values)})",
                {"ref": number},
            )

        # 先に全項目を検証してから送る(1項目でも不正なら1コマンドも送らない)
        plan = self._command_plan(
            _REFERENCE_PREFIX, 0, _REFERENCE_ITEMS, values, argument=str(number)
        )
        plan += self._command_plan(
            _REFERENCE_PREFIX, 0, _REFERENCE_GLOBAL_ITEMS, values
        )

        applied: dict[str, object] = {"ref": number}
        for key, set_cmd, query, from_scpi in plan:
            applied[key] = from_scpi(self.session.set_and_verify(set_cmd, query))
        return applied

    def get_reference_config(self, ref: int) -> dict:
        """リファレンス波形1枠の現在設定を意味的なキーで返す。

        `label_display` は全枠共通のスイッチなので、どの枠を読んでも同じ値になる
        (この枠のラベルが見えるかどうかを決めるのはこの値なので、枠の設定と
        一緒に返す)。**保存済みの波形があるかどうかを問い合わせるコマンドは
        存在しない**ため、そこは返せない。

        実測(firmware 00.01.00): 枠ごとの問い合わせは全10枠とも正常応答し、
        エラーキューは終始 `0,"No error"` だった(沈黙も応答のずれも無し)。
        全枠を舐める `get_reference_state` の集約読みはそのまま安全に行える。
        """
        number = self._reference_slot(ref)
        config: dict[str, object] = {"ref": number}
        for key, path, kind in _REFERENCE_ITEMS:
            _, from_scpi = self._converter(kind, 0)
            config[key] = from_scpi(
                self.session.query(f"{_REFERENCE_PREFIX}{path}? {number}")
            )
        for key, path, kind in _REFERENCE_GLOBAL_ITEMS:
            _, from_scpi = self._converter(kind, 0)
            config[key] = from_scpi(self.session.query(f"{_REFERENCE_PREFIX}{path}?"))
        return config

    def save_reference(self, ref: int) -> None:
        """現在の波形を指定枠へ保存する(ガイド3.20.8)。

        **不可逆**: その枠に入っていた波形は失われ、元に戻す手段も「入っているか
        どうか」を問い合わせる手段も機器に無い。read-backできない書き込み専用の
        動作コマンド(run/stopと同じ扱い)。
        """
        self.session.write_checked(
            f"{_REFERENCE_PREFIX}:SAVE {self._reference_slot(ref)}"
        )

    def reset_reference(self, ref: int) -> None:
        """指定枠の垂直スケールと位置を既定へ戻す(ガイド3.20.9)。

        read-backできない書き込み専用の動作コマンド。保存済みの波形自体は消えない。

        **保存済み波形の無い枠では何も起きないことがある**(実測
        firmware 00.01.00: 一度も `:REFerence:SAVE` していない枠1に
        `VSCale 1,0.5` / `VOFFset 1,0.2` を書いてから `:RESet 1` を送ったところ、
        エラーは積まれないまま値も 5.000000E-1 / 2.000000E-1 のままで、既定の
        5.000000E-2 / 0.000000 には戻らなかった)。**保存の有無が条件だと確定した
        わけではない**(観測は1件)。呼び出し側は「戻ったはず」と仮定せず、必要なら
        `get_reference_config` で読み直すこと。
        """
        self.session.write_checked(
            f"{_REFERENCE_PREFIX}:RESet {self._reference_slot(ref)}"
        )

    # -- Acquisition ------------------------------------------------------

    def run(self) -> None:
        self.session.write_checked(":RUN")

    def stop(self) -> None:
        self.session.write_checked(":STOP")

    def single(self) -> None:
        self.session.write_checked(":SINGle")

    def autoset(self) -> None:
        """オートセットアップ。信号系統を大きく変えるため、承認は上位の責務。

        ニモニックは世代で分岐する(MHO900/DHO系: :AUToset。旧世代DS1000Z等:
        :AUToscale)ためdialect宣言必須。かつてハードコードしていた :AUToscale は
        MHO900では **:SYSTem:AUToscale(AUTOキーの有効化スイッチ)しか存在しない**
        未定義ヘッダで、実機に送ればSCPIサーバーが沈黙するところだった
        (autoset書き込みは実機実行禁止のため未発火。docs/verification/
        mho98-autoset.md)。
        """
        command = self._required_dialect("autoset_command", "the auto setup")
        self.session.write_checked(command)
