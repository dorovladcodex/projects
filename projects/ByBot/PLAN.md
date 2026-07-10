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

## Phase 3B - simulation quality

- Fees, funding, slippage, partial fills, and position lifecycle
- Daily/weekly PnL accounting and pause state
- Replay/backtest harness and analytics
- WebSocket stability monitoring and stale-data protection

## Phase 4 - Bybit demo only

- Authenticated Bybit Demo adapter with strict environment checks
- Idempotent order lifecycle and reconciliation
- Operational runbooks for Docker Compose on GCP VM
- No live-trading activation path in v1
