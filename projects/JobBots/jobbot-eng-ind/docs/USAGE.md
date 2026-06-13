# JobBot Eng Ind Usage

Run from `projects/JobBots/jobbot-eng-ind`:

```powershell
python .\src\jobbot_eng_ind.py --vacancies-json .\data\example-vacancies.json
```

The bot writes:

- `D:\Job Search 2026\dd.MM.yyyy JobBot Eng Ind\email-report.txt`
- `D:\Job Search 2026\dd.MM.yyyy JobBot Eng Ind\vacancies.json`
- updates `D:\Job Search 2026\seen-vacancies.json`

Email sending is intentionally outside this script and is handled by the connected Gmail connector when requested in Codex.
