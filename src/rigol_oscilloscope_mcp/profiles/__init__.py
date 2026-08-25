"""機種プロファイル(docs/device-profiles.md)。

宣言的なYAMLで機種差(capabilities / dialect・quirks / limits)を吸収する。
"""

from .loader import available_profiles, load_profile, resolve_profile
from .profile import Profile, ResolvedProfile

__all__ = [
    "Profile",
    "ResolvedProfile",
    "available_profiles",
    "load_profile",
    "resolve_profile",
]
