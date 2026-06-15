#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
LOCK_FILE="${JOBBOT_LOCK_FILE:-$ROOT_DIR/.manual-runs/jobbot-eng-ind.lock}"

mkdir -p "$(dirname "$LOCK_FILE")"

exec 9>"$LOCK_FILE"
if ! flock -n 9; then
  echo "JobBot Eng Ind is already running; skipping this scheduled run."
  exit 0
fi

export JOBBOT_EMAIL_TO="${JOBBOT_EMAIL_TO:-dorovlad@gmail.com}"
export JOBBOT_ENABLE_GMAIL="${JOBBOT_ENABLE_GMAIL:-1}"
export JOBBOT_NEW_ONLY="${JOBBOT_NEW_ONLY:-1}"
export JOBBOT_STATE_FILE="${JOBBOT_STATE_FILE:-$ROOT_DIR/.manual-runs/state/seen-vacancies.json}"
export JOBBOT_RUN_DIR="${JOBBOT_RUN_DIR:-$ROOT_DIR/.manual-runs/$(date -u +%Y-%m-%dT%H-%M-%SZ) JobBot Eng Ind Scheduled}"

bash "$ROOT_DIR/scripts/run_jobbot_eng_ind_multipass.sh"
