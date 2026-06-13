# JobBot Eng Ind Runbook

## Manual Run

From the project root:

```powershell
python .\src\jobbot_eng_ind.py --vacancies-json .\data\example-vacancies.json
```

## After Running

1. Review the generated `email-report.txt`.
2. Send the report through the connected Gmail connector if requested.
3. Generate a tailored CV only when a specific vacancy is selected.

## Notes

- This bot is intentionally manual.
- It should not be converted into the broad NRW scheduled automation.
- It uses the shared state file so repeated vacancies can be marked as watchlist items.
