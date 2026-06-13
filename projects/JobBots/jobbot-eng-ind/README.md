# JobBot Eng Ind

Manual English-friendly indexed-search bot.

## Purpose

JobBot Eng Ind is used for manual searches focused on English-friendly Senior Data Engineer, Azure Databricks, Data Platform, and remote Germany / NRW hybrid roles.

It is intentionally separate from the scheduled NRW bot. It is useful when the search should prioritize weak-German-friendly roles and indexed job snippets from LinkedIn, English job boards, remote boards, aggregators, and company pages.

## Current Entry Point

```powershell
python .\src\jobbot_eng_ind.py --vacancies-json .\data\example-vacancies.json
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

This project prepares local report files. Email sending is performed through the connected Gmail connector when explicitly requested in Codex.

## CV Generation

Disabled by default. Tailored CVs are generated only when a specific vacancy number, company, or title is requested.
