#!/usr/bin/env bash
# Save the Race Coach data (goals, plans, exports) to its private git repository.
# Usage: scripts/sync-coach-data.sh [data dir] [message]
set -euo pipefail
dir="${1:-${GARMIN_COACH_DATA_DIR:?set GARMIN_COACH_DATA_DIR or pass the data dir}}"
msg="${2:-Race Coach: update goals and plans}"
cd "$dir"
git rev-parse --is-inside-work-tree >/dev/null
if git ls-files --others --cached --exclude-standard | grep -qE '(^|/)garmin_tokens\.json$'; then
  echo "sync-coach-data: refusing to commit a Garmin token file" >&2
  exit 1
fi
git pull --rebase --autostash --quiet
git add -A goals plans exports 2>/dev/null || git add -A
if git diff --cached --quiet; then
  echo "sync-coach-data: nothing to save"
  exit 0
fi
git commit --quiet -m "$msg"
git push --quiet
echo "sync-coach-data: saved and pushed ($(git rev-parse --short HEAD))"
