#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
LOCK_FILE="${JOBBOT_LOCK_FILE:-$ROOT_DIR/.manual-runs/jobbot-eng-ind.lock}"
WAIT_SECONDS="${JOBBOT_LOCK_WAIT_SECONDS:-3600}"
DEADLINE=$(( $(date +%s) + WAIT_SECONDS ))

while ! flock -n "$LOCK_FILE" -c true; do
  if (( $(date +%s) >= DEADLINE )); then
    echo "Timed out waiting for lock: $LOCK_FILE" >&2
    exit 1
  fi
  sleep 15
done

exec /bin/bash "$ROOT_DIR/scripts/run_scheduled_jobbot_eng_ind.sh"
