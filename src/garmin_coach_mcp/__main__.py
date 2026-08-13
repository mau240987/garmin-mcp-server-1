"""Entry point for ``python -m garmin_coach_mcp``.

Equivalent in every way to the ``garmin-coach-mcp`` console script; it exists so
the server can be launched from a checkout or an environment where the script
shim is not on ``PATH`` (``uv run python -m garmin_coach_mcp``, Docker, CI).
"""

from __future__ import annotations

import sys

from garmin_coach_mcp.cli import main

if __name__ == "__main__":
    sys.exit(main())
