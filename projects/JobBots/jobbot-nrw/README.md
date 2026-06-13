# JobBot NRW

Codex scheduled automation for broad NRW/Germany job monitoring.

## Purpose

JobBot NRW searches broadly across NRW, Germany-remote, recruiter, company-career, and indexed job sources for Senior Data Engineer / Oracle PL/SQL / ERP-WMS / Azure Databricks roles.

## Schedule

Monday-Friday at 18:00 Europe/Berlin.

## Codex Automation

The active local Codex automation lives at:

```text
C:\Users\dorov\.codex\automations\nrw-senior-data-oracle-job-monitor\automation.toml
```

This project stores a copy of that automation config under:

```text
automation/automation.toml
```

## Output

- Google Drive project path: `Projects/JobBots/jobbot-nrw`
- State file: `Projects/JobBots/jobbot-nrw/state/seen-vacancies.json`
- Run report: `Projects/JobBots/jobbot-nrw/runs/yyyy-mm-dd/email-report.txt`
- Vacancy data: `Projects/JobBots/jobbot-nrw/runs/yyyy-mm-dd/vacancies.json`
- Resumes: `Projects/JobBots/jobbot-nrw/resumes/`

## CV Generation

Disabled by default. Tailored CVs are generated only when a specific vacancy number, company, or title is requested.
