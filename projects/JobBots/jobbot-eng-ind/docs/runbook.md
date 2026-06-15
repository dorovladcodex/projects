# JobBot Eng Ind Runbook

## Manual Run

From the project root:

```powershell
python .\src\jobbot_eng_ind.py --vacancies-json .\data\example-vacancies.json
```

Full Ubuntu/Codex CLI run:

```bash
bash scripts/run_jobbot_eng_ind.sh
```

Scheduled multi-source run:

```bash
bash scripts/run_scheduled_jobbot_eng_ind.sh
```

Full run with Drive and email:

```bash
JOBBOT_ENABLE_DRIVE=1 JOBBOT_ENABLE_GMAIL=1 bash scripts/run_jobbot_eng_ind.sh
```

Email sending is on by default in the runner. The default delivery mode is `codex-plugin`, which uses `codex exec` plus the Gmail plugin. Use `JOBBOT_ENABLE_GMAIL=0` only for a one-off run without email.

The scheduled runner sends to `dorovlad@gmail.com` by default, uses `.manual-runs/state/seen-vacancies.json` as the shared state file, and includes only new vacancies in the email report.

Before Codex source passes, the scheduled runner fetches public APIs (`Arbeitnow`, `RemoteOK`, `Remotive`) into `raw-source-passes/public-apis.json`. This gives stable direct-source coverage when those APIs expose relevant roles.

Crontab for 12:00 and 16:00 Germany time:

```cron
CRON_TZ=Europe/Berlin
0 12,16 * * * cd /home/dorovlad_codex/projects/projects/JobBots/jobbot-eng-ind && bash scripts/run_scheduled_jobbot_eng_ind.sh >> .manual-runs/scheduled.log 2>&1
```

The repository also contains scheduler templates:

- `config/jobbot-eng-ind.cron`
- `config/systemd/jobbot-eng-ind.service`
- `config/systemd/jobbot-eng-ind.timer`

Smoke test:

```bash
bash scripts/smoke_test.sh
```

Google Drive mode:

```powershell
python .\src\jobbot_eng_ind.py --vacancies-json .\data\example-vacancies.json --drive
```

## After Running

1. Review the generated `email-report.txt`.
2. The runner sends the report through the Gmail plugin automatically unless email is disabled for that run.
3. Generate a tailored CV only when a specific vacancy is selected.

## Notes

- This bot is intentionally manual.
- It should not be converted into the broad NRW scheduled automation.
- It uses the shared state file so repeated vacancies can be marked as watchlist items.
