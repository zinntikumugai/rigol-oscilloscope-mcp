"""測定(tools.md 5章)。

ドライバが付与した品質(quality)を、LLMが読み飛ばしにくい形へ組み替える:

- `values` は SI単位付きキー → 値(無効なら None)
- `quality` は意味的名 → 品質
- 無効な項目があれば `warnings` に自然文を積み、正常値として解釈させない

未確認ニモニック(UNSUPPORTED_FEATURE)はドライバの判断をそのまま伝播する。
"""

from __future__ import annotations

from ..driver.scope import ScopeDriver, normalize_channel
from ..errors import ErrorCode, ScopeError

VALID_QUALITY = "valid"

#: 現在値の単位(ガイド3.7.4 / 3.10.4)。**カウンタの単位はモードで変わる**ため、
#: 値だけを返しても意味が定まらない。TOTalize は無次元のイベント数なのでSI単位を
#: 持たず、それが分かる語("counts")を単位の位置に置く。
_METER_UNITS: dict[str, dict[str, str]] = {
    "counter": {"frequency": "Hz", "period": "s", "totalize": "counts"},
    "dvm": {"ac_rms": "V", "dc": "V", "dc_rms": "V"},
}


def _warning(name: str) -> str:
    return f"{name} measurement is invalid (possibly no signal or not yet settled)"


def measure(driver: ScopeDriver, channel: str, measurements: list[str]) -> dict:
    """指定チャンネルの測定値を読む。"""
    # 重複除去(順序は維持)。同一項目を2度問い合わせない。
    names = list(dict.fromkeys(measurements))
    if not names:
        raise ScopeError(
            ErrorCode.INVALID_PARAMETER,
            "measurements is empty (specify at least one measurement item)",
            {"measurements": measurements},
        )

    # 先にドライバへ委ねる(チャンネル名・測定項目の検証はドライバの責務)
    results = driver.measure(channel, names)
    return {
        # 返却は正規化名で揃える(`chan1` / `1` でも "CH1")。
        # 検証済みの入力に対する表記ゆれ吸収のみで、判定はドライバが済ませている。
        "channel": normalize_channel(channel),
        "values": {r.key: r.value for r in results},
        "quality": {r.name: r.quality for r in results},
        "warnings": [_warning(r.name) for r in results if r.quality != VALID_QUALITY],
    }


def get_meter_value(driver: ScopeDriver, kind: str) -> dict:
    """周波数カウンタ / 電圧計の現在値を、モードと単位を添えて返す。

    ドライバの `get_meter_value` は裸の数値しか返さない(単位はモード依存)。
    値と単位を組にするのは本層の責務なので、設定を1度読んでモードと突き合わせる。
    設定一式をそのまま添えるのは、無効化されている計の値には意味が無く、
    どのソースを見ているかも合わせて示す必要があるため。

    **無効な計では `value` が `None` になる**(ドライバが現在値を問い合わせない
    — 実機の電圧計は無効時に空応答を返す)。「なぜ値が無いのか」は同じ返却に
    含まれる `enabled: false` が示す。

    `kind` の検証と対応機種の判定はドライバに任せる(未知の種別・未対応機では
    機器へ1コマンドも送らずに例外)。表に無いモードは単位不明として `None`。
    """
    config = driver.get_meter_config(kind)
    return {
        **config,
        "value": driver.get_meter_value(kind),
        "unit": _METER_UNITS.get(kind, {}).get(config["mode"]),
    }
