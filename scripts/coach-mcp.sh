#!/usr/bin/env bash
# Launcher for the Garmin Coach MCP server declared in the project .mcp.json.
# Installs ./.venv on first use, then runs the server on stdio.
set -euo pipefail
root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
"$root/scripts/setup-venv.sh"
if [ "${CLAUDE_CODE_REMOTE:-}" = "true" ]; then
  # Cloud session: keep goals, plans and exports inside the git-ignored project folder,
  # where the Claude app can open the exported files.
  export GARMIN_COACH_DATA_DIR="${GARMIN_COACH_DATA_DIR:-$root/.coach-data}"
fi
exec "$root/.venv/bin/garmin-coach-mcp" "$@"
