from __future__ import annotations

import argparse
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation
import json
import math
import os
from pathlib import Path
import socket
import subprocess
import sys
import time
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

from sqlalchemy import func, select
from sqlalchemy.orm import Session


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.bybit.demo_diagnostics import DemoDiagnosticsConfig
from app.db.readiness import wait_for_persistence
from app.db.persistence import (
    PersistenceRepository,
    V2MarketFeatureRow,
)
from app.v2.models import MarketFeatureSnapshot, UniverseStatus


TERMINAL = {"DEMO_CLOSED", "DEMO_CLOSED_EXTERNALLY"}
PREFERRED_CANARY_SYMBOLS = ("XRPUSDT", "ADAUSDT")
FALLBACK_CANARY_SYMBOLS = (
    "SOLUSDT", "AVAXUSDT", "SUIUSDT", "LINKUSDT",
    "DOGEUSDT", "NEARUSDT", "LTCUSDT", "BTCUSDT",
)


class ControllerFailure(RuntimeError):
    def __init__(self, category: str, message: str) -> None:
        super().__init__(message)
        self.category = category


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Guarded production-path two-close terminalization canary."
    )
    parser.add_argument("--allow-demo-orders", action="store_true")
    parser.add_argument("--symbols", nargs=2)
    parser.add_argument("--notional-tier", type=int, choices=[100], default=100)
    parser.add_argument("--startup-timeout", type=int, default=60)
    args = parser.parse_args()
    if not args.allow_demo_orders:
        raise SystemExit("explicit --allow-demo-orders is required")

    run_id = datetime.now(timezone.utc).strftime(
        "demo-v2-two-close-%Y%m%dT%H%M%S%fZ"
    )
    artifact_dir = ROOT / "artifacts" / "demo-v2" / run_id
    monitor_dir = artifact_dir / "certification-monitor"
    artifact_dir.mkdir(parents=True, exist_ok=False)
    stdout_path = artifact_dir / "uvicorn.stdout.log"
    stderr_path = artifact_dir / "uvicorn.stderr.log"
    report_path = artifact_dir / "report.json"
    port = available_port()
    base_url = f"http://127.0.0.1:{port}"
    child: subprocess.Popen[bytes] | None = None
    monitor: subprocess.Popen[bytes] | None = None
    report: dict[str, Any] = {
        "run_id": run_id,
        "started_at": datetime.now(timezone.utc).isoformat(),
        "result": "FAIL",
        "requested_symbols": args.symbols,
        "live_execution_blocked": True,
        "controller_steps": [],
    }
    try:
        config = DemoDiagnosticsConfig.load(env_path=ROOT / ".env")
        env = guarded_environment(run_id, config.database_url)
        readiness_details: dict[str, Any] = {}

        def verify_postgresql() -> None:
            result = wait_for_persistence(
                config.database_url,
                timeout_seconds=35,
                connection_timeout_seconds=4,
                create_schema=False,
                status_callback=lambda payload: readiness_details.update(payload),
            )
            result.repository.dispose()

        run_controller_step(
            report, "postgresql_readiness", verify_postgresql, timeout_seconds=40
        )
        report["controller_postgresql_readiness"] = readiness_details

        def start_uvicorn() -> subprocess.Popen[bytes]:
            return subprocess.Popen(
                [
                    str(ROOT / ".venv" / "Scripts" / "python.exe"),
                    "-m",
                    "uvicorn",
                    "app.main:app",
                    "--host",
                    "127.0.0.1",
                    "--port",
                    str(port),
                ],
                cwd=ROOT,
                env=env,
                stdout=stdout_path.open("wb"),
                stderr=stderr_path.open("wb"),
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            )

        child = run_controller_step(
            report, "uvicorn_process_start", start_uvicorn, timeout_seconds=5
        )
        startup_artifact = artifact_dir / "startup" / "startup-diagnostics.json"
        uvicorn_ready = run_controller_step(
            report,
            "application_startup_readiness",
            lambda: wait_ready(
                base_url, child, args.startup_timeout, startup_artifact
            ),
            timeout_seconds=args.startup_timeout + 2,
        )
        report["uvicorn_readiness_duration_ms"] = uvicorn_ready

        startup_status = run_controller_step(
            report,
            "startup_status_validation",
            lambda: validate_startup_status(
                get_json(base_url, "/startup/status")
            ),
            timeout_seconds=10,
        )
        report["application_persistence_readiness"] = startup_status[
            "persistence_connect"
        ]
        runtime_status = get_json(base_url, "/v2/status")
        accepted_symbols = list(runtime_status.get("accepted_symbols") or [])
        feature_readiness = run_controller_step(
            report,
            "CANARY_MARKET_FEATURE_READY",
            lambda: wait_for_market_feature_readiness(
                config.database_url,
                accepted_symbols=accepted_symbols,
                requested_symbols=args.symbols,
                started_at=datetime.now(timezone.utc),
                timeout_seconds=45,
                poll_seconds=0.35,
                freshness_seconds=read_int_setting(
                    ROOT / ".env", "V2_MARKET_STALE_SECONDS", 15
                ),
                max_new_entries_per_5_minutes=read_int_setting(
                    ROOT / ".env", "MAX_NEW_ENTRIES_PER_5_MINUTES", 5
                ),
                max_trades_per_day=read_int_setting(
                    ROOT / ".env", "MAX_TRADES_PER_DAY", 100
                ),
            ),
            timeout_seconds=46,
        )
        args.symbols = feature_readiness["selected_symbols"]
        report["symbols"] = args.symbols
        report["market_feature_readiness"] = feature_readiness
        report["market_feature_warmup_duration_ms"] = feature_readiness[
            "warmup_duration_ms"
        ]
        demo = run_controller_step(
            report,
            "diagnostics",
            lambda: validate_demo_status(get_json(base_url, "/demo/status")),
            timeout_seconds=20,
        )
        preflight = run_controller_step(
            report,
            "preflight",
            lambda: validate_preflight(get_json(base_url, "/v2/preflight")),
            timeout_seconds=30,
        )
        report["diagnostics_result"] = "PASS"
        report["preflight_result"] = "PASS"
        report["preflight_accepted_symbols"] = preflight.get("accepted_symbols")

        execution_step = begin_controller_step(
            report, "canary_execution", timeout_seconds=480
        )
        opened: list[dict[str, Any]] = []
        for symbol in args.symbols:
            response = post_json(
                base_url, f"/v2/canary/sizing/{symbol}/{args.notional_tier}"
            )
            execution_id = str(response.get("execution_id") or "")
            require(execution_id, f"{symbol} execution ID missing")
            record = wait_execution(base_url, execution_id, {"DEMO_POSITION_OPEN"}, 120)
            require(record.get("protection_confirmed") is True, f"{symbol} protection absent")
            require(record.get("tp_order_id"), f"{symbol} TP ID missing")
            require(record.get("sl_order_id"), f"{symbol} SL ID missing")
            opened.append(record)

        monitor_dir.mkdir(parents=True, exist_ok=True)
        monitor = subprocess.Popen(
            [
                str(ROOT / ".venv" / "Scripts" / "python.exe"),
                str(ROOT / "scripts" / "demo_v2_certification_monitor.py"),
                "--run-id",
                run_id,
                "--runner-pid",
                str(os.getpid()),
                "--uvicorn-pid",
                str(child.pid),
                "--base-url",
                base_url,
                "--output-dir",
                str(monitor_dir),
                "--hard-timeout-seconds",
                "30",
                "--idle-poll-seconds",
                "1",
                "--active-poll-seconds",
                "1",
                "--drain-poll-seconds",
                "1",
                "--retry-poll-seconds",
                "1",
                "--inject-status-timeouts",
                "3",
                "--max-polls",
                "5",
            ],
            cwd=ROOT,
            env=env,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
        time.sleep(0.25)
        with ThreadPoolExecutor(max_workers=2) as pool:
            close_results = list(pool.map(
                lambda item: post_json(
                    base_url, f"/demo/canary/{item['id']}/close"
                ),
                opened,
            ))
        require(all(item.get("reduce_only") is True for item in close_results), "close not reduce-only")
        closed = [
            wait_execution(base_url, str(item["id"]), TERMINAL, 120)
            for item in opened
        ]
        post_json(base_url, "/demo/reconcile")
        post_json(base_url, "/v2/stop-new-entries")
        post_json(base_url, "/demo/reconcile")
        monitor_code = monitor.wait(timeout=90)
        require(monitor_code == 0, f"monitor exited {monitor_code}")
        monitor_result = json.loads(
            (monitor_dir / "monitor-result.json").read_text(encoding="utf-8")
        )
        require(
            monitor_result.get("result") == "OBSERVATION_COMPLETE",
            f"monitor result={monitor_result.get('result')}",
        )
        events = read_jsonl(monitor_dir / "monitor-events.jsonl")
        pending_events = [
            item for item in events
            if item.get("event") == "MONITOR_TERMINALIZATION_PENDING"
        ]
        expected_pending_ids = {str(item["id"]) for item in opened}
        observed_pending_ids = terminalization_pending_execution_ids(events)
        require(
            not any(item.get("event") == "MONITOR_FAIL_FAST" for item in events),
            "monitor fail-fast occurred",
        )
        require(
            expected_pending_ids.issubset(observed_pending_ids),
            (
                "monitor did not observe both exact terminalization windows: "
                f"expected={sorted(expected_pending_ids)} "
                f"observed={sorted(observed_pending_ids)}"
            ),
        )
        final_v2 = wait_phase(base_url, "FINISHED", 60)
        final_demo = get_json(base_url, "/demo/status")
        executions = [
            item for item in get_json(base_url, "/demo/executions").get("executions", [])
            if item.get("run_id") == run_id
        ]
        require(len(executions) == 2, "canary did not create exactly two executions")
        require(all(item.get("state") in TERMINAL for item in executions), "execution non-terminal")
        require(all(
            item.get("accounting_status") == "FINAL"
            and decimal_text(item.get("realized_exchange_pnl"))
            == decimal_text(item.get("authoritative_closed_pnl"))
            for item in executions
        ), "durable/exchange PnL mismatch")
        repository = PersistenceRepository(
            config.database_url, create_schema=False
        )
        terminal_counts = {
            str(item["id"]): sum(
                event.get("event_type") == "DEMO_CLOSE_TERMINALIZED"
                for event in repository.load_demo_execution_events(str(item["id"]))
            )
            for item in executions
        }
        portfolio_state = repository.load_v2_portfolio_state() or {}
        realized_events = dict(portfolio_state.get("realized_events") or {})
        reservations = [
            item for item in portfolio_state.get("reservations") or []
            if item.get("run_id") == run_id
        ]
        ledger_count = sum(
            str(item["id"]) in realized_events for item in executions
        )
        release_count = sum(
            item.get("state") == "RELEASED" for item in reservations
        )
        require(
            terminal_counts and all(value == 1 for value in terminal_counts.values()),
            f"terminal event counts are not exactly once: {terminal_counts}",
        )
        require(ledger_count == 2, f"ledger finalizations={ledger_count}")
        require(release_count == 2, f"capacity releases={release_count}")
        require(int(final_demo.get("bot_owned_open_positions") or 0) == 0, "final position open")
        require(int(final_demo.get("bot_owned_open_orders") or 0) == 0, "final order open")
        require(int(final_demo.get("confirmed_unrelated_orders") or 0) == 0, "unrelated order")
        require(not final_demo.get("kill_switch_active"), "kill switch active")
        report.update({
            "result": "PASS",
            "finished_at": datetime.now(timezone.utc).isoformat(),
            "execution_ids": [str(item["id"]) for item in executions],
            "states": [item["state"] for item in executions],
            "accepted_quantities": [item["accepted_quantity"] for item in executions],
            "terminal_events": sum(terminal_counts.values()),
            "terminal_events_by_execution": terminal_counts,
            "ledger_finalizations": ledger_count,
            "capacity_releases": release_count,
            "monitor_terminalization_pending_events": len(pending_events),
            "monitor": monitor_result,
            "cycle_failures": int(final_v2.get("total_cycle_failures") or 0),
            "persistence_status": final_v2.get("persistence_status"),
            "final_phase": final_v2.get("run_phase"),
            "final_positions": 0,
            "final_orders": 0,
            "unresolved_executions": 0,
            "no_emergency_cleanup": True,
        })
        require(report["cycle_failures"] == 0, "cycle failure emitted")
        require(report["persistence_status"] == "OK", "persistence failure")
        finish_controller_step(report, execution_step, "PASS")
        print("TWO POSITIONS PROTECTED: PASS")
        print("TERMINALIZATION_PENDING: PASS")
        print("TWO TERMINAL EVENTS: PASS")
        print("TWO LEDGER FINALIZATIONS: PASS")
        print("TWO CAPACITY RELEASES: PASS")
        print("FINAL DEMO STATE FLAT: PASS")
        print("OVERALL: PASS")
        return 0
    except Exception as exc:
        active_stage = report.get("_active_controller_step")
        fail_active_controller_step(report, exc)
        report["failure_stage"] = getattr(
            exc, "category", active_stage
        )
        report["failure"] = f"{type(exc).__name__}: {' '.join(str(exc).split())[:500]}"
        report["finished_at"] = datetime.now(timezone.utc).isoformat()
        print(report["failure"], file=sys.stderr)
        return 2
    finally:
        report.pop("_active_controller_step", None)
        report_path.write_text(json.dumps(report, indent=2), encoding="utf-8")
        if monitor and monitor.poll() is None:
            terminate(monitor)
        if child and child.poll() is None:
            terminate(child)


def guarded_environment(run_id: str, database_url: str) -> dict[str, str]:
    env = dict(os.environ)
    env.update({
        "APP_ENV": "demo",
        "TEST_MODE": "false",
        "BOT_MODE": "BYBIT_DEMO",
        "EXECUTION_MODE": "BYBIT_DEMO",
        "BYBIT_ENV": "demo",
        "BYBIT_DEMO_TRADING_ENABLED": "true",
        "DEMO_ORDER_EXECUTION_AUTHORIZED": "true",
        "BYBIT_LIVE_TRADING_ENABLED": "false",
        "BYBIT_ENABLE_TRADING": "false",
        "AUTO_PAPER_EXECUTION": "false",
        "DEMO_CANARY_ENABLED": "true",
        "V2_ENABLED": "true",
        "V2_AUTO_DEMO_EXECUTION": "false",
        "BYBIT_PRIVATE_DEMO_BASE_URL": "https://api-demo.bybit.com",
        "BYBIT_PRIVATE_DEMO_WS_URL": "wss://stream-demo.bybit.com",
        "DEMO_RUN_ID": run_id,
        "DEMO_RUN_STARTED_AT": datetime.now(timezone.utc).isoformat(),
        "NEWS_ENABLE_RSS": "false",
        "MARKET_DATA_PROVIDER": "BYBIT_REST",
        # DemoDiagnosticsConfig has already normalized the Docker hostname to
        # the Windows-host endpoint. The value is never printed or reported.
        "DATABASE_URL": database_url,
    })
    return env


def get_json(base_url: str, path: str, timeout: int = 15) -> dict[str, Any]:
    with urlopen(base_url.rstrip("/") + path, timeout=timeout) as response:
        return json.loads(response.read().decode("utf-8"))


def post_json(base_url: str, path: str, timeout: int = 90) -> dict[str, Any]:
    request = Request(
        base_url.rstrip("/") + path,
        method="POST",
        headers={"Content-Type": "application/json"},
        data=b"{}",
    )
    try:
        with urlopen(request, timeout=timeout) as response:
            return json.loads(response.read().decode("utf-8"))
    except HTTPError as exc:
        body = exc.read().decode("utf-8", errors="replace")
        try:
            detail = json.loads(body).get("detail")
        except (json.JSONDecodeError, AttributeError):
            detail = body[:500]
        raise ControllerFailure(
            "canary_execution",
            f"HTTP {exc.code} for {path}: {detail}",
        ) from exc


def wait_ready(
    base_url: str,
    child: subprocess.Popen[bytes],
    timeout: int,
    startup_artifact: Path,
) -> int:
    started = time.monotonic()
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if child.poll() is not None:
            child.wait()
            startup = read_json(startup_artifact)
            persistence = startup.get("persistence_connect") or {}
            if persistence.get("state") == "FAIL":
                raise ControllerFailure(
                    "persistence_startup_failure",
                    "bounded PostgreSQL persistence startup failed",
                )
            raise ControllerFailure(
                "uvicorn_crash",
                f"Uvicorn exited before readiness with code {child.returncode}",
            )
        try:
            get_json(base_url, "/health", 2)
            return round((time.monotonic() - started) * 1000)
        except Exception:
            time.sleep(0.5)
    raise ControllerFailure(
        "readiness_timeout",
        f"application readiness exceeded {timeout} seconds",
    )


def validate_startup_status(payload: dict[str, Any]) -> dict[str, Any]:
    persistence = payload.get("persistence_connect") or {}
    require(payload.get("state") == "READY", "application startup is not READY")
    require(
        payload.get("startup_final_result") == "PASS",
        "startup final result is not PASS",
    )
    require(
        persistence.get("state") == "PASS",
        "persistence startup did not reach PASS",
    )
    require(
        persistence.get("repository_available") is True,
        "persistence repository is unavailable",
    )
    require(
        persistence.get("health_query_passed") is True,
        "persistence health query did not pass",
    )
    return payload


def validate_demo_status(payload: dict[str, Any]) -> dict[str, Any]:
    require(not payload.get("kill_switch_active"), "kill switch active")
    require(
        int(payload.get("bot_owned_open_positions") or 0) == 0,
        "account not flat",
    )
    require(
        int(payload.get("bot_owned_open_orders") or 0) == 0,
        "open bot order exists",
    )
    require(
        int(payload.get("confirmed_unrelated_orders") or 0) == 0,
        "unrelated order exists",
    )
    return payload


def validate_preflight(payload: dict[str, Any]) -> dict[str, Any]:
    require(
        payload.get("ok") is True,
        f"preflight blocked: {payload.get('blockers')}",
    )
    return payload


def select_canary_symbols(
    accepted_symbols: list[str],
    requested_symbols: list[str] | None = None,
) -> list[str]:
    accepted = {str(value).upper() for value in accepted_symbols}
    if requested_symbols:
        selected = [str(value).upper() for value in requested_symbols]
        if len(set(selected)) != 2 or any(
            symbol not in accepted for symbol in selected
        ):
            raise ControllerFailure(
                "CANARY_MARKET_FEATURE_READY",
                "requested canary symbols are not two distinct accepted symbols",
            )
        return selected
    ordered = [
        symbol for symbol in (
            *PREFERRED_CANARY_SYMBOLS, *FALLBACK_CANARY_SYMBOLS
        )
        if symbol in accepted
    ]
    if len(ordered) < 2:
        raise ControllerFailure(
            "CANARY_MARKET_FEATURE_READY",
            "fewer than two preferred/fallback Demo symbols are accepted",
        )
    return ordered


def wait_for_market_feature_readiness(
    database_url: str,
    *,
    accepted_symbols: list[str],
    requested_symbols: list[str] | None,
    started_at: datetime,
    timeout_seconds: float,
    poll_seconds: float,
    freshness_seconds: int,
    max_new_entries_per_5_minutes: int = 5,
    max_trades_per_day: int = 100,
    required_count: int = 2,
) -> dict[str, Any]:
    ordered = select_canary_symbols(accepted_symbols, requested_symbols)
    repository = PersistenceRepository(
        database_url, create_schema=False, connect_timeout_seconds=4
    )
    if not repository.available or not repository.health_check():
        repository.dispose()
        raise ControllerFailure(
            "CANARY_MARKET_FEATURE_READY",
            "PostgreSQL became unavailable during feature readiness",
        )
    statuses = {
        row.symbol.value: row for row in repository.load_v2_universe()
    }
    started = time.monotonic()
    attempted: dict[str, dict[str, Any]] = {}
    try:
        deadline = started + timeout_seconds
        while time.monotonic() < deadline:
            rows = latest_feature_rows(
                repository, ordered, captured_after=started_at
            )
            ready: list[str] = []
            attempted = {}
            now = datetime.now(timezone.utc)
            portfolio_state = repository.load_v2_portfolio_state() or {}
            for symbol in ordered:
                evaluation = evaluate_feature_readiness(
                    symbol=symbol,
                    universe_status=statuses.get(symbol),
                    payload=(
                        rows[symbol].payload if symbol in rows else None
                    ),
                    now=now,
                    freshness_seconds=freshness_seconds,
                )
                portfolio_reasons = canary_portfolio_reasons(
                    portfolio_state,
                    symbol=symbol,
                    now=now,
                    max_new_entries_per_5_minutes=(
                        max_new_entries_per_5_minutes
                    ),
                    max_trades_per_day=max_trades_per_day,
                    required_slots=required_count,
                )
                evaluation["portfolio_eligible"] = not portfolio_reasons
                evaluation["portfolio_reasons"] = portfolio_reasons
                attempted[symbol] = evaluation
                if evaluation["ready"] and not portfolio_reasons:
                    ready.append(symbol)
            if len(ready) >= required_count:
                selected = ready[:required_count]
                elapsed_ms = round((time.monotonic() - started) * 1000)
                return {
                    "state": "PASS",
                    "selected_symbols": selected,
                    "warmup_duration_ms": elapsed_ms,
                    "freshness_limit_seconds": freshness_seconds,
                    "attempted_symbols": attempted,
                    "selected_snapshots": {
                        symbol: attempted[symbol] for symbol in selected
                    },
                }
            time.sleep(poll_seconds)
    finally:
        repository.dispose()
    compact = {
        symbol: {
            "ready": value.get("ready"),
            "source": value.get("source"),
            "age_seconds": value.get("age_seconds"),
            "missing_fields": value.get("missing_fields"),
            "reason": value.get("reason"),
        }
        for symbol, value in attempted.items()
    }
    raise ControllerFailure(
        "CANARY_MARKET_FEATURE_READY",
        f"{required_count} fresh production feature snapshot(s) "
        "were not available within "
        f"{timeout_seconds} seconds: {json.dumps(compact, sort_keys=True)}",
    )


def canary_portfolio_reasons(
    state: dict[str, Any],
    *,
    symbol: str,
    now: datetime,
    max_new_entries_per_5_minutes: int,
    max_trades_per_day: int,
    required_slots: int = 2,
) -> list[str]:
    """Mirror durable admission limits before a canary creates a candidate."""
    current = ensure_utc(now)
    reasons: list[str] = []
    cooldown_value = (state.get("symbol_cooldowns") or {}).get(symbol)
    if cooldown_value:
        try:
            cooldown_until = ensure_utc(
                datetime.fromisoformat(str(cooldown_value).replace("Z", "+00:00"))
            )
        except ValueError:
            reasons.append("invalid durable symbol cooldown")
        else:
            if cooldown_until > current:
                reasons.append("symbol cooldown is active")
    reservations = list(state.get("reservations") or [])
    counted = [
        item for item in reservations
        if (
            str(item.get("state") or "") in {"RESERVED", "EXECUTING", "OPEN"}
            or item.get("execution_id") is not None
        )
    ]
    recent = [
        item for item in counted
        if (
            timestamp := _optional_utc(item.get("created_at"))
        ) is not None
        and current - timestamp <= timedelta(minutes=5)
    ]
    if len(recent) + required_slots > max_new_entries_per_5_minutes:
        reasons.append("five-minute entry capacity is unavailable for two positions")
    today = [
        item for item in counted
        if (
            timestamp := _optional_utc(item.get("created_at"))
        ) is not None
        and timestamp.date() == current.date()
    ]
    if len(today) + required_slots > max_trades_per_day:
        reasons.append("daily trade capacity is unavailable for two positions")
    return reasons


def _optional_utc(value: Any) -> datetime | None:
    if not value:
        return None
    try:
        return ensure_utc(
            datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        )
    except (TypeError, ValueError):
        return None


def latest_feature_rows(
    repository: PersistenceRepository,
    symbols: list[str],
    *,
    captured_after: datetime,
) -> dict[str, V2MarketFeatureRow]:
    latest = (
        select(
            V2MarketFeatureRow.symbol,
            func.max(V2MarketFeatureRow.captured_at).label("captured_at"),
        )
        .where(
            V2MarketFeatureRow.symbol.in_(symbols),
            V2MarketFeatureRow.captured_at >= captured_after,
        )
        .group_by(V2MarketFeatureRow.symbol)
        .subquery()
    )
    query = select(V2MarketFeatureRow).join(
        latest,
        (V2MarketFeatureRow.symbol == latest.c.symbol)
        & (V2MarketFeatureRow.captured_at == latest.c.captured_at),
    )
    with Session(repository.engine) as session:
        return {
            row.symbol: row for row in session.scalars(query).all()
        }


def evaluate_feature_readiness(
    *,
    symbol: str,
    universe_status: UniverseStatus | None,
    payload: dict[str, Any] | None,
    now: datetime,
    freshness_seconds: int,
) -> dict[str, Any]:
    missing: list[str] = []
    if (
        universe_status is None
        or not universe_status.accepted
        or universe_status.instrument is None
    ):
        missing.append("accepted_instrument_metadata")
        raw_category = None
        normalized_category = None
    else:
        instrument = universe_status.instrument
        raw_category = instrument.category
        normalized_category = normalize_linear_category(raw_category)
        if (
            not instrument.exists
            or instrument.status != "Trading"
            or normalized_category is None
            or instrument.settle_coin != "USDT"
        ):
            missing.append("tradable_linear_usdt_instrument")
        for field in (
            "min_order_qty", "qty_step", "tick_size", "max_leverage",
        ):
            if not decimal_is_positive(getattr(instrument, field, None)):
                missing.append(f"instrument.{field}")
        if not decimal_is_nonnegative(instrument.min_notional_value):
            missing.append("instrument.min_notional_value")
    feature: MarketFeatureSnapshot | None = None
    if payload is None:
        missing.append("feature_snapshot")
    else:
        try:
            feature = MarketFeatureSnapshot.model_validate(payload)
        except Exception:
            missing.append("valid_feature_payload")
    age: float | None = None
    source = None
    exchange_timestamp = None
    received_at = None
    if feature is not None:
        received_at = ensure_utc(feature.timestamp)
        age = (now - received_at).total_seconds()
        if not feature.fresh:
            missing.append("feature.fresh")
        if not math.isfinite(age) or age < 0 or age > freshness_seconds:
            missing.append("feature.timestamp_fresh")
        for field in ("last_price", "bid_price", "ask_price"):
            if not decimal_is_positive(getattr(feature, field, None)):
                missing.append(f"feature.{field}")
        if feature.ask_price < feature.bid_price:
            missing.append("feature.non_crossed_bid_ask")
        for field in (
            "bid_depth_usdt", "ask_depth_usdt",
            "bid_depth_10bps_usdt", "ask_depth_10bps_usdt",
        ):
            if not decimal_is_positive(getattr(feature, field, None)):
                missing.append(f"feature.{field}")
        ticker_at = feature.source_timestamps.get("ticker")
        orderbook_at = feature.source_timestamps.get("orderbook")
        if ticker_at is None:
            missing.append("source_timestamps.ticker")
        if orderbook_at is None:
            missing.append("source_timestamps.orderbook")
        if ticker_at is not None and orderbook_at is not None:
            exchange_timestamp = min(
                ensure_utc(ticker_at), ensure_utc(orderbook_at)
            )
            source = "WS"
    return {
        "symbol": symbol,
        "ready": not missing,
        "raw_category": raw_category,
        "normalized_category": normalized_category,
        "source": source,
        "age_seconds": round(age, 6) if age is not None else None,
        "exchange_timestamp": (
            exchange_timestamp.isoformat() if exchange_timestamp else None
        ),
        "received_at": received_at.isoformat() if received_at else None,
        "missing_fields": list(dict.fromkeys(missing)),
        "reason": None if not missing else "required_feature_data_unavailable",
    }


def normalize_linear_category(value: Any) -> str | None:
    normalized = "".join(
        character for character in str(value or "").strip().lower()
        if character.isalnum()
    )
    if normalized in {"linear", "linearperpetual"}:
        return "LINEAR_PERPETUAL"
    return None


def decimal_is_positive(value: Any) -> bool:
    try:
        parsed = Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError):
        return False
    return parsed.is_finite() and parsed > 0


def decimal_is_nonnegative(value: Any) -> bool:
    try:
        parsed = Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError):
        return False
    return parsed.is_finite() and parsed >= 0


def ensure_utc(value: datetime) -> datetime:
    return (
        value.replace(tzinfo=timezone.utc)
        if value.tzinfo is None else value.astimezone(timezone.utc)
    )


def read_int_setting(path: Path, name: str, default: int) -> int:
    if not path.exists():
        return default
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        if key.strip() == name:
            try:
                return int(value.strip().strip('"').strip("'"))
            except ValueError:
                return default
    return default


def run_controller_step(
    report: dict[str, Any],
    name: str,
    action,
    *,
    timeout_seconds: float,
):
    record = begin_controller_step(
        report, name, timeout_seconds=timeout_seconds
    )
    try:
        result = action()
    except Exception as exc:
        finish_controller_step(report, record, "FAIL", exc)
        raise
    finish_controller_step(report, record, "PASS")
    return result


def begin_controller_step(
    report: dict[str, Any], name: str, *, timeout_seconds: float
) -> dict[str, Any]:
    record = {
        "name": name,
        "status": "RUNNING",
        "started_at": datetime.now(timezone.utc).isoformat(),
        "timeout_seconds": timeout_seconds,
        "_started": time.monotonic(),
    }
    report["controller_steps"].append(record)
    report["_active_controller_step"] = name
    return record


def finish_controller_step(
    report: dict[str, Any],
    record: dict[str, Any],
    status: str,
    error: BaseException | None = None,
) -> None:
    started = float(record.pop("_started", time.monotonic()))
    record["status"] = status
    record["finished_at"] = datetime.now(timezone.utc).isoformat()
    record["elapsed_ms"] = round((time.monotonic() - started) * 1000)
    record["error_type"] = type(error).__name__ if error else None
    if report.get("_active_controller_step") == record["name"]:
        report.pop("_active_controller_step", None)


def fail_active_controller_step(
    report: dict[str, Any], error: BaseException
) -> None:
    active = report.get("_active_controller_step")
    if not active:
        return
    for record in reversed(report.get("controller_steps") or []):
        if record.get("name") == active and record.get("status") == "RUNNING":
            finish_controller_step(report, record, "FAIL", error)
            return


def wait_execution(
    base_url: str, execution_id: str, states: set[str], timeout: int,
) -> dict[str, Any]:
    deadline = time.monotonic() + timeout
    last_error: BaseException | None = None
    while time.monotonic() < deadline:
        try:
            payload = get_json(base_url, f"/demo/canary/{execution_id}", 5)
        except (TimeoutError, URLError, ConnectionError, OSError) as exc:
            last_error = exc
            time.sleep(0.5)
            continue
        record = payload.get("execution") or {}
        if record.get("state") in states:
            return record
        time.sleep(0.5)
    detail = (
        f"; last polling error={type(last_error).__name__}: "
        f"{' '.join(str(last_error).split())[:200]}"
        if last_error else ""
    )
    raise TimeoutError(
        f"execution {execution_id} did not reach {sorted(states)}{detail}"
    )


def terminalization_pending_execution_ids(
    events: list[dict[str, Any]],
) -> set[str]:
    """Return exact covered executions; repeated live/resolved evidence is valid."""
    return {
        str(execution_id)
        for event in events
        if event.get("event") == "MONITOR_TERMINALIZATION_PENDING"
        for execution_id in (event.get("execution_ids") or [])
        if execution_id
    }


def wait_phase(base_url: str, phase: str, timeout: int) -> dict[str, Any]:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        status = get_json(base_url, "/v2/status", 5)
        if status.get("run_phase") == phase:
            return status
        post_json(base_url, "/demo/reconcile", 20)
        time.sleep(0.5)
    raise TimeoutError(f"V2 run did not reach {phase}")


def available_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def terminate(process: subprocess.Popen[bytes]) -> None:
    process.terminate()
    try:
        process.wait(timeout=10)
    except subprocess.TimeoutExpired:
        process.kill()
        process.wait(timeout=10)


def read_jsonl(path: Path) -> list[dict[str, Any]]:
    if not path.exists():
        return []
    return [
        json.loads(line)
        for line in path.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]


def read_json(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {}
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}


def decimal_text(value: Any) -> str:
    from decimal import Decimal
    return str(Decimal(str(value)))


def require(condition: bool, message: str) -> None:
    if not condition:
        raise RuntimeError(message)


if __name__ == "__main__":
    raise SystemExit(main())
