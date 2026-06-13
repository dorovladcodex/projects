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
