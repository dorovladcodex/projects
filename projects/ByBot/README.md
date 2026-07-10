# ByBot

Lightweight, deterministic Bybit trading-bot foundation. Phase 1 supports only
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
cd C:\Users\dorov\Documents\ByBot
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

Expected safety fields include `"status": "ok"` from `/health` and
`"live_trading": false` from `/status`. Stop the server with `Ctrl+C`.

If Python 3.11 is not installed but a newer supported version is available,
replace both `py -3.11` occurrences with `py -3`.

### Run tests later

```powershell
cd C:\Users\dorov\Documents\ByBot
.\.venv\Scripts\python.exe -m pytest -q
```

## Docker Compose

```powershell
Copy-Item .env.example .env
docker compose up --build
```

The API is exposed on port 8000 and PostgreSQL on port 5432. Phase 1 does not
yet persist data; the database service is prepared for the next phase.

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
