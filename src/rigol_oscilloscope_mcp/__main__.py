"""エントリポイント(スタブ)。

MCPサーバー本体は後続タスクで実装する。
"""

from __future__ import annotations

import sys


def main() -> int:
    print("rigol-oscilloscope-mcp: server not yet implemented", file=sys.stderr)
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
