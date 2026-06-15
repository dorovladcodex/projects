# JobBot Eng Ind Usage

Run from `projects/JobBots/jobbot-eng-ind`:

```bash
bash scripts/run_jobbot_eng_ind.sh
```

For the scheduled multi-source run:

```bash
bash scripts/run_scheduled_jobbot_eng_ind.sh
```

To upload to Google Drive and send Gmail after the run:

```bash
JOBBOT_ENABLE_DRIVE=1 JOBBOT_ENABLE_GMAIL=1 bash scripts/run_jobbot_eng_ind.sh
```

For a local smoke test without Codex web search:

```bash
bash scripts/smoke_test.sh
```

Email sending is enabled by default in the runner. The default delivery mode is `codex-plugin`, which uses `codex exec` plus the Gmail plugin. Set `JOBBOT_ENABLE_GMAIL=0` only if you want to skip email for a specific run.

To force the old Gmail API fallback instead:

```bash
JOBBOT_GMAIL_DELIVERY_MODE=gmail-api bash scripts/run_jobbot_eng_ind.sh
```

Scheduled runs default to:

- recipient: `dorovlad@gmail.com`
- shared state: `.manual-runs/state/seen-vacancies.json`
- email body: new vacancies only
- lock file: `.manual-runs/jobbot-eng-ind.lock`

The scheduled multi-source runner first fetches public APIs (`Arbeitnow`, `RemoteOK`, `Remotive`) and then runs Codex source-specific searches. Public API results are merged with Codex results before validation and ranking.

Example crontab for 12:00 and 16:00 Germany time:

```cron
CRON_TZ=Europe/Berlin
0 12,16 * * * cd /home/dorovlad_codex/projects/projects/JobBots/jobbot-eng-ind && bash scripts/run_scheduled_jobbot_eng_ind.sh >> .manual-runs/scheduled.log 2>&1
```

The same schedule is stored in `config/jobbot-eng-ind.cron`. If `crontab` is available:

```bash
crontab config/jobbot-eng-ind.cron
```

If user systemd is preferred, install the files from `config/systemd/` into `~/.config/systemd/user/`, then run:

```bash
systemctl --user daemon-reload
systemctl --user enable --now jobbot-eng-ind.timer
```

For direct report generation from an existing JSON file:

```powershell
python .\src\jobbot_eng_ind.py --vacancies-json .\data\example-vacancies.json --drive
```

The bot writes to Google Drive:

- `Projects/JobBots/jobbot-eng-ind/state/seen-vacancies.json`
- `Projects/JobBots/jobbot-eng-ind/runs/yyyy-mm-dd/email-report.txt`
- `Projects/JobBots/jobbot-eng-ind/runs/yyyy-mm-dd/vacancies.json`
- `Projects/JobBots/jobbot-eng-ind/resumes/`

Credentials are expected at:

- `credentials/google-oauth-client.json`
- `credentials/gmail-token.json`

The `credentials/` directory is ignored by git and is only required for the explicit `gmail-api` fallback mode.
