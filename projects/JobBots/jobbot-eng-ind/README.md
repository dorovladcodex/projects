# JobBot Eng Ind

English-friendly indexed-search bot for Senior Data Engineer roles.

## Purpose

JobBot Eng Ind is used for manual searches focused on English-friendly Senior Data Engineer, Azure Databricks, Data Platform, and remote Germany / NRW hybrid roles.

It is intentionally separate from the scheduled NRW bot. It is useful when the search should prioritize weak-German-friendly roles and indexed job snippets from LinkedIn, English job boards, remote boards, aggregators, and company pages.

## Current Entry Points

```powershell
python .\src\jobbot_eng_ind.py --vacancies-json .\data\example-vacancies.json
```

Ubuntu/Codex CLI pipeline:

```bash
bash scripts/run_jobbot_eng_ind.sh
```

Multi-source pipeline for scheduled runs:

```bash
bash scripts/run_scheduled_jobbot_eng_ind.sh
```

With Drive and email enabled:

```bash
JOBBOT_ENABLE_DRIVE=1 JOBBOT_ENABLE_GMAIL=1 bash scripts/run_jobbot_eng_ind.sh
```

Email sending is enabled by default in `scripts/run_jobbot_eng_ind.sh`. Scheduled and normal runners now use `codex exec` plus the Gmail plugin by default. Set `JOBBOT_ENABLE_GMAIL=0` to skip mail for a specific run.

Scheduled runs send to `dorovlad@gmail.com` by default, use the shared state file at `.manual-runs/state/seen-vacancies.json`, and include only newly discovered vacancies in the email report.

Local smoke test:

```bash
bash scripts/smoke_test.sh
```

Google Drive mode:

```powershell
python .\src\jobbot_eng_ind.py --vacancies-json .\data\example-vacancies.json --drive
```

## Output

- Google Drive project path: `Projects/JobBots/jobbot-eng-ind`
- State file: `Projects/JobBots/jobbot-eng-ind/state/seen-vacancies.json`
- Run report: `Projects/JobBots/jobbot-eng-ind/runs/yyyy-mm-dd/email-report.txt`
- Vacancy data: `Projects/JobBots/jobbot-eng-ind/runs/yyyy-mm-dd/vacancies.json`
- Resumes: `Projects/JobBots/jobbot-eng-ind/resumes/`

## Email

This project prepares report files. On Ubuntu/Codex CLI, scheduled delivery now uses the Gmail plugin through `codex exec` via `scripts/send_report_via_codex_gmail.sh`. The old `src/send_gmail.py` path remains available only as an explicit fallback when `JOBBOT_GMAIL_DELIVERY_MODE=gmail-api`.

For the Gmail API fallback only, provide Gmail API credentials locally or through environment variables:

- `GOOGLE_CLIENT_FILE`
- `GMAIL_TOKEN_FILE`
- `JOBBOT_EMAIL_TO`

## CV Generation

Disabled by default. Tailored CVs are generated only when a specific vacancy number, company, or title is requested.
