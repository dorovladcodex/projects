#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

TO="${1:-${JOBBOT_EMAIL_TO:-dorovlad@gmail.com}}"
SUBJECT="${2:-}"
BODY_FILE="${3:-}"
ATTACHMENT_FILE="${4:-}"

if [[ -z "$SUBJECT" || -z "$BODY_FILE" ]]; then
  echo "Usage: $0 [to] <subject> <body_file> [attachment_file]" >&2
  exit 2
fi

if [[ ! -f "$BODY_FILE" ]]; then
  echo "Body file not found: $BODY_FILE" >&2
  exit 2
fi

if [[ -n "$ATTACHMENT_FILE" && ! -f "$ATTACHMENT_FILE" ]]; then
  echo "Attachment file not found: $ATTACHMENT_FILE" >&2
  exit 2
fi

if [[ -n "$ATTACHMENT_FILE" ]]; then
  echo "Automatic JobBot delivery does not support attachments; the report itself contains all new vacancies." >&2
  exit 2
fi

if [[ -n "${CODEX_BIN:-}" ]]; then
  :
elif command -v codex >/dev/null 2>&1; then
  CODEX_BIN="$(command -v codex)"
elif [[ -x "$HOME/.local/bin/codex" ]]; then
  CODEX_BIN="$HOME/.local/bin/codex"
elif [[ -x "$HOME/.codex/packages/standalone/releases/0.139.0-x86_64-unknown-linux-musl/bin/codex" ]]; then
  CODEX_BIN="$HOME/.codex/packages/standalone/releases/0.139.0-x86_64-unknown-linux-musl/bin/codex"
else
  echo "codex binary not found. Set CODEX_BIN explicitly." >&2
  exit 127
fi

DELIVERY_RESULT="${JOBBOT_GMAIL_RESULT_FILE:-$(mktemp)}"
RESULT_IS_TEMPORARY=0
if [[ -z "${JOBBOT_GMAIL_RESULT_FILE:-}" ]]; then
  RESULT_IS_TEMPORARY=1
else
  mkdir -p "$(dirname "$DELIVERY_RESULT")"
fi
cleanup() {
  if [[ "$RESULT_IS_TEMPORARY" == "1" ]]; then
    rm -f "$DELIVERY_RESULT"
  fi
}
trap cleanup EXIT

PROMPT=$(
  cat <<EOF
Send the JobBot report through the Gmail plugin now.

Requirements:
- Call the Gmail send-email tool exactly once; do not create a draft and do not run another Codex process.
- Do not read skill files or reprint the report in your response.
- Send to the literal address: $TO
- Subject: $SUBJECT
- Read this body file and use its complete contents as a text/plain MIME body: $BODY_FILE
- Do not attach files.
- Only after the Gmail tool confirms success, respond with exactly: JOBBOT_GMAIL_SENT:<message-id>
- If the Gmail tool fails, explain the failure without this success marker.
EOF
)

if ! "$CODEX_BIN" exec \
  --skip-git-repo-check \
  --cd "$ROOT_DIR" \
  "$PROMPT" 2>&1 | tee "$DELIVERY_RESULT"; then
  echo "Gmail delivery command failed." >&2
  cat "$DELIVERY_RESULT" >&2
  exit 1
fi

if ! grep -Eq 'JOBBOT_GMAIL_SENT:[[:alnum:]]+' "$DELIVERY_RESULT"; then
  echo "Gmail delivery was not confirmed by a Gmail message ID." >&2
  cat "$DELIVERY_RESULT" >&2
  exit 1
fi

cat "$DELIVERY_RESULT"
