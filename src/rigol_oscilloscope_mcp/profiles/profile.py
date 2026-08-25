"""機種プロファイルの値オブジェクト(docs/device-profiles.md 2章)。

プロファイルは capabilities / dialect(quirks) / limits の3ブロックからなる。
YAMLの継承マージ後の姿を表し、生成は loader.py が担う。
"""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass(frozen=True)
class Profile:
    """継承解決済みの機種プロファイル。"""

    name: str  # プロファイル名(YAMLのファイル名。例 "mho98")
    confidence: str  # "verified" | "family" | "generic"
    capabilities: dict = field(default_factory=dict)
    dialect: dict = field(default_factory=dict)
    limits: dict = field(default_factory=dict)

    def measurement_mnemonic(self, name: str) -> str | None:
        """意味的測定名(例 "vavg")を SCPI ニモニックへ変換する。

        dialect.measurement_items に無い項目は None を返す。未確認ニモニックは
        実機へ送らない(docs/device-profiles.md 4.2)ため、呼び出し側は None を
        UNSUPPORTED_FEATURE として扱う。
        """
        items = self.dialect.get("measurement_items")
        if not isinstance(items, dict):
            return None
        mnemonic = items.get(name)
        return mnemonic if isinstance(mnemonic, str) else None

    def supports(self, capability: str) -> bool:
        """capabilities の真偽値項目(screenshot 等)を判定する。未定義は False。"""
        return bool(self.capabilities.get(capability, False))


@dataclass(frozen=True)
class ResolvedProfile:
    """`*IDN?` から解決したプロファイルと、その付随情報。"""

    profile: Profile
    unsupported_vendor: bool  # 製造者が RIGOL でない場合 True(接続は拒否しない)
