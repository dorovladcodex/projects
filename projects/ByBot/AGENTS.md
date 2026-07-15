# ByBot development instructions

## Scope and safety

ByBot is a Python 3.11 modular monolith for BTCUSDT/ETHUSDT linear perpetual
research, PAPER execution, and explicitly gated Bybit Demo execution.
`BYBIT_ENABLE_TRADING=false` and `BYBIT_LIVE_TRADING_ENABLED=false` are permanent.
Live/mainnet/testnet execution must never be added or called. Demo orders are
allowed only with the complete fail-closed `APP_ENV=demo`, `TEST_MODE=false`,
`EXECUTION_MODE=BYBIT_DEMO`, `BYBIT_ENV=demo`, and
`BYBIT_DEMO_TRADING_ENABLED=true` gate and exact Demo domains. Never run the Demo
soak without the user's explicit `-AllowDemoOrders` confirmation.

## Important paths

- `app/main.py`: FastAPI wiring, lifecycle loops, local test endpoints.
- `app/config.py`: Pydantic environment settings and hard safety validation.
- `app/news/`: RSS normalization/filtering and mock/LLM/Codex classifiers.
- `app/bybit/demo.py`: exact-domain Demo adapter, private WS, reconciliation,
  exchange protection, ownership cleanup, and latched kill switch.
- `app/bybit/`: public market data and read-only private account clients.
- `app/signals/service.py`: news candidate lifecycle and execution gate.
- `app/risk/manager.py`: deterministic sizing and risk preview.
- `app/portfolio/paper_trading.py`: paper positions, accounting, cooldowns, kill switch.
- `app/db/persistence.py`: SQLAlchemy rows and atomic persistence operations.
- `app/v2/`: V2 universe, rolling features, five strategies, scoring,
  durable portfolio reservations, Demo coordinator, analytics and runtime.
- `alembic/versions/`: migrations; current head is `20260715_0014`.
- `scripts/repair_news_payloads.py`: transactional historical NewsItem audit,
  deterministic repair, and quarantine (`--dry-run` before `--apply`).
- `tests/`: unit/API tests plus optional PostgreSQL regression tests.

PostgreSQL also stores one Demo execution per candidate, deduplicated private
events, every lifecycle transition payload, and the Demo kill switch. A durable
Demo reservation must exist before create-order is called.
Malformed historical news is retained in `persistence_quarantine` and excluded
from normal news/candidate restore; never delete or bypass this audit trail.

## Environment rules

- Docker services connect to PostgreSQL host `db`.
- Alembic, pytest, and FastAPI running on Windows use `127.0.0.1`.
- Use `postgresql+psycopg://...`; psycopg v3 only. Never add psycopg2.
- Keep Windows PowerShell 5.1 compatibility in `scripts/*.ps1`.
- Use `Decimal` for financial assertions/calculations where practical; never
  allow missing numeric fields to silently become zero.
- Never print `.env`, `DATABASE_URL`, API keys, authorization values, or CLI credentials.
- V1 canary sizing uses `DEMO_RISK_CAPITAL_USDT=10000` and exactly 1x. V2 uses
  dedicated `RISK_CAPITAL_USDT`, per-category leverage and live instrument
  precision; neither path sizes from Demo wallet equity.

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
- `bybit_demo_soak.ps1`: real 12-hour Bybit Demo execution soak. It can submit
  Demo orders and therefore requires explicit `-AllowDemoOrders`; Codex must
  never launch it autonomously.
- `bybit_demo_canary.ps1`: one operator-authorized real Demo canary bounded by
  an explicit `-MaxNotionalUSDT`; it dynamically submits only the exchange
  minimum quantity and also requires
  `-AllowDemoOrders` and Codex must never launch it autonomously.
- `demo_kill_switch_diagnostics.py`: standalone GET-only Demo/DB diagnostics;
  it does not load FastAPI settings and has no exchange mutation methods.
- `demo_kill_switch_reset.py`: dry-run-by-default latch recovery; applying a
  reset requires the exact terminal execution ID and `--confirm-reset`.
- `demo_v2_preflight.py`: standalone read-only V2 account, persistence and
  17-symbol universe validation; it has no exchange mutation methods.
- `demo_v2_soak.ps1`: operator-authorized direct-Demo V2 runner. It requires
  `-AllowDemoOrders`; Codex must never launch it autonomously.

Run targeted tests while editing, the full suite once at the end, then all
smokes. Preserve unrelated user changes and keep `.env` untracked.
