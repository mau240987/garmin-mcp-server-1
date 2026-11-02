#!/usr/bin/env bash
# Install the server and its dev tools into ./.venv (editable), only when needed.
# Used by the cloud-session hook and by scripts/coach-mcp.sh.
# All output goes to stderr: on the MCP path, stdout is the protocol channel.
set -euo pipefail
exec 1>&2

root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$root"
hashes=.venv/.setup-venv.sha256
lock=.venv.setup.lock

# Portable lock (macOS has no flock): the hook and the MCP launcher may start together.
for _ in $(seq 1 600); do
  mkdir "$lock" 2>/dev/null && break
  sleep 0.5
done
if ! [ -d "$lock" ]; then
  echo "setup-venv: could not take the lock $root/$lock" >&2
  exit 1
fi
trap 'rmdir "$lock" 2>/dev/null || true' EXIT

# Reinstall only if this script or pyproject.toml changed since the last successful install.
if ! sha256sum --check --status "$hashes" 2>/dev/null; then
  rm -f "$hashes"
  [ -x .venv/bin/python ] || python3 -m venv .venv
  .venv/bin/python -m pip install --quiet --upgrade pip
  .venv/bin/python -m pip install --quiet -e ".[dev]"
  sha256sum scripts/setup-venv.sh pyproject.toml > "$hashes"
fi
