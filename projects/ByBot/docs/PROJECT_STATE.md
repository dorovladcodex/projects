# ByBot project state

## V2 alpha-safety status

- Liquidation side follows Bybit's documented position-side semantics.
- Scoring is side-aware and regime-aware; zero evidence no longer earns a
  positive magnitude score.
- OI/funding, liquidation, meme-trend and rolling-history inputs have explicit
  setup gates.
- Edge proxies are bounded by the strategy TP and retained separately from
  empirical calibration.
- Candidate admission ranks current-cycle opportunities and limits dispatches.
- Demo sizing uses risk capital / stop distance, then notional and near-touch
  liquidity caps.
- The durable portfolio ledger credits each terminal execution once and tracks
  realized/unrealized PnL, equity, peak equity and UTC daily/weekly limits.
- A shadow empirical calibrator remains non-authoritative until its minimum
  sample count is reached. `scripts/v2_alpha_validation.py` creates a read-only
  bootstrap evidence report.

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

## V2 direct-Demo milestone

- Dynamic validation for 17 USDT linear symbols with durable exclusions.
- Public ticker/trade/order-book/liquidation WebSocket ingestion and bounded REST
  funding/open-interest/ticker fallback across seven rolling windows.
- News Momentum V2, Volume Breakout, OI/Funding Squeeze, Liquidation Momentum and
  Meme Trend strategies with independent feature flags.
- Shared scoring, Decimal quantity/leverage normalization, restart-safe portfolio
  reservations and concurrent per-symbol execution.
- Strategy-specific volatility stops, targets, trailing/break-even metadata and
  exact-owned position monitoring reuse V1 protection/reconciliation.
- Run-scoped JSON/CSV analytics; migration head `20260715_0014`.

The V2 soak is prepared but requires explicit operator authorization. Feature
history is primarily in memory; selected snapshots and all decisions are durable.
