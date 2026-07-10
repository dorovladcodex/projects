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

The API is exposed on port 8000 and PostgreSQL on port 5432. Current code does not
yet persist data; the database service is prepared for the next phase.

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

## Safety

`BOT_MODE=LIVE` (or any unsupported mode) fails configuration validation and
prevents application startup. `PaperExecutionEngine` only accepts PAPER mode.
No module in v1 sends orders to Bybit.

See [PLAN.md](PLAN.md) for phase boundaries.

## Documentation

- [Architecture](docs/ARCHITECTURE.md)
- [Operations runbook](docs/RUNBOOK.md)
- [Security policy](docs/SECURITY.md)
- [Planned GCP deployment](docs/GCP_DEPLOYMENT.md)
