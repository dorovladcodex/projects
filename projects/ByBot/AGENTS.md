# ByBot development instructions

## Scope and safety

ByBot is a Python 3.11 modular monolith for BTCUSDT/ETHUSDT linear perpetual
research and PAPER execution. `BYBIT_ENABLE_TRADING=false` is permanent in this
version. Never add or call Bybit demo/live order placement. Automatic execution
may open PAPER positions only. Local mutation/test endpoints require
`APP_ENV=local`, `TEST_MODE=true`, and PAPER mode.

## Important paths

- `app/main.py`: FastAPI wiring, lifecycle loops, local test endpoints.
- `app/config.py`: Pydantic environment settings and hard safety validation.
- `app/news/`: RSS normalization/filtering and mock/LLM/Codex classifiers.
- `app/bybit/`: public market data and read-only private account clients.
- `app/signals/service.py`: news candidate lifecycle and execution gate.
- `app/risk/manager.py`: deterministic sizing and risk preview.
- `app/portfolio/paper_trading.py`: paper positions, accounting, cooldowns, kill switch.
- `app/db/persistence.py`: SQLAlchemy rows and atomic persistence operations.
- `alembic/versions/`: migrations; current head is `20260714_0006`.
- `tests/`: unit/API tests plus optional PostgreSQL regression tests.

PostgreSQL tables: news/classification/cache tables, signal candidates and
evaluations, risk decisions, paper executions, positions, trades, singleton
paper account, and singleton paper risk state. Paper execution and close paths
must remain durable and idempotent.

## Environment rules

- Docker services connect to PostgreSQL host `db`.
- Alembic, pytest, and FastAPI running on Windows use `127.0.0.1`.
- Use `postgresql+psycopg://...`; psycopg v3 only. Never add psycopg2.
- Keep Windows PowerShell 5.1 compatibility in `scripts/*.ps1`.
- Use `Decimal` for financial assertions/calculations where practical; never
  allow missing numeric fields to silently become zero.
- Never print `.env`, `DATABASE_URL`, API keys, authorization values, or CLI credentials.

## Validation commands

From the ByBot directory:

```powershell
.\.venv\Scripts\python.exe -m pytest -q
$env:BYBOT_TEST_POSTGRES_URL="postgresql+psycopg://...@127.0.0.1:5432/..."
.\.venv\Scripts\python.exe -m pytest tests\test_postgres_paper_execution.py -q
```

Smoke scripts (Docker Desktop and local `.venv` required):

```powershell
powershell -ExecutionPolicy Bypass -File .\scripts\postgres_e2e_smoke.ps1
powershell -ExecutionPolicy Bypass -File .\scripts\paper_execution_smoke.ps1
powershell -ExecutionPolicy Bypass -File .\scripts\auto_paper_execution_smoke.ps1
powershell -ExecutionPolicy Bypass -File .\scripts\paper_stabilization_smoke.ps1
powershell -ExecutionPolicy Bypass -File .\scripts\paper_soak.ps1 -Hours 1
```

- `postgres_e2e_smoke.ps1`: Codex/news persistence and dry-run restart recovery.
- `paper_execution_smoke.ps1`: manual PAPER execution idempotency and TP close.
- `auto_paper_execution_smoke.ps1`: automatic PAPER open/close/account recovery.
- `paper_stabilization_smoke.ps1`: limits, cooldowns, loss kill switch, restart.
- `paper_soak.ps1`: unattended real RSS/public Bybit PAPER soak with JSONL,
  accounting/invariant checks, a controlled restart, and Markdown report.

Run targeted tests while editing, the full suite once at the end, then all
smokes. Preserve unrelated user changes and keep `.env` untracked.
