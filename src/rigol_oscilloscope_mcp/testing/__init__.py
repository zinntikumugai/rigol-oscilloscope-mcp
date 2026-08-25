"""テスト用インフラ(MHO98方言のフェイク機器とそのトランスポート)。"""

from .fake_scope import FakeScope, SilentTimeout
from .fake_transport import FakeTransport

__all__ = ["FakeScope", "FakeTransport", "SilentTimeout"]
