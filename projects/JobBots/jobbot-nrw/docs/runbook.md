# JobBot NRW Runbook

## Scheduled Run

JobBot NRW is run by Codex scheduled automation.

Schedule:

```text
FREQ=WEEKLY;BYDAY=MO,TU,WE,TH,FR;BYHOUR=18;BYMINUTE=0
```

## Manual Run

Ask Codex:

```text
запусти JobBot NRW
```

## Maintenance

- Keep the active Codex automation TOML and this repository copy in sync.
- Do not add Google Cloud resources for this bot unless explicitly requested.
- Do not enable automatic CV generation.
- Use Google Drive as primary storage: `Projects/JobBots/jobbot-nrw`.

## Upload Manual Run To Google Drive

From `projects/JobBots/jobbot-nrw`:

```powershell
python .\src\upload_run_to_drive.py --run-dir ".\.manual-runs\2026-06-13 Initial"
```

The helper creates or updates:

- `Projects/JobBots/jobbot-nrw/state/seen-vacancies.json`
- `Projects/JobBots/jobbot-nrw/runs/<run-folder>/email-report.txt`
- `Projects/JobBots/jobbot-nrw/runs/<run-folder>/vacancies.json`

OAuth credentials are local only:

- `credentials/google-oauth-client.json`
- `credentials/google-token.json`
