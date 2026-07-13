# ByBot

Lightweight, deterministic Bybit trading-bot foundation. Current builds support only
`DATA_ONLY`, `PAPER`, and `BYBIT_DEMO`. **Live trading is intentionally blocked.**

The LLM boundary is limited to compact news classification. Strategy, market
filters, risk decisions, and paper execution are deterministic Python code.

## Scope

- Symbols: `BTCUSDT`, `ETHUSDT`
- Market: Bybit linear perpetuals
- Maximum one open position
- Leverage: 1x-2x
- Mandatory stop loss
- No martingale or averaging down

## Local setup (Windows 10/11, PowerShell)

Prerequisites: install Python 3.11 or newer from `python.org` with the Python
Launcher (`py`) enabled. Run the following commands from PowerShell:

```powershell
cd D:\VibeProjects\projects\projects\ByBot
py -3.11 --version
py -3.11 -m venv .venv
.\.venv\Scripts\python.exe -m pip install --upgrade pip
.\.venv\Scripts\python.exe -m pip install -r requirements-dev.txt
if (-not (Test-Path .env)) { Copy-Item .env.example .env }
.\.venv\Scripts\python.exe -m pytest -q
.\.venv\Scripts\python.exe -m uvicorn app.main:app --host 127.0.0.1 --port 8000 --reload
```

Keep that PowerShell window open while the API is running. In a second
PowerShell window, verify both endpoints:

```powershell
Invoke-RestMethod http://127.0.0.1:8000/health | ConvertTo-Json
Invoke-RestMethod http://127.0.0.1:8000/status | ConvertTo-Json
```

Expected safety fields include `"status": "ok"` from `/health`. `/status`
returns `"mode"`, `"live_trading": false`, `"trading_enabled"`,
`"trading_paused"`, `"active_symbols"`, `"open_paper_position"`,
`"last_signal"`, `"market"`, and `"risk_status"`. Stop the server with
`Ctrl+C`.

If Python 3.11 is not installed but a newer supported version is available,
replace both `py -3.11` occurrences with `py -3`.

### Run tests later

```powershell
cd D:\VibeProjects\projects\projects\ByBot
.\.venv\Scripts\python.exe -m pytest -q
```

## Docker Compose

```powershell
Copy-Item .env.example .env
docker compose up --build
```

The API is exposed on port 8000 and PostgreSQL on port 5432. Pipeline state is
persisted in PostgreSQL. Apply migrations before starting the API outside Compose:

```powershell
$env:DATABASE_URL="postgresql+psycopg://bybot:bybot@localhost:5432/bybot"
.\.venv\Scripts\python.exe -m alembic upgrade head
```

For normal RSS-to-Codex dry-run processing, keep these safety settings in `.env`:

```powershell
NEWS_CLASSIFIER_MODE=codex_cli
CODEX_CLI_ENABLED=true
CODEX_CLI_MIN_NEWS_IMPORTANCE=0.70
PAPER_STARTING_EQUITY_USDT=10000
AUTO_PAPER_EXECUTION=false
BYBIT_ENABLE_TRADING=false
```

Paper sizing and PnL use `PAPER_STARTING_EQUITY_USDT`; Bybit demo equity is
read-only context and never funds the paper account.

### PostgreSQL end-to-end smoke test

With Docker Desktop running, the local virtual environment prepared, and Codex
CLI authenticated, run this single command from the project root:

```powershell
powershell -ExecutionPolicy Bypass -File .\scripts\postgres_e2e_smoke.ps1
```

The script starts only PostgreSQL in Docker and runs FastAPI locally. It leaves
PostgreSQL running, stops its local uvicorn process, and never enables paper or
exchange execution.

## Automatic paper execution

Automatic paper execution is disabled by default. To enable it explicitly in
PAPER mode, set `AUTO_PAPER_EXECUTION=true`. Exchange execution remains blocked
by `BYBIT_ENABLE_TRADING=false`.

Paper fills use configurable deterministic costs:

```powershell
PAPER_MAKER_FEE_BPS=2
PAPER_TAKER_FEE_BPS=6
PAPER_SLIPPAGE_BPS=2
```

Paper account reporting uses one formula: `equity = starting_equity +
cumulative net realized PnL + current unrealized PnL`. `GET /paper/pnl`
returns `starting_equity`, `equity`, realized and unrealized PnL, total PnL,
fees paid, and open/closed position counts. Closed account totals are persisted
in PostgreSQL and reconciled from durable trades during startup recovery.

Only a fresh, non-expired `READY` candidate with an approved risk preview can
open a paper position. PostgreSQL enforces one paper execution per candidate,
including across application restarts.

With the local API running in `APP_ENV=local`, `TEST_MODE=true`, PAPER mode, and
`AUTO_PAPER_EXECUTION=false`, run the deterministic execution lifecycle smoke test:

```powershell
powershell -ExecutionPolicy Bypass -File .\scripts\paper_execution_smoke.ps1
```

It verifies `READY → PAPER_OPENED → idempotent duplicate → TAKE_PROFIT →
PAPER_CLOSED` and finishes with `OVERALL: PASS`.

To verify the complete automatic path without calling the manual candidate
execution endpoint, keep Docker Desktop running and run:

```powershell
powershell -ExecutionPolicy Bypass -File .\scripts\auto_paper_execution_smoke.ps1
```

The script creates an isolated temporary PostgreSQL database, starts FastAPI
locally with `AUTO_PAPER_EXECUTION=true` and `BYBIT_ENABLE_TRADING=false`, tests
automatic open, duplicate protection, take-profit close, and restart recovery,
then removes the temporary database. The PostgreSQL container remains running.

To verify sanitized database diagnostics locally:

```powershell
.\.venv\Scripts\python.exe -m pytest tests\test_auto_paper_execution.py -k persistence_failure -s
```

PostgreSQL-only transaction tests run when a dedicated test database is supplied:

```powershell
$env:BYBOT_TEST_POSTGRES_URL="postgresql+psycopg://bybot:bybot@localhost:5432/bybot_test"
.\.venv\Scripts\python.exe -m pytest tests\test_postgres_paper_execution.py -q
```

Use only a disposable test database for that command. Error responses expose a
sanitized code such as `DB_INTEGRITY_ERROR`; credentials and `DATABASE_URL` are
never returned.

## Market data

By default, local development uses mock market data so the app and tests run
without internet access:

```powershell
MARKET_DATA_PROVIDER=MOCK
```

To test public Bybit market data in `DATA_ONLY` mode, edit `.env`:

```powershell
BOT_MODE=DATA_ONLY
MARKET_DATA_PROVIDER=BYBIT_REST
BYBIT_PUBLIC_BASE_URL=https://api.bybit.com
```

Then start the API:

```powershell
.\.venv\Scripts\python.exe -m uvicorn app.main:app --host 127.0.0.1 --port 8000 --reload
```

Verify market data:

```powershell
Invoke-RestMethod http://127.0.0.1:8000/market | ConvertTo-Json -Depth 10
Invoke-RestMethod http://127.0.0.1:8000/market/BTCUSDT | ConvertTo-Json -Depth 10
Invoke-RestMethod http://127.0.0.1:8000/market/ETHUSDT | ConvertTo-Json -Depth 10
Invoke-RestMethod http://127.0.0.1:8000/status | ConvertTo-Json -Depth 10
```

If Bybit is unreachable, `/market` and `/status.market` report
`"DATA_UNAVAILABLE"` and the bot stays safe. No order-placement code exists.
The Phase 2 adapter uses public Bybit V5 REST polling first. The interface is
kept small so a WebSocket provider can replace it later without changing
strategy, risk, or API code.

## Private account connection, read-only

Phase 3A can connect to Bybit private account data for reconciliation only.
It does not place orders. Use demo API keys with read-only permissions.

Edit `.env`:

```powershell
notepad .env
```

Set these values:

```env
BYBIT_ENV=demo
BYBIT_ENABLE_TRADING=false
BYBIT_API_KEY=your_demo_read_only_key
BYBIT_API_SECRET=your_demo_read_only_secret
BYBIT_PRIVATE_DEMO_BASE_URL=https://api-demo.bybit.com
```

Start the API:

```powershell
.\.venv\Scripts\python.exe -m uvicorn app.main:app --host 127.0.0.1 --port 8000 --reload
```

Verify account status:

```powershell
Invoke-RestMethod http://127.0.0.1:8000/account | ConvertTo-Json -Depth 10
Invoke-RestMethod http://127.0.0.1:8000/status | ConvertTo-Json -Depth 10
```

`/account` force-refreshes private account data. On startup and on `/status`,
the app refreshes account data when the cached state is missing or stale. If a
refresh fails after a previous success, the last connected account state is kept
and marked `stale=true` with `last_error`, instead of crashing or falsely
disconnecting paper trading context.

Expected safety fields:

- `trading_enabled` is `false`
- `order_placement_blocked` is `true`
- `account.trading_enabled` is `false`
- invalid keys show `connected=false` and `last_error`, without crashing

## Paper trading simulation

Phase 3B simulates internal paper trades using current market data. It never
submits Bybit orders. Keep:

```env
BYBIT_ENABLE_TRADING=false
BOT_MODE=PAPER
```

Start the API:

```powershell
cd D:\VibeProjects\projects\projects\ByBot
.\.venv\Scripts\python.exe -m uvicorn app.main:app --host 127.0.0.1 --port 8000 --reload
```

Inject a manual paper test signal:

```powershell
$body = @{
  symbol = "BTCUSDT"
  side = "BUY"
  confidence = 0.9
  expected_edge_bps = 20
  stop_loss_pct = 0.5
  take_profit_pct = 1.0
} | ConvertTo-Json

Invoke-RestMethod `
  -Method Post `
  -Uri http://127.0.0.1:8000/paper/test-signal `
  -ContentType "application/json" `
  -Body $body | ConvertTo-Json -Depth 10
```

Inspect paper state:

```powershell
Invoke-RestMethod http://127.0.0.1:8000/paper/positions | ConvertTo-Json -Depth 10
Invoke-RestMethod http://127.0.0.1:8000/paper/trades | ConvertTo-Json -Depth 10
Invoke-RestMethod http://127.0.0.1:8000/paper/pnl | ConvertTo-Json -Depth 10
Invoke-RestMethod http://127.0.0.1:8000/status | ConvertTo-Json -Depth 10
```

Manually close the open paper position at the latest market price:

```powershell
Invoke-RestMethod `
  -Method Post `
  -Uri http://127.0.0.1:8000/paper/close-position | ConvertTo-Json -Depth 10
```

Closed positions move from `/paper/positions` to `/paper/trades`. Realized PnL
is reflected in `/paper/pnl` and `/status`.

Safety rules enforced:

- max one open paper position
- stop-loss is mandatory
- no averaging down
- no martingale
- market data must be available
- RiskManager approval is required
- position size is capped by equity, available balance, leverage, max notional,
  and max percentage of equity
- paper PnL subtracts estimated fees and slippage
- automatic closes run for `stop_loss`, `take_profit`, and `timeout`

## News ingestion (Phase 4A)

The bot can read public RSS news and filters it locally before passing only a
compact title/summary/source/timestamp/asset hint to the deterministic mock
classifier. It never sends market ticks, order books, candles, full articles,
or API credentials to a classifier.

For local development, enable the RSS reader in `.env` (the defaults below are
also in `.env.example`):

```env
BOT_MODE=PAPER
BYBIT_ENABLE_TRADING=false
NEWS_ENABLE_RSS=true
NEWS_RSS_URLS=["https://cointelegraph.com/rss"]
NEWS_POLL_INTERVAL_SECONDS=60
NEWS_MAX_ITEM_AGE_MINUTES=60
NEWS_MIN_IMPORTANCE_TO_CLASSIFY=0.3
NEWS_ENABLE_MOCK_CLASSIFIER=true
```

Run the app, then inspect the in-memory feed state:

```powershell
Invoke-RestMethod http://127.0.0.1:8000/news | ConvertTo-Json -Depth 10
Invoke-RestMethod http://127.0.0.1:8000/news/filtered | ConvertTo-Json -Depth 10
Invoke-RestMethod http://127.0.0.1:8000/news/classifications | ConvertTo-Json -Depth 10
Invoke-RestMethod http://127.0.0.1:8000/news/filter-debug | ConvertTo-Json -Depth 10
```

To test filtering without any external request:

```powershell
$body = @{
  title = "Bitcoin ETF sees major BlackRock inflow"
  summary = "Fresh BTC fund flow may affect the crypto market."
  source = "local-test"
  url = "https://example.invalid/news"
  published_at = (Get-Date).ToUniversalTime().ToString("o")
} | ConvertTo-Json

Invoke-RestMethod -Method Post -Uri http://127.0.0.1:8000/news/test-item `
  -ContentType "application/json" -Body $body | ConvertTo-Json -Depth 10
```

RSS failures only set `news_status`/`news_last_error` in `/status`; they do not
stop the API. This phase remains paper-only and contains no Bybit order
placement.

`/news/filter-debug` shows each recent decision, including `matched_keywords`,
importance, and one of `accepted`, `old_news`, `duplicate`, `unrelated_asset`,
`missing_keywords`, or `low_importance`.

## News-to-signal dry run (Phase 4C)

Accepted classifications can be evaluated against deterministic market data
without opening a paper position. Keep these safety settings in `.env`:

```env
BOT_MODE=PAPER
BYBIT_ENABLE_TRADING=false
TEST_MODE=false
AUTO_PAPER_EXECUTION=false
SIGNAL_MIN_CLASSIFICATION_CONFIDENCE=0.80
SIGNAL_MIN_NEWS_IMPORTANCE=0.70
SIGNAL_TTL_SECONDS=300
SIGNAL_CONFIRMATION_WINDOW_SECONDS=60
SIGNAL_REEVALUATION_INTERVAL_SECONDS=5
SIGNAL_CONFLICT_THRESHOLD_PCT=0.30
SIGNAL_MIN_EXPECTED_EDGE_BPS=12
SIGNAL_DEFAULT_STOP_LOSS_PCT=0.5
SIGNAL_DEFAULT_TAKE_PROFIT_PCT=1.0
```

List dry-run state:

```powershell
Invoke-RestMethod http://127.0.0.1:8000/signals/candidates | ConvertTo-Json -Depth 12
Invoke-RestMethod http://127.0.0.1:8000/signals/latest | ConvertTo-Json -Depth 12
Invoke-RestMethod http://127.0.0.1:8000/signals/dry-run | ConvertTo-Json -Depth 12
```

Create a candidate from an already classified `news_id`:

```powershell
$body = @{ news_id = "replace-with-news-id"; reprocess = $false } | ConvertTo-Json
Invoke-RestMethod -Method Post -Uri http://127.0.0.1:8000/signals/test-from-news `
  -ContentType "application/json" -Body $body | ConvertTo-Json -Depth 12
```

The response contains a candidate and RiskManager preview with capped size,
notional, and rejection reasons. `execution_attempted=false` and
`paper_position_opened=false` are invariant in this phase.

### Pending confirmation lifecycle (Phase 4D)

Strong classifications now remain `PENDING_CONFIRMATION` while the market is
sideways or confirmation data is temporarily insufficient. Every five seconds
the app refreshes market data and updates the same candidate until it becomes
`READY`, `BLOCKED`, or `EXPIRED`. RiskManager preview runs only for `READY`.

```powershell
Invoke-RestMethod http://127.0.0.1:8000/signals/pending | ConvertTo-Json -Depth 12
Invoke-RestMethod http://127.0.0.1:8000/signals/history | ConvertTo-Json -Depth 12
Invoke-RestMethod http://127.0.0.1:8000/signals/latest | ConvertTo-Json -Depth 12
```

Inspect or manually recheck one candidate:

```powershell
$candidateId = "replace-with-candidate-id"
Invoke-RestMethod "http://127.0.0.1:8000/signals/$candidateId" | ConvertTo-Json -Depth 12
Invoke-RestMethod -Method Post "http://127.0.0.1:8000/signals/$candidateId/recheck" |
  ConvertTo-Json -Depth 12
```

Candidate output separates `proposed_action` from `final_action` and includes
the complete `evaluation_history`. Pending, blocked, and expired candidates
return `preview_performed=false` with `candidate is not tradeable yet` instead
of irrelevant order-validation errors.

### Deterministic local market confirmation test

The test-only snapshot endpoint is available only with all safety conditions:

```env
APP_ENV=local
TEST_MODE=true
BOT_MODE=PAPER
AUTO_PAPER_EXECUTION=false
BYBIT_ENABLE_TRADING=false
```

It rechecks one candidate through the normal confirmation and risk-preview
logic without writing into the global Bybit market cache:

```powershell
$candidateId = "replace-with-pending-candidate-id"
$body = @{
  price = 60300
  bid = 60299
  ask = 60301
  price_change_1m_pct = 0.30
  trend_direction = "BULLISH"
  trend_score = 0.60
  volatility_pct = 1.0
  volume_24h = 25000
  volume_change_pct = 25
  volume_spike = $true
  fresh = $true
} | ConvertTo-Json

Invoke-RestMethod -Method Post `
  -Uri "http://127.0.0.1:8000/signals/$candidateId/test-market-snapshot" `
  -ContentType "application/json" -Body $body | ConvertTo-Json -Depth 12
```

The endpoint returns `404` outside local test mode. It always reports
`execution_attempted=false`, `paper_position_opened=false`, and blocked exchange
order placement.

## Optional real LLM classifier (Phase 4E)

The default remains deterministic mock classification:

```env
NEWS_CLASSIFIER_MODE=mock
```

To configure an OpenAI-compatible structured-output provider, keep credentials
only in the ignored `.env` file and set:

```env
NEWS_CLASSIFIER_MODE=llm
LLM_API_KEY=replace-with-secret-provider-key
LLM_API_URL=https://api.openai.com/v1/chat/completions
LLM_MODEL=gpt-4.1-mini
LLM_CLASSIFIER_VERSION=news-v1
LLM_ALLOW_MOCK_FALLBACK=false
```

The classifier sends only normalized filtered-news fields. It never receives
Bybit credentials, wallet/account data, positions, orders, market snapshots,
logs, links to follow, or raw webpage HTML. Invalid output, provider errors,
timeouts, budget rejection, and open circuit breakers produce a non-tradeable
neutral `FAILED` classification.

Trade eligibility is calculated centrally. Only `SUCCESS` or `CACHE_HIT`
classifications with directional sentiment, sufficient confidence, a supported
BTC/ETH/MARKET asset, valid category, and no error may enter signal generation.
Neutral, failed, low-confidence, OTHER-asset, and mock-fallback results include
explicit `eligibility_reasons` and remain non-tradeable.

Inspect classifier health and budgets:

```powershell
Invoke-RestMethod http://127.0.0.1:8000/news/classifier/status | ConvertTo-Json -Depth 10
Invoke-RestMethod http://127.0.0.1:8000/news/classifier/metrics | ConvertTo-Json -Depth 10
```

`POST /news/classifier/test` is available only under `APP_ENV=local` and
`TEST_MODE=true`. It does not store news, create a signal, or execute a trade.

When `NEWS_CLASSIFIER_MODE=llm` has no usable credential (including the fake
`.env.example` placeholder), `/news/classifier/status` reports `UNAVAILABLE`,
`configured=false`, `provider_available=false`, and `PROVIDER_UNAVAILABLE`.
No provider request or request/token budget is consumed.

### Codex CLI classifier provider

Codex CLI can be selected without placing an API credential in ByBot:

```env
NEWS_CLASSIFIER_MODE=codex_cli
CODEX_CLI_ENABLED=true
CODEX_CLI_PATH=codex
CODEX_CLI_MODEL=gpt-5.4-mini
CODEX_CLI_FALLBACK_MODEL=gpt-5.6-luna
CODEX_CLI_REASONING_EFFORT=low
CODEX_CLI_FALLBACK_MIN_CONFIDENCE=0.75
AUTO_PAPER_EXECUTION=false
BYBIT_ENABLE_TRADING=false
```

Each classification runs `codex exec` with `shell=False`, an isolated temporary
directory, ephemeral/read-only mode, ignored user config, a UTF-8 schema file,
and the news prompt over stdin. The fallback model is called only for a valid
neutral primary result below the confidence threshold. Timeout, authentication,
invalid JSON/executable, and budget failures never trigger fallback. Existing
cache, eligibility, rate, budget, retry, and circuit-breaker controls remain in
force.
- real exchange execution remains blocked

Sizing defaults in `.env`:

```env
MAX_POSITION_NOTIONAL_USDT=5000
MAX_POSITION_NOTIONAL_PCT_OF_EQUITY=5
MIN_POSITION_NOTIONAL_USDT=10
DEFAULT_PAPER_FEES_BPS=6
DEFAULT_SLIPPAGE_BPS=2
MIN_NET_EDGE_BPS=5
```

For example, a very tight `stop_loss_pct` may produce a large risk-based size,
but the final paper size is reduced so notional stays within the configured
caps.

Paper trades are also rejected when the take-profit target is too small to cover
round-trip fees, slippage, and `MIN_NET_EDGE_BPS`. Manual `expected_edge_bps`
cannot exceed the configured take-profit target.

## Safety

`BOT_MODE=LIVE` (or any unsupported mode) fails configuration validation and
prevents application startup. `PaperExecutionEngine` only accepts PAPER mode.
`BYBIT_ENABLE_TRADING=true` fails configuration validation in Phase 3A.
No module in v1 sends orders to Bybit.

See [PLAN.md](PLAN.md) for phase boundaries.

## Documentation

- [Architecture](docs/ARCHITECTURE.md)
- [Operations runbook](docs/RUNBOOK.md)
- [Security policy](docs/SECURITY.md)
- [Planned GCP deployment](docs/GCP_DEPLOYMENT.md)
