"""エントリポイント(`rigol-oscilloscope-mcp` / `python -m rigol_oscilloscope_mcp`)。

サーバー本体は server.py。ここは起動だけを担う。
"""

from __future__ import annotations

from .server import main

__all__ = ["main"]


if __name__ == "__main__":
    raise SystemExit(main())
