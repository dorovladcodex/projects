# ByBot runbook

## Operating constraint

ByBot v1 must run only in `DATA_ONLY`, `PAPER`, or `BYBIT_DEMO`. Live trading is
disabled in configuration and absent from the execution architecture. Stop the
application if `/status` ever reports `live_trading` other than `false`.

## Local startup on Windows

From PowerShell in the repository root:

```powershell
if (-not (Test-Path .venv)) { py -3.11 -m venv .venv }
.\.venv\Scripts\python.exe -m pip install -r requirements-dev.txt
if (-not (Test-Path .env)) { Copy-Item .env.example .env }
.\.venv\Scripts\python.exe -m pytest -q
.\.venv\Scripts\python.exe -m uvicorn app.main:app --host 127.0.0.1 --port 8000
```

Verify from a second PowerShell window:

```powershell
Invoke-RestMethod http://127.0.0.1:8000/health | ConvertTo-Json
Invoke-RestMethod http://127.0.0.1:8000/status | ConvertTo-Json
```

Expected results:

- `/health`: `status` is `ok`.
- `/status`: `live_trading` is `false`.
- `/status`: `mode` matches the intended safe mode.

Stop with `Ctrl+C`.

## Mode procedures

### DATA_ONLY

Set `BOT_MODE=DATA_ONLY`. Confirm execution is reported as disabled. Use this
mode first for new data providers, schema changes, and production public feeds.

### PAPER

Set `BOT_MODE=PAPER`. Paper positions must remain local simulations. Confirm
that no authenticated production Bybit client is configured or instantiated.

### BYBIT_DEMO

This is a planned later-phase procedure. Use only Bybit demo endpoints and
demo-only API credentials. Before enabling it, verify endpoint allowlisting,
credential scope, order reconciliation, stop behavior, and restart recovery.
Never reuse a production API key.

### GCP

Start in `DATA_ONLY`, validate logs and persistence, then move to `PAPER` after
an operational review. GCP deployment does not permit live trading. See
`docs/GCP_DEPLOYMENT.md`.

### Shadow live

Shadow live observes public production feeds but executes nothing. Verify that
all generated orders remain hypothetical and that production trading
credentials are absent. It must still report `live_trading=false`.

## Pre-start checklist

- `.env` is not tracked by Git.
- `BOT_MODE` is one of the three v1 allowlisted values.
- `/status` reports the expected mode and `live_trading=false`.
- Tests pass.
- System clock is synchronized.
- PostgreSQL, data sources, and disk have sufficient capacity.
- No production trading or withdrawal-capable Bybit key is present.
- Daily/weekly loss state and consecutive-loss pause state are restored before
  any future demo execution.

## Incident response

### Health endpoint fails

1. Stop the process or container.
2. Inspect startup logs and validate `.env` values.
3. Check port conflicts and PostgreSQL availability.
4. Run the tests locally.
5. Restart only after the root cause is understood.

### Stale data, WebSocket instability, or provider errors

1. Force `NO_TRADE` and disable execution.
2. Record the outage interval and last valid timestamp.
3. Reconnect with bounded backoff.
4. Require a fresh snapshot before resuming decisions.

### Unexpected position or order

1. Stop ByBot immediately.
2. Preserve logs and database records.
3. Inspect the relevant external environment manually.
4. Revoke the API key if the environment is uncertain.
5. Do not restart until reconciliation and root-cause review are complete.

### Safety invariant violation

If live trading appears enabled, treat it as a critical incident: stop the
service, revoke credentials, preserve evidence, and block deployment. There is
no acceptable degraded state with live execution in v1.

## Recovery and rollback

- Roll back to a previously tested image and configuration together.
- Do not delete audit records during recovery.
- Reconcile open paper/demo state before accepting new signals.
- Resume in `DATA_ONLY`; promote back to `PAPER` or `BYBIT_DEMO` only after
  health, freshness, and risk checks pass.
