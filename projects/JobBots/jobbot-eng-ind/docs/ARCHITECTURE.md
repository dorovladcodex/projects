# JobBot Eng Ind Architecture

`JobBot Eng Ind` is a reproducible search/report pipeline for English-friendly Data Engineering, AI Engineering, and Oracle/Enterprise Data roles.

## Components

- `prompts/jobbot-eng-ind-search.md` defines the Codex CLI web-search task and strict vacancy criteria.
- `schemas/vacancies.schema.json` defines the JSON contract for Codex search output.
- `src/validate_and_rank.py` normalizes, filters, deduplicates, and ranks vacancies.
- `src/jobbot_eng_ind.py` compares vacancies with `seen-vacancies.json` and creates an email report grouped into Data Engineering, AI Engineering, and Oracle/Enterprise Data sections.
- `src/drive_store.py` can write report/state files to Google Drive when OAuth credentials exist.
- `src/upload_run_to_drive.py` uploads report files and `seen-vacancies.json` to Google Drive.
- `scripts/send_report_via_codex_gmail.sh` sends the report through `codex exec` and the Gmail plugin.
- `src/send_gmail.py` remains as an optional Gmail API fallback when local OAuth credentials exist.
- `scripts/run_jobbot_eng_ind.sh` runs the full Ubuntu/Codex CLI pipeline.
- `scripts/smoke_test.sh` verifies the local validator and report generator without web search.

## Execution Flow

1. `scripts/run_jobbot_eng_ind.sh` creates a run folder.
2. Codex CLI runs with `--search` and `prompts/jobbot-eng-ind-search.md`.
3. Codex writes raw structured output to `raw-vacancies.json`.
4. `src/validate_and_rank.py` writes:
   - `vacancies.json`
   - `source-coverage.json`
   - `rejected-vacancies.json`
5. `src/jobbot_eng_ind.py` reads `vacancies.json` and state, then writes:
   - `email-report.txt`
   - `vacancies.json`
   - `pending-new-vacancies.json`
6. If `JOBBOT_ENABLE_DRIVE=1`, `src/upload_run_to_drive.py` uploads the run to Google Drive.
7. If `JOBBOT_ENABLE_GMAIL=1`, the runner sends the report through `scripts/send_report_via_codex_gmail.sh` by default, or through `src/send_gmail.py` only when `JOBBOT_GMAIL_DELIVERY_MODE=gmail-api`.
8. Only after a confirmed delivery does `src/jobbot_eng_ind.py --commit-only` update `seen-vacancies.json`. A failed delivery leaves pending vacancies eligible for the next report.

## State

The desired production state file is:

`Projects/JobBots/jobbot-eng-ind/state/seen-vacancies.json`

Local/manual runs can use:

`.manual-runs/<run>/seen-vacancies.json`

The scheduled service is controlled by `config/systemd/jobbot-eng-ind.timer`; do not enable the cron template at the same time.

## Important Limitation

Google Drive upload still requires local OAuth credentials. Gmail API fallback also requires local OAuth credentials. The default Gmail plugin delivery path does not depend on local Gmail OAuth files.
