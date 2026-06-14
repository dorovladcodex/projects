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

With Drive and Gmail enabled:

```bash
JOBBOT_ENABLE_DRIVE=1 JOBBOT_ENABLE_GMAIL=1 bash scripts/run_jobbot_eng_ind.sh
```

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

This project prepares report files. In Codex App, email can be sent through the Gmail connector. On Ubuntu/Codex CLI, `src/send_gmail.py` can send through Gmail API when OAuth credentials are present.

## CV Generation

Disabled by default. Tailored CVs are generated only when a specific vacancy number, company, or title is requested.
