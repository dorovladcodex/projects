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
- Windows PowerShell 5.1 soak runner for real RSS/public Bybit data, Codex CLI,
  continuous accounting/database checks, controlled restart, and artifacts.
- Explicit fail-closed Bybit Demo execution: durable reservation, deterministic
  orderLinkId, private order/execution/position/wallet stream, REST reconciliation,
  actual-fill TP/SL protection, emergency reduce-only close, and ownership cleanup.
- Startup-safe NewsItem restore with dedicated repair columns, durable quarantine
  audit, idempotent repair CLI, and visible restore metrics.

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
- candidate -> one `demo_executions`; `demo_execution_events` deduplicates stream
  events and records transitions; `demo_kill_switch` persists fail-closed incidents.
- `persistence_quarantine` preserves malformed historical row audits; quarantined
  `news_items` and their candidates are excluded from normal restore.

Current Alembic head: `20260714_0013`. Demo canary submission is a durable
background job (`demo_canary_jobs`), so a client timeout or Windows sleep does
not imply that no exchange action occurred; recover by `run_id`/execution ID.

Paper equity is authoritative and consistent across API/restart:

`equity = starting_equity + cumulative net realized PnL + current unrealized PnL`

Paper sizing never uses Bybit demo equity.

## Safety guarantees

- `BYBIT_ENABLE_TRADING=true` and live trading are rejected by configuration.
- Demo execution requires all explicit Demo gates and exact api-demo/stream-demo
  domains; mainnet and testnet adapters are unreachable.
- Demo create acknowledgement never means FILLED; remote exchange state is
  authoritative and entry protection is verified before POSITION_OPEN.
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
- Demo runner intentionally has no synthetic trade: zero trades is valid. It is
  single-process and requires PostgreSQL, valid Demo credentials, network, RSS,
  public market data, and the configured classifier.
- Live order placement remains permanently out of scope.
