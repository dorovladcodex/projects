#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

TO="${1:-${JOBBOT_EMAIL_TO:-dorovlad@gmail.com}}"
SUBJECT="${2:-}"
BODY_FILE="${3:-}"
ATTACHMENT_FILE="${4:-}"
CONTENT_TYPE="${JOBBOT_EMAIL_CONTENT_TYPE:-text/plain}"

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

PROMPT=$(
  cat <<EOF
Use the Gmail plugin to send an email immediately.

Requirements:
- Send to: $TO
- Use this exact recipient address literally. Do not replace it with "me" or any self-delivery alias.
- Subject: $SUBJECT
- Use content type: $CONTENT_TYPE
- Use this exact body file: $BODY_FILE
EOF
)

if [[ -n "$ATTACHMENT_FILE" ]]; then
  PROMPT+=$'\n'"- Attach this exact file: $ATTACHMENT_FILE"
fi

PROMPT+=$'\n\n'"Do not create a draft. Send it now and report the Gmail message ID."

"$CODEX_BIN" exec \
  --skip-git-repo-check \
  --cd "$ROOT_DIR" \
  "$PROMPT"
