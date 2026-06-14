# JobBot Eng Ind Usage

Run from `projects/JobBots/jobbot-eng-ind`:

```bash
./scripts/run_jobbot_eng_ind.sh
```

To upload to Google Drive and send Gmail after the run:

```bash
JOBBOT_ENABLE_DRIVE=1 JOBBOT_ENABLE_GMAIL=1 ./scripts/run_jobbot_eng_ind.sh
```

For a local smoke test without Codex web search:

```bash
./scripts/smoke_test.sh
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
- `credentials/google-token.json`

The `credentials/` directory is ignored by git.
