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

- Output root: `D:\Job Search 2026`
- Report file: `email-report.txt`
- State file: `D:\Job Search 2026\seen-vacancies.json`

## CV Generation

Disabled by default. Tailored CVs are generated only when a specific vacancy number, company, or title is requested.
