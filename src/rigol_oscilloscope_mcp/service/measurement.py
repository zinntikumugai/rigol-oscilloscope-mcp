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
