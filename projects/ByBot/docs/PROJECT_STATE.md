# ByBot project state

## Completed

- FastAPI health/status, mock and Bybit REST public market data with safe failure.
- Read-only Bybit private account reconciliation; exchange execution is blocked.
- RSS normalization/filtering/deduplication, mock/LLM/Codex CLI classification,
  caching, budgets, and fail-closed eligibility.
- Persisted news-to-signal lifecycle: pending, ready, blocked, expired, paper opened/closed.
- Risk preview, capped PAPER sizing, stops/targets, fees, slippage, and net-edge checks.
- Durable automatic PAPER execution, idempotency, SL/TP/timeout/manual close,
  paper account recovery, position limits, cooldowns, loss/drawdown kill switch.

## Architecture and data

The application is a synchronous modular monolith with short FastAPI background
loops. Deterministic Python owns market confirmation, strategy, risk, execution,
and accounting. LLMs receive filtered compact news only.

PostgreSQL relationships:

- `news_items` -> one `news_classifications`; cache is keyed by content/version.
- news -> `signal_candidates` -> `signal_evaluations` and `risk_decisions`.
- candidate -> one `paper_executions` -> one durable `paper_positions` row.
- closed position -> one `paper_trades` row.
- `paper_accounts` stores singleton realized totals; `paper_risk_state` stores
  kill switch, peak equity, entry time, and cooldowns.

Current Alembic head: `20260714_0006`.

Paper equity is authoritative and consistent across API/restart:

`equity = starting_equity + cumulative net realized PnL + current unrealized PnL`

Paper sizing never uses Bybit demo equity.

## Safety guarantees

- `BYBIT_ENABLE_TRADING=true` is rejected by configuration.
- No Bybit order-placement adapter is called; PAPER is the only execution path.
- One open paper position per symbol plus configurable total cap.
- Candidate/execution/open-slot/close idempotency prevents duplicate accounting.
- Stop loss is mandatory; no martingale or averaging down.
- Cooldowns and latched daily/weekly loss or drawdown kill switch block entries
  while existing positions remain closable.
- Test endpoints are local TEST_MODE only; secrets are not logged or committed.

## Validation

```powershell
.\.venv\Scripts\python.exe -m pytest -q
$env:BYBOT_TEST_POSTGRES_URL="postgresql+psycopg://...@127.0.0.1:5432/..."
.\.venv\Scripts\python.exe -m pytest tests\test_postgres_paper_execution.py -q
```

Run all four scripts in `scripts/` for Windows/Docker E2E validation.

## Known limitations and next milestones

- In-memory market history and polling loops are single-process; horizontal
  workers require distributed coordination.
- Financial storage remains SQL float in legacy tables; API smoke assertions use Decimal.
- RSS quality and real-provider classification still need production tuning.
- Next: longer PAPER soak/shadow-live observation, dashboard/alerts, operational
  metrics, backup/restore drills, then BYBIT_DEMO order design behind a new explicit gate.
- Demo/live order placement remains out of scope.
