#!/usr/bin/env python3
"""Backward-compatible entry point.

Existing Claude Desktop configs run `python3 /path/to/garmin_mcp_server.py`. This shim
keeps that working; the implementation lives in `src/garmin_coach_mcp/`.
Prefer `pip install .` and the `garmin-coach-mcp` command.
"""

import sys
from pathlib import Path

_SRC = Path(__file__).resolve().parent / "src"
if _SRC.is_dir() and str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

from garmin_coach_mcp.cli import main  # noqa: E402

if __name__ == "__main__":
    raise SystemExit(main())
