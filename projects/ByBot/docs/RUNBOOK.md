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
- `/market`: returns `status=OK` and latest BTCUSDT/ETHUSDT snapshots, or
  `status=DATA_UNAVAILABLE` if public market data cannot be refreshed.
- `/market/BTCUSDT` and `/market/ETHUSDT`: return one latest snapshot.
- `/account`: returns `connected=false` without crashing if private keys are
  absent or invalid; with valid demo read-only keys it returns wallet, positions,
  and orders.
- `/status`: uses cached account data and refreshes it when missing or stale.
  If refresh fails after a previous success, the account remains connected with
  `stale=true` and a clear `last_error`.
- `/paper/pnl`: returns internal paper realized/unrealized/total PnL.

Stop with `Ctrl+C`.

## Mode procedures

### DATA_ONLY

Set `BOT_MODE=DATA_ONLY`. Confirm execution is reported as disabled. Use this
mode first for new data providers, schema changes, and production public feeds.

### Private account read-only

Use only demo read-only keys. Keep `BYBIT_ENABLE_TRADING=false`. Verify:

```powershell
Invoke-RestMethod http://127.0.0.1:8000/account | ConvertTo-Json -Depth 10
```

Expected safety fields:

- `trading_enabled=false`
- `/status` has `order_placement_blocked=true`
- invalid private API credentials produce `connected=false` and `last_error`

### PAPER

Set `BOT_MODE=PAPER`. Paper positions must remain local simulations. Confirm
that no authenticated production Bybit client is configured or instantiated.
Use `POST /paper/test-signal` only for local/manual paper testing. Confirm that
`/status` still reports `order_placement_blocked=true`.
Paper position size must respect account equity, available balance when known,
requested leverage, max notional, and max percent of equity. Tight stop losses
must not create oversized notional exposure.
Paper trades must also pass net-edge validation: take profit must exceed
round-trip fees, slippage, and the configured minimum net edge.

Useful checks:

```powershell
Invoke-RestMethod http://127.0.0.1:8000/paper/positions | ConvertTo-Json -Depth 10
Invoke-RestMethod http://127.0.0.1:8000/paper/trades | ConvertTo-Json -Depth 10
Invoke-RestMethod http://127.0.0.1:8000/paper/pnl | ConvertTo-Json -Depth 10
Invoke-RestMethod -Method Post http://127.0.0.1:8000/paper/close-position | ConvertTo-Json -Depth 10
```

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

## Twelve-hour Bybit Demo soak

With Demo-only credentials in untracked `.env`, run:

```powershell
powershell -ExecutionPolicy Bypass -File .\scripts\bybit_demo_soak.ps1 -Hours 12 -SampleSeconds 30 -AllowDemoOrders
```

The explicit flag authorizes Demo orders. The runner uses one local worker,
performs a controlled restart, reconciles exchange/PostgreSQL state, and cleans
up only bot-owned state. Never use mainnet/testnet or `TEST_MODE=true`. Inspect
`artifacts\demo-soak` before retrying any failed run.

## Controlled Bybit Demo canary

Run only after reviewing the Demo account and with Demo-only credentials:

```powershell
powershell -ExecutionPolicy Bypass `
  -File .\scripts\bybit_demo_canary.ps1 `
  -Symbol BTCUSDT `
  -MaxNotionalUSDT 75 `
  -AllowDemoOrders
```

The runner requires an exact `api-demo.bybit.com` configuration, a flat
account, no active order for the selected symbol, and 1x leverage. It fetches
the current exchange minimums and price, rounds quantity upward to `qtyStep`,
and submits only that minimum through the production Demo execution service.
The calculated notional plus the default 5% price buffer must fit inside the
explicit `MaxNotionalUSDT`; the budget is never silently increased. It verifies
TP/SL and restart recovery, then performs a reduce-only close. Never use this
runner with mainnet or testnet. After previewing the plan, type the exact
confirmation phrase displayed by the runner to authorize that quantity.
The runner writes `artifacts\demo-canary\<run_id>\report.json` with entry and
close order history, executions, remote position observations, durable state
transitions, fees, realized PnL, and separate `functional_result` and
`safety_cleanup_result`. A functional timeout remains a failure even when the
idempotent reduce-only safety cleanup successfully leaves the Demo account flat.

## Read-only Demo kill-switch diagnostics

These commands do not start FastAPI and the diagnostics client implements only
signed GET requests:

```powershell
.\.venv\Scripts\python.exe .\scripts\demo_kill_switch_diagnostics.py

# Guard validation only; no database change:
.\.venv\Scripts\python.exe .\scripts\demo_kill_switch_reset.py `
  --execution-id 0033f5c9-7b97-4832-92f1-6853e1e4d95f

# Apply only after reviewing a passing dry run:
.\.venv\Scripts\python.exe .\scripts\demo_kill_switch_reset.py `
  --execution-id 0033f5c9-7b97-4832-92f1-6853e1e4d95f `
  --confirm-reset
```

The reset preserves activation reasons and appends `KILL_SWITCH_RESET`. It
refuses active positions/orders, unresolved executions, unknown exchange state,
and daily/weekly/drawdown reasons.

## V2 read-only preflight and Demo soak

Windows host processes use PostgreSQL at `127.0.0.1`:

```powershell
.\.venv\Scripts\python.exe -m alembic upgrade head
.\.venv\Scripts\python.exe .\scripts\demo_v2_preflight.py
```

The preflight performs only signed GET and public market requests. It does not
load FastAPI or any exchange mutation adapter.

Operator-authorized 30-minute burn-in:

```powershell
powershell -ExecutionPolicy Bypass -File .\scripts\demo_v2_soak.ps1 -Hours 0.5 -AllowDemoOrders
```

Operator-authorized 24-hour soak:

```powershell
powershell -ExecutionPolicy Bypass -File .\scripts\demo_v2_soak.ps1 -Hours 24 -AllowDemoOrders
```

The runner never resets a kill switch. Optional forced cleanup is explicit and
may touch only exact bot-owned Demo executions.
