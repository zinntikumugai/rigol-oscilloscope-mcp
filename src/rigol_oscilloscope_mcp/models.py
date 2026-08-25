"""レイヤ間で共有する値オブジェクト。

キー名はスネークケース + SI単位サフィックス(tools.md 0.1 / Requirements.md 7.5)。
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class IdnInfo:
    """`*IDN?` の解析結果。"""

    manufacturer: str
    model: str
    serial: str
    firmware: str


@dataclass(frozen=True)
class ChannelState:
    """垂直軸(1チャンネル分)の状態。"""

    channel: str  # "CH1"〜"CH4"
    enabled: bool
    scale_v_per_div: float
    offset_v: float
    coupling: str  # "DC" | "AC" | "GND"
    # プロファイルが impedance_control を宣言しない機種では、:IMPedance? を
    # 送らない(未対応機では無応答タイムアウトになる)ため "unknown" となる。
    impedance: str  # "1M" | "50" | "unknown"
    probe_ratio: float
    bandwidth_limit: bool


@dataclass(frozen=True)
class TimebaseState:
    """水平軸の状態。取得できない値は None。"""

    scale_s_per_div: float
    position_s: float
    sample_rate_sa_per_s: float | None
    memory_depth: float | None


@dataclass(frozen=True)
class TriggerState:
    """トリガ設定と状態。"""

    type: str  # "edge"
    source: str
    level_v: float
    slope: str  # "rising" | "falling" | "either"
    sweep_mode: str  # "auto" | "normal" | "single"
    status: str  # :TRIGger:STATus? の生値(例 "TD")


@dataclass(frozen=True)
class MeasurementResult:
    """測定1項目の結果。値が得られない場合 value は None。"""

    name: str  # 意味的名(例 "frequency")
    key: str  # SI単位付きキー(例 "frequency_hz")
    value: float | None
    quality: str  # "valid" | "overflow" | "no_signal" | "unstable" | "unknown"
