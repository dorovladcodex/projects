# JobBot Eng Ind Architecture

`JobBot Eng Ind` is a reproducible search/report pipeline for English-friendly Senior Data Engineer roles.

## Components

- `prompts/jobbot-eng-ind-search.md` defines the Codex CLI web-search task and strict vacancy criteria.
- `schemas/vacancies.schema.json` defines the JSON contract for Codex search output.
- `src/validate_and_rank.py` normalizes, filters, deduplicates, and ranks vacancies.
- `src/jobbot_eng_ind.py` compares vacancies with `seen-vacancies.json` and creates the email report.
- `src/drive_store.py` can write report/state files to Google Drive when OAuth credentials exist.
- `src/upload_run_to_drive.py` uploads report files and `seen-vacancies.json` to Google Drive.
- `src/send_gmail.py` sends the report through Gmail API when OAuth credentials exist.
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
   - `seen-vacancies.json`
6. If `JOBBOT_ENABLE_DRIVE=1`, `src/upload_run_to_drive.py` uploads the run to Google Drive.
7. If `JOBBOT_ENABLE_GMAIL=1`, `src/send_gmail.py` sends the report to Gmail.

## State

The desired production state file is:

`Projects/JobBots/jobbot-eng-ind/state/seen-vacancies.json`

Local/manual runs can use:

`.manual-runs/<run>/seen-vacancies.json`

## Important Limitation

Google Drive and Gmail API steps require local OAuth credentials. Without those credentials the pipeline can still search, validate, rank, and generate local reports, but it cannot upload state to Drive or send email from Ubuntu.
