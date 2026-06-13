# JobBot Eng Ind Usage

Run from `projects/JobBots/jobbot-eng-ind`:

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
