# Implementation plan

## Phase 1 — foundation (implemented)

- Project skeleton, typed configuration, and explicit live-mode block
- Core Pydantic domain models
- Mock news, LLM classification, and market snapshot providers
- One deterministic `NewsMomentumStrategy`
- Hard-coded `RiskManager` as final authority
- In-memory `PaperExecutionEngine`
- Minimal FastAPI `/health` and `/status`
- Unit tests for strategy and risk controls

## Phase 2 — data and observability

- PostgreSQL persistence and migrations
- Bybit V5 read-only market-data adapter
- News provider adapters, filters, deduplication, and classifier cache
- Dashboard views and Telegram notifications
- Structured logs, metrics, and bot-event audit trail

## Phase 3 — simulation quality

- Fees, funding, slippage, partial fills, and position lifecycle
- Daily/weekly PnL accounting and pause state
- Replay/backtest harness and analytics
- WebSocket stability monitoring and stale-data protection

## Phase 4 — Bybit demo only

- Authenticated Bybit Demo adapter with strict environment checks
- Idempotent order lifecycle and reconciliation
- Operational runbooks for Docker Compose on GCP VM
- No live-trading activation path in v1
