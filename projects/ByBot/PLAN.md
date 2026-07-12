# Implementation plan

## Phase 1 - foundation (implemented)

- Project skeleton, typed configuration, and explicit live-mode block
- Core Pydantic domain models
- Mock news, LLM classification, and market snapshot providers
- One deterministic `NewsMomentumStrategy`
- Hard-coded `RiskManager` as final authority
- In-memory `PaperExecutionEngine`
- Minimal FastAPI `/health` and `/status`
- Unit tests for strategy and risk controls

## Phase 2 - data-only market data (implemented)

- Bybit V5 public REST ticker adapter for BTCUSDT and ETHUSDT
- In-memory latest market snapshot cache
- `/market` and `/market/{symbol}` API endpoints
- `/status` market-data status, latest BTC/ETH snapshots, and data-unavailable block
- Safe `DATA_UNAVAILABLE` behavior when Bybit is unreachable
- No authenticated Bybit client and no order placement

Deferred from Phase 2:

- WebSocket market-data provider
- PostgreSQL persistence and migrations
- News provider adapters, filters, deduplication, and classifier cache
- Dashboard views and Telegram notifications
- Structured logs, metrics, and bot-event audit trail

## Phase 3A - safe private account connection (implemented)

- Bybit V5 read-only private client configuration
- Demo/mainnet environment flag, with trading still blocked
- `/account` API endpoint for wallet/equity, open positions, open orders, and
  recent closed orders when available
- `/status` account connection status and order-placement block state
- Safe disconnected/error state when private API validation fails
- `BYBIT_ENABLE_TRADING=true` rejected during configuration validation
- No live trading, no demo order placement, and no order-placement methods

## Phase 3B - paper trading loop (implemented)

- In-memory `PaperTradingService`
- Open/close internal LONG and SHORT paper positions
- Update unrealized PnL from latest market price
- Stop-loss, take-profit, and timeout closes
- Realized/unrealized/total PnL accounting
- Position sizing caps by equity, available balance, leverage, max notional,
  and max percent of equity
- Estimated paper fees and slippage in PnL
- `POST /paper/test-signal` manual test endpoint with RiskManager approval
- `GET /paper/positions`, `GET /paper/trades`, and `GET /paper/pnl`
- `/status` paper trading status, open paper position, last paper trade, and
  last risk decision
- No Bybit order placement, no demo order placement, no live trading

Deferred from Phase 3B:

- Fees, funding, slippage, and partial fills
- Persistent paper trade storage
- Replay/backtest harness and analytics
- WebSocket stability monitoring and stale-data protection

## Phase 4A - real news ingestion and filtering (implemented)

- Dependency-free public RSS source adapter and source interfaces/placeholders
  for CryptoPanic, GDELT, and Bybit announcements
- Normalized in-memory news items with title/source deduplication
- Freshness, relevance, keyword, and BTC/ETH/MARKET classification filters
- Compact deterministic mock news classifier and `/news` API endpoints
- Safe background polling with visible error state in `/status`
- No Bybit order placement, demo order placement, or live trading

Deferred from Phase 4A:

- Real LLM provider and persistent classification cache
- Source API credentials/adapters and persistent news storage
- Connecting accepted news signals to automatic paper entry

## Phase 4C - news-to-signal dry run (implemented)

- Deterministic BTC/ETH/MARKET news-to-symbol candidate mapping
- Price direction, trend, volume, spread, volatility, freshness, edge, and TTL checks
- RiskManager preview with capped size/notional and complete rejection reasons
- Candidate deduplication by processed news ID
- `/signals/candidates`, `/signals/latest`, `/signals/dry-run`, and
  `POST /signals/test-from-news`
- No automatic paper entry and no Bybit order execution

## Phase 4D - pending signal confirmation (implemented)

- Candidate lifecycle: `PENDING_CONFIRMATION`, `READY`, `BLOCKED`, `EXPIRED`
- Separate proposed and final actions
- Five-second configurable market re-evaluation until confirmation or TTL
- Configurable adverse-move tolerance before early blocking
- Per-candidate evaluation history without duplicate candidate creation
- RiskManager preview only for `READY` candidates
- Pending/history/detail/manual-recheck API endpoints and lifecycle status counts
- Dry-run only: no automatic paper trade and no exchange order placement
- Local-test-only snapshot injection through the production confirmation path,
  isolated from the global Bybit market cache

## Phase 4E - production-safe optional LLM classification (implemented)

- Default mock/optional LLM classifier modes behind one typed interface
- Strict structured response validation with fail-closed neutral results
- Normalized filtered-news-only prompt boundary and injection-resistant instructions
- Deterministic TTL cache, retries/backoff, concurrency/rate limits, budgets,
  token accounting, and circuit breaker
- Explicit local-only non-tradeable mock fallback
- Classifier status, metrics, and isolated local test endpoints
- Failed/fallback classifications cannot enter the signal pipeline
- No automatic paper execution and no Bybit order placement

## Phase 4B - Bybit demo only

- Authenticated Bybit Demo adapter with strict environment checks
- Idempotent order lifecycle and reconciliation
- Operational runbooks for Docker Compose on GCP VM
- No live-trading activation path in v1
