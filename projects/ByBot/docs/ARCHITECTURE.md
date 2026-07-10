# ByBot architecture

## Safety boundary

ByBot v1 supports only `DATA_ONLY`, `PAPER`, and `BYBIT_DEMO` modes. Live order
submission is explicitly disabled. `BOT_MODE=LIVE` fails configuration
validation, and v1 contains no live-execution adapter.

`SHADOW_LIVE` is a planned observation phase, not a trading mode: it will read
live public market data and produce hypothetical decisions without submitting
orders. Adding credentials or deploying to GCP must never change this boundary.

## Design principles

- The application is a modular monolith with explicit provider interfaces.
- Market data, strategy, risk, sizing, and execution decisions are deterministic.
- The LLM sees only compact news metadata and classifies sentiment; it cannot
  place orders, size positions, override risk, or consume ticks and order books.
- Failure, stale data, uncertainty, or an unavailable LLM resolves to
  `NO_TRADE`.
- The hard-coded `RiskManager` is the final authority before any simulated or
  demo execution.
- Every decision is intended to become auditable through persisted events.

## Modules

| Module | Responsibility |
| --- | --- |
| `app/config.py` | Validated environment configuration and mode allowlist |
| `app/news/` | News ingestion boundary, deduplication, compact classification |
| `app/bybit/` | Market-data providers; mocks in Phase 1 |
| `app/strategy/` | Deterministic `NewsMomentumStrategy` and market filters |
| `app/risk/` | Position, loss, leverage, spread, confidence, and stability limits |
| `app/portfolio/` | Paper execution and later position lifecycle management |
| `app/db/` | Persistence contracts and, later, PostgreSQL repositories |
| `app/dashboard/` | Read-only operational visibility |
| `app/alerts/` | Telegram notifications, never trade authorization |
| `app/analytics/` | PnL, skipped-trade, and decision-quality analysis |

## Decision flow

1. Ingest and deduplicate a fresh news item.
2. Send only title, short summary, source, timestamp, and asset hint to the
   classifier; cache the result.
3. Obtain a fresh market snapshot for `BTCUSDT` or `ETHUSDT`.
4. Require sentiment confidence, trend confirmation, acceptable volatility,
   liquidity, spread, and positive expected edge after costs.
5. Produce `TRADE` or `NO_TRADE` with reasons.
6. Apply the independent `RiskManager` checks.
7. Route an approved decision only to the engine permitted by the active mode.
8. Record the input, decision, execution result, and errors.

No component may skip steps 4-6. In v1, the only implemented execution engine
is in-memory paper execution.

## Planned phases

### Phase A — DATA_ONLY

- Read news and public Bybit market data.
- Phase 2 uses public Bybit V5 REST polling for market tickers first. WebSocket
  can be added later behind the same market-data provider interface.
- Classify, calculate filters, and record hypothetical signals.
- Never create orders or positions.
- Validate freshness, reconnect behavior, deduplication, and observability.

### Phase B — PAPER

- Use live or replayed input data with local simulated fills.
- Model fees, slippage, funding, stops, and position lifecycle.
- Enforce one position, 1x-2x leverage, loss limits, and pause rules.
- Compare expected and realized simulated outcomes.

### Phase C — BYBIT_DEMO

- Use Bybit's isolated demo environment only.
- Add an authenticated demo adapter, idempotency, reconciliation, and restart
  recovery after paper behavior is accepted.
- Require demo-only credentials and reject production endpoints.
- This phase is not authorization for live trading.

### Phase D — GCP deployment

- Deploy the same containerized application to a hardened GCP VM.
- Move secrets outside the repository, restrict network access, persist data,
  add backups, health checks, monitoring, and restart procedures.
- Initially run `DATA_ONLY`, then `PAPER`; enable `BYBIT_DEMO` only after an
  operational review.
- Deployment changes location, not trading permissions.

### Phase E — shadow live

- Observe real production public feeds and generate hypothetical decisions.
- Compare shadow fills and risk decisions with actual market behavior.
- Submit no orders and use no production trading credentials.
- Maintain explicit `live_trading=false` status and alert on any safety-boundary
  violation.

Live trading is outside v1 and requires a separately designed, reviewed, and
approved future version. There is intentionally no automatic promotion path
from shadow mode to live execution.
