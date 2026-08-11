# Parallel agent tracks: ownership boundary

Two agents develop ByBot in parallel from the same git history. This document is
the contract that keeps the two tracks mergeable. It grants no new execution
permission: every safety rule in `AGENTS.md` applies unchanged to both tracks.

## Tracks

| | Codex track | Claude track |
|---|---|---|
| Branch | `feature/bybot-v2` | `feature/bybot-claude` |
| Worktree | `D:\VibeProjects\projects` | `D:\VibeProjects\bybot-claude` |
| ByBot path | `…\projects\projects\ByBot` | `…\bybot-claude\projects\ByBot` |
| Database | `bybot` | `bybot_claude` |
| Virtualenv | own `.venv` | own `.venv` |
| Focus | runtime, Demo execution, microstructure shadow collection | historical data ingestion, offline backtest, alpha research |

Both branches share one `.git` (worktree, not a clone). `main` is the trunk and
was fast-forwarded to `b1ffd44` on 2026-08-11.

## File ownership

A track may freely create, edit and delete files it owns. It must not edit files
owned by the other track.

### Codex owns

- `app/v2/`, `app/v4/`, `app/v5/`
- `app/bybit/`, `app/microstructure/`
- `app/main.py`, `app/runtime.py`, `app/startup.py`
- `app/news/`, `app/signals/`, `app/risk/`, `app/portfolio/`
- `scripts/demo_*`, `scripts/*.ps1`, `scripts/alpha_lab_v*.py`
- `tests/` files covering the above

### Claude owns

- `app/history/` — historical market/funding/OI ingestion (new)
- `app/backtest/` — offline backtest engine (new)
- `app/research/` — shared research primitives (new)
- `scripts/history_*.py`, `scripts/backtest_*.py`
- `tests/test_history_*.py`, `tests/test_backtest_*.py`
- `docs/AGENT_BOUNDARY.md` (this file)

### Shared — change only by explicit agreement

These are the real merge-conflict surface. Neither track edits them without the
operator deciding first:

- `app/config.py`
- `app/db/models.py`, `app/db/persistence.py`
- `app/models.py`
- `.env.example`
- `AGENTS.md`, `README.md`, `PLAN.md`
- `requirements.txt`, `requirements-dev.txt`
- `alembic/versions/` — see below

## Migrations

`alembic/versions/` is a linear chain and cannot absorb two parallel heads
cleanly. Current file head is `20260811_0015`; the `bybot` database is at
`20260715_0014`.

Rule: **the Claude track does not add migrations to `alembic/versions/`.**
Historical-research tables live in a separate schema created by an idempotent
script under `app/history/`, applied only to `bybot_claude`. If a research table
must eventually become part of the production schema, it is promoted as a single
migration authored on the Codex track at merge time.

This keeps `alembic upgrade head` meaning exactly one thing on the Codex side.

## Database

`bybot` holds the project's most valuable asset — collected market snapshots and
durable execution history — and is not in git.

- The Claude track treats `bybot` as **read-only input**. It never runs
  migrations, writes, or `DROP` against it.
- Research runs against `bybot_claude`, seeded from a dump of `bybot`.
- Reseed with a fresh dump rather than mutating `bybot` to suit research.

## History rewriting

The Codex track amends and rebases published commits (`19c1230` became `b1ffd44`
on 2026-08-11 with an unchanged message and author date). Consequences:

- Do not assume a commit hash on `feature/bybot-v2` is stable.
- Never rebase `feature/bybot-claude` onto a moving `feature/bybot-v2`; sync
  through `main`, and only when `main` moves forward as a fast-forward.
- Before any merge between tracks, re-read the actual tip; do not trust a hash
  recorded earlier.

## Sync protocol

1. Codex track merges into `main` when a milestone is stable.
2. Claude track merges `main` in (`git merge main`), never rebases onto it.
3. Merging the Claude track back into `main` is an operator decision, made only
   after its own tests pass and the shared-file list above is untouched or
   explicitly negotiated.

## Unchanged safety rules

- `BYBIT_ENABLE_TRADING=false` and `BYBIT_LIVE_TRADING_ENABLED=false` stay permanent.
- The Claude track is offline research; it submits no orders and needs no
  exchange mutation path at all.
- Demo soak and canary scripts remain operator-launched only, on the Codex track.
