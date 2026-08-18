#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
TEST_DIR="$(mktemp -d)"
trap 'rm -rf "$TEST_DIR"' EXIT

BODY_FILE="$TEST_DIR/report.txt"
FAKE_CODEX="$TEST_DIR/codex"
RESULT_FILE="$TEST_DIR/result.txt"

printf '%s\n' 'Test JobBot report body' > "$BODY_FILE"
printf '%s\n' '#!/usr/bin/env bash' 'printf "%s\\n" "JOBBOT_GMAIL_SENT:abc123"' > "$FAKE_CODEX"
chmod +x "$FAKE_CODEX"

CODEX_BIN="$FAKE_CODEX" \
JOBBOT_GMAIL_RESULT_FILE="$RESULT_FILE" \
bash "$ROOT_DIR/scripts/send_report_via_codex_gmail.sh" \
  'dorovlad@gmail.com' \
  'JobBot sender test' \
  "$BODY_FILE"

grep -Eq 'JOBBOT_GMAIL_SENT:[[:alnum:]]+' "$RESULT_FILE"
