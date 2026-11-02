#!/usr/bin/env bash
# Launcher for the Garmin Coach MCP server declared in the project .mcp.json.
# Installs ./.venv on first use, then runs the server on stdio.
set -euo pipefail
root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
"$root/scripts/setup-venv.sh"
if [ "${CLAUDE_CODE_REMOTE:-}" = "true" ]; then
  # Cloud session: keep goals, plans and exports inside the git-ignored project folder,
  # where the Claude app can open the exported files.
  # A clone of the private data repository next to this one (see
  # docs/garmin-push-actions.md) keeps goals and plans across sessions.
  if [ -z "${GARMIN_COACH_DATA_DIR:-}" ] && [ -d "$root/../garmin-coach-data/.git" ]; then
    GARMIN_COACH_DATA_DIR="$(cd "$root/../garmin-coach-data" && pwd)"
  fi
  export GARMIN_COACH_DATA_DIR="${GARMIN_COACH_DATA_DIR:-$root/.coach-data}"
fi
exec "$root/.venv/bin/garmin-coach-mcp" "$@"
