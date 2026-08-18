# JobBot Eng Ind

English-friendly indexed-search bot for Data Engineering, AI Engineering, and Oracle/Enterprise Data roles.

## Purpose

JobBot Eng Ind is used for manual searches focused on English-friendly Data Engineering, AI Engineering, and Oracle/Enterprise Data roles: Azure Databricks, Data Platform, Azure AI/OpenAI, LLM/RAG, MLOps, Oracle data integration, Oracle Cloud/OCI, Oracle ERP/EBS integration, Oracle-to-Azure migration, GoldenGate replication, logistics data, and remote Germany / NRW hybrid roles.

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

Email sending is enabled by default in `scripts/run_jobbot_eng_ind.sh`. Scheduled and normal runners use `codex exec` plus the Gmail plugin by default. A run is considered delivered only when Gmail returns a message ID; otherwise the command fails and the new vacancies remain queued for the next run. Set `JOBBOT_ENABLE_GMAIL=0` to skip mail for a specific run.

Scheduled runs send to `dorovlad@gmail.com` by default, use the shared state file at `.manual-runs/state/seen-vacancies.json`, and include only newly discovered vacancies in the email report. The state is updated only after confirmed delivery, and the email contains the report body without a JSON attachment.

## Scheduling

The production scheduler is the user-level systemd timer in `config/systemd/`. It runs at 12:00 and 16:00 Europe/Berlin, handles daylight-saving time, and is persistent across logout when user lingering is enabled. Do not also install the supplied cron entry; running both creates duplicate reports.

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

This project prepares report files. On Ubuntu/Codex CLI, scheduled delivery uses the Gmail plugin through `codex exec` via `scripts/send_report_via_codex_gmail.sh`. Each run saves `gmail-delivery-result.txt`; a missing Gmail message ID is a delivery failure. The old `src/send_gmail.py` path remains available only as an explicit fallback when `JOBBOT_GMAIL_DELIVERY_MODE=gmail-api`.

For the Gmail API fallback only, provide Gmail API credentials locally or through environment variables:

- `GOOGLE_CLIENT_FILE`
- `GMAIL_TOKEN_FILE`
- `JOBBOT_EMAIL_TO`

## CV Generation

Disabled by default. Tailored CVs are generated only when a specific vacancy number, company, or title is requested.
