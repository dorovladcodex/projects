#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT_DIR"

DATE_UTC="$(date -u +%Y-%m-%dT%H-%M-%SZ)"
RUN_DIR="${JOBBOT_RUN_DIR:-$ROOT_DIR/.manual-runs/$DATE_UTC JobBot Eng Ind}"
RAW_JSON="$RUN_DIR/raw-vacancies.json"
VALID_JSON="$RUN_DIR/vacancies.json"
COVERAGE_JSON="$RUN_DIR/source-coverage.json"
REJECTS_JSON="$RUN_DIR/rejected-vacancies.json"
STATE_FILE="$RUN_DIR/seen-vacancies.json"
REPORT_ROOT="$RUN_DIR/output"
PROMPT_FILE="$ROOT_DIR/prompts/jobbot-eng-ind-search.md"
SCHEMA_FILE="$ROOT_DIR/schemas/vacancies.schema.json"

mkdir -p "$RUN_DIR" "$REPORT_ROOT"

if [[ -f "$ROOT_DIR/.env" ]]; then
  set -a
  # shellcheck disable=SC1091
  source "$ROOT_DIR/.env"
  set +a
fi

PYTHON_BIN="${PYTHON_BIN:-python3}"
CODEX_BIN="${CODEX_BIN:-codex}"

"$CODEX_BIN" exec \
  --search \
  --sandbox workspace-write \
  --ask-for-approval never \
  --cd "$ROOT_DIR" \
  --output-schema "$SCHEMA_FILE" \
  -o "$RAW_JSON" \
  "$(cat "$PROMPT_FILE")"

"$PYTHON_BIN" "$ROOT_DIR/src/validate_and_rank.py" \
  --input "$RAW_JSON" \
  --output "$VALID_JSON" \
  --coverage-output "$COVERAGE_JSON" \
  --rejects-output "$REJECTS_JSON"

"$PYTHON_BIN" "$ROOT_DIR/src/jobbot_eng_ind.py" \
  --vacancies-json "$VALID_JSON" \
  --output-root "$REPORT_ROOT" \
  --state-file "$STATE_FILE"

REPORT_FILE="$(find "$REPORT_ROOT" -name email-report.txt -type f | sort | tail -n 1)"
REPORT_DIR="$(dirname "$REPORT_FILE")"

if [[ "${JOBBOT_ENABLE_DRIVE:-0}" == "1" ]]; then
  "$PYTHON_BIN" "$ROOT_DIR/src/upload_run_to_drive.py" \
    --run-dir "$REPORT_DIR" \
    --state-file "$STATE_FILE" \
    --run-name "$(basename "$RUN_DIR")" \
    --google-client-file "${GOOGLE_CLIENT_FILE:-$ROOT_DIR/credentials/google-oauth-client.json}" \
    --google-token-file "${GOOGLE_TOKEN_FILE:-$ROOT_DIR/credentials/google-token.json}"
fi

if [[ "${JOBBOT_ENABLE_GMAIL:-0}" == "1" ]]; then
  "$PYTHON_BIN" "$ROOT_DIR/src/send_gmail.py" \
    --to "${JOBBOT_EMAIL_TO:-dorovlad@gmail.com}" \
    --subject "JobBot Eng Ind - English-friendly job results - $(date +%Y-%m-%d)" \
    --body-file "$REPORT_FILE" \
    --attachment "$VALID_JSON" \
    --google-client-file "${GOOGLE_CLIENT_FILE:-$ROOT_DIR/credentials/google-oauth-client.json}" \
    --gmail-token-file "${GMAIL_TOKEN_FILE:-$ROOT_DIR/credentials/gmail-token.json}"
fi

echo "Run directory: $RUN_DIR"
echo "Validated vacancies: $VALID_JSON"
echo "State file: $STATE_FILE"
echo "Report file: $REPORT_FILE"
