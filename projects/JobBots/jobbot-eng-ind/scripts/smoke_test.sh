#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT_DIR"

RUN_DIR="$ROOT_DIR/.manual-runs/smoke-test"
mkdir -p "$RUN_DIR"

PYTHON_BIN="${PYTHON_BIN:-python3}"

"$PYTHON_BIN" "$ROOT_DIR/src/validate_and_rank.py" \
  --input "$ROOT_DIR/data/example-vacancies.json" \
  --output "$RUN_DIR/vacancies.json" \
  --coverage-output "$RUN_DIR/source-coverage.json" \
  --rejects-output "$RUN_DIR/rejected-vacancies.json"

"$PYTHON_BIN" "$ROOT_DIR/src/jobbot_eng_ind.py" \
  --vacancies-json "$RUN_DIR/vacancies.json" \
  --output-root "$RUN_DIR/output" \
  --state-file "$RUN_DIR/seen-vacancies.json"

echo "Smoke test output: $RUN_DIR"
