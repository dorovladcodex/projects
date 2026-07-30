from __future__ import annotations

import argparse
from datetime import datetime, timezone
from decimal import Decimal
import json
import os
from pathlib import Path
import subprocess
import sys
import time
from typing import Any


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.bybit.demo import classify_demo_order_ownership
from app.bybit.demo_diagnostics import (
    DemoDiagnosticsConfig,
    ReadOnlyBybitDemoClient,
)
from app.db.readiness import wait_for_persistence
from app.db.persistence import PersistenceRepository
from scripts.demo_v2_two_close_canary import (
    available_port,
    decimal_text,
    get_json,
    guarded_environment,
    post_json,
    read_int_setting,
    require,
    terminate,
    validate_demo_status,
    validate_preflight,
    validate_startup_status,
    wait_execution,
    wait_for_market_feature_readiness,
    wait_phase,
    wait_ready,
)


TERMINAL_STATES = {"DEMO_CLOSED", "DEMO_CLOSED_EXTERNALLY"}


def main() -> int:
    parser = argparse.ArgumentParser(
        description=(
            "Guarded production-path protection-establishment monitor canary."
        )
    )
    parser.add_argument("--allow-demo-orders", action="store_true")
    parser.add_argument("--symbol")
    parser.add_argument("--notional-usdt", type=int, choices=[100], default=100)
    args = parser.parse_args()
    if not args.allow_demo_orders:
        raise SystemExit("explicit --allow-demo-orders is required")

    run_id = datetime.now(timezone.utc).strftime(
        "demo-v2-protection-pending-%Y%m%dT%H%M%S%fZ"
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
    execution_id: str | None = None
    safe_to_terminate = True
    report: dict[str, Any] = {
        "run_id": run_id,
        "started_at": datetime.now(timezone.utc).isoformat(),
        "result": "FAIL",
        "requested_symbol": args.symbol,
        "notional_usdt": args.notional_usdt,
        "live_execution_blocked": True,
        "emergency_cleanup": False,
    }
    try:
        config = DemoDiagnosticsConfig.load(env_path=ROOT / ".env")
        readiness = wait_for_persistence(
            config.database_url,
            timeout_seconds=35,
            connection_timeout_seconds=4,
            create_schema=False,
        )
        readiness.repository.dispose()
        env = guarded_environment(run_id, config.database_url)
        child = subprocess.Popen(
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
        report["uvicorn_pid"] = child.pid
        report["uvicorn_readiness_duration_ms"] = wait_ready(
            base_url,
            child,
            60,
            artifact_dir / "startup" / "startup-diagnostics.json",
        )
        validate_startup_status(get_json(base_url, "/startup/status"))
        status = get_json(base_url, "/v2/status")
        accepted = list(status.get("accepted_symbols") or [])
        feature = wait_for_market_feature_readiness(
            config.database_url,
            accepted_symbols=accepted,
            requested_symbols=([args.symbol] if args.symbol else None),
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
            required_count=1,
        )
        symbol = str(feature["selected_symbols"][0])
        report["symbol"] = symbol
        report["market_feature_readiness"] = feature
        validate_demo_status(get_json(base_url, "/demo/status"))
        validate_preflight(get_json(base_url, "/v2/preflight"))
        report["diagnostics_result"] = "PASS"
        report["preflight_result"] = "PASS"

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
                "0.5",
                "--active-poll-seconds",
                "1",
                "--drain-poll-seconds",
                "1",
                "--retry-poll-seconds",
                "1",
                "--protection-poll-seconds",
                "1",
                "--protection-timeout-seconds",
                "30",
                "--max-polls",
                "60",
            ],
            cwd=ROOT,
            env=env,
            stdout=(artifact_dir / "monitor.stdout.log").open("wb"),
            stderr=(artifact_dir / "monitor.stderr.log").open("wb"),
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
        report["monitor_pid"] = monitor.pid
        time.sleep(0.5)

        response = post_json(
            base_url,
            f"/v2/canary/sizing/{symbol}/{args.notional_usdt}",
        )
        execution_id = str(response.get("execution_id") or "")
        require(execution_id, "execution ID missing")
        safe_to_terminate = False
        report["execution_id"] = execution_id
        opened = wait_execution(
            base_url, execution_id, {"DEMO_POSITION_OPEN"}, 120
        )
        require(opened.get("protection_confirmed") is True, "protection absent")
        require(opened.get("tp_order_id"), "TP order ID missing")
        require(opened.get("sl_order_id"), "SL order ID missing")

        pending_event = wait_for_pending_event(
            monitor_dir / "monitor-events.jsonl",
            execution_id,
            timeout_seconds=20,
        )
        evidence = next(
            item
            for item in pending_event.get("execution_evidence") or []
            if str(item.get("execution_id")) == execution_id
        )
        elapsed_ms = Decimal(str(evidence.get("elapsed_ms")))
        remaining_ms = Decimal(str(evidence.get("remaining_deadline_ms")))
        require(elapsed_ms < Decimal("30000"), "pending observation missed deadline")
        require(remaining_ms > 0, "pending observation has no bounded time left")
        require(
            evidence.get("protection_state")
            in {
                "ENTRY_ACKNOWLEDGED",
                "ENTRY_PARTIALLY_FILLED",
                "PROTECTION_PENDING",
            },
            "first exact fill observation was not protection pending",
        )

        close = post_json(base_url, f"/demo/canary/{execution_id}/close")
        require(close.get("reduce_only") is True, "close was not reduce-only")
        closed = wait_execution(
            base_url, execution_id, TERMINAL_STATES, 120
        )
        post_json(base_url, "/demo/reconcile")
        post_json(base_url, "/v2/stop-new-entries")
        post_json(base_url, "/demo/reconcile")
        final_v2 = wait_phase(base_url, "FINISHED", 60)
        final_demo = get_json(base_url, "/demo/status")
        residual_result = wait_for_authoritative_final_orders(
            config=config,
            execution_id=execution_id,
            cached_open_order_count=int(
                final_demo.get("bot_owned_open_orders") or 0
            ),
        )
        final_demo = get_json(base_url, "/demo/status")

        monitor_code = monitor.wait(timeout=90)
        require(monitor_code == 0, f"monitor exited {monitor_code}")
        monitor_result = json.loads(
            (monitor_dir / "monitor-result.json").read_text(encoding="utf-8")
        )
        events = read_jsonl(monitor_dir / "monitor-events.jsonl")
        require(
            not any(item.get("event") == "MONITOR_FAIL_FAST" for item in events),
            "monitor emitted false fail-fast",
        )
        require(
            monitor_result.get("result") == "OBSERVATION_COMPLETE",
            f"monitor result={monitor_result.get('result')}",
        )

        fill_at = parse_utc(closed.get("exchange_fill_at"))
        protection_at = parse_utc(closed.get("protection_confirmed_at"))
        attachment_ms = decimal_duration_ms(fill_at, protection_at)
        require(attachment_ms <= Decimal("30000"), "protection deadline exceeded")
        require(
            closed.get("accounting_status") == "FINAL"
            and decimal_text(closed.get("realized_exchange_pnl"))
            == decimal_text(closed.get("authoritative_closed_pnl")),
            "durable/exchange PnL mismatch",
        )

        repository = PersistenceRepository(
            config.database_url, create_schema=False
        )
        terminal_events = sum(
            item.get("event_type") == "DEMO_CLOSE_TERMINALIZED"
            for item in repository.load_demo_execution_events(execution_id)
        )
        portfolio = repository.load_v2_portfolio_state() or {}
        ledger = int(execution_id in (portfolio.get("realized_events") or {}))
        releases = sum(
            item.get("run_id") == run_id and item.get("state") == "RELEASED"
            for item in portfolio.get("reservations") or []
        )
        repository.dispose()
        require(terminal_events == 1, f"terminal events={terminal_events}")
        require(ledger == 1, f"ledger finalizations={ledger}")
        require(releases == 1, f"capacity releases={releases}")
        require(
            int(final_demo.get("bot_owned_open_positions") or 0) == 0,
            "final position open",
        )
        require(
            int(final_demo.get("confirmed_unrelated_orders") or 0) == 0,
            "unrelated order",
        )
        require(not final_demo.get("kill_switch_active"), "kill switch active")
        require(
            int(final_v2.get("total_cycle_failures") or 0) == 0,
            "cycle failure emitted",
        )
        require(
            final_v2.get("persistence_status") == "OK",
            "persistence failure",
        )
        report.update({
            "result": "PASS",
            "finished_at": datetime.now(timezone.utc).isoformat(),
            "protection_pending_event": pending_event,
            "protection_attachment_elapsed_ms": str(attachment_ms),
            "protection_deadline_ms": "30000",
            "state": closed.get("state"),
            "terminal_events": terminal_events,
            "ledger_finalizations": ledger,
            "capacity_releases": releases,
            "cycle_failures": 0,
            "persistence_failures": 0,
            "final_phase": final_v2.get("run_phase"),
            "final_positions": 0,
            "final_orders": 0,
            "unresolved_executions": 0,
            "monitor": monitor_result,
            **residual_result,
        })
        safe_to_terminate = True
        print("PROTECTION_PENDING: PASS")
        print("AUTHORITATIVE TP/SL: PASS")
        print("EXACT TERMINAL/LEDGER/CAPACITY: PASS")
        print("FINAL DEMO STATE FLAT: PASS")
        print("OVERALL: PASS")
        return 0
    except Exception as exc:
        if isinstance(exc, ResidualOrderVerificationError):
            report.update(exc.result)
        report["failure"] = (
            f"{type(exc).__name__}: {' '.join(str(exc).split())[:500]}"
        )
        report["finished_at"] = datetime.now(timezone.utc).isoformat()
        if execution_id and child and child.poll() is None:
            try:
                current = wait_execution(
                    base_url,
                    execution_id,
                    TERMINAL_STATES
                    | {
                        "DEMO_POSITION_OPEN",
                        "DEMO_PROTECTION_PENDING",
                        "DEMO_FULLY_FILLED",
                    },
                    5,
                )
                if current.get("state") not in TERMINAL_STATES:
                    post_json(
                        base_url,
                        f"/demo/canary/{execution_id}/close",
                    )
                    wait_execution(
                        base_url, execution_id, TERMINAL_STATES, 120
                    )
                    report["emergency_cleanup"] = True
                post_json(base_url, "/demo/reconcile")
                demo = get_json(base_url, "/demo/status")
                safe_to_terminate = bool(
                    int(demo.get("bot_owned_open_positions") or 0) == 0
                    and int(demo.get("bot_owned_open_orders") or 0) == 0
                )
                report["failure_cleanup_flat"] = safe_to_terminate
            except Exception as cleanup_exc:
                report["failure_cleanup_error"] = (
                    f"{type(cleanup_exc).__name__}: "
                    f"{' '.join(str(cleanup_exc).split())[:300]}"
                )
        print(report["failure"], file=sys.stderr)
        return 2
    finally:
        report_path.write_text(
            json.dumps(report, indent=2, default=str), encoding="utf-8"
        )
        if monitor and monitor.poll() is None:
            terminate(monitor)
        if child and child.poll() is None and safe_to_terminate:
            terminate(child)


def wait_for_pending_event(
    path: Path, execution_id: str, *, timeout_seconds: float,
) -> dict[str, Any]:
    deadline = time.monotonic() + timeout_seconds
    while time.monotonic() < deadline:
        for event in read_jsonl(path):
            if (
                event.get("event") == "MONITOR_PROTECTION_PENDING"
                and execution_id in (event.get("execution_ids") or [])
            ):
                return event
            if event.get("event") == "MONITOR_FAIL_FAST":
                raise RuntimeError(
                    f"monitor fail-fast: {event.get('reason')}"
                )
        time.sleep(0.2)
    raise TimeoutError("monitor did not observe PROTECTION_PENDING")


def read_jsonl(path: Path) -> list[dict[str, Any]]:
    if not path.exists():
        return []
    values: list[dict[str, Any]] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        if line.strip():
            values.append(json.loads(line))
    return values


def parse_utc(value: Any) -> datetime:
    require(value is not None, "required timestamp is missing")
    parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    return (
        parsed.replace(tzinfo=timezone.utc)
        if parsed.tzinfo is None else parsed.astimezone(timezone.utc)
    )


def decimal_duration_ms(start: datetime, end: datetime) -> Decimal:
    return Decimal(str((end - start).total_seconds())) * Decimal("1000")


class ResidualOrderVerificationError(RuntimeError):
    def __init__(self, code: str, result: dict[str, Any]) -> None:
        super().__init__(code)
        self.code = code
        self.result = result


def wait_for_authoritative_final_orders(
    *,
    config: DemoDiagnosticsConfig | None = None,
    execution_id: str,
    cached_open_order_count: int,
    repository: Any | None = None,
    client: Any | None = None,
    timeout_seconds: float = 30.0,
    poll_seconds: float = 0.75,
    monotonic: Any = time.monotonic,
    sleeper: Any = time.sleep,
    utcnow: Any = lambda: datetime.now(timezone.utc),
) -> dict[str, Any]:
    """Bounded exact-ID verification for post-close protection residuals."""
    owns_repository = repository is None
    if repository is None:
        require(config is not None, "diagnostics config is required")
        repository = PersistenceRepository(
            config.database_url, create_schema=False
        )
    if client is None:
        require(config is not None, "diagnostics config is required")
        client = ReadOnlyBybitDemoClient(
            config.api_key,
            config.api_secret,
            base_url=config.rest_url,
            timeout_seconds=min(5.0, timeout_seconds),
        )
    started = monotonic()
    deadline = started + timeout_seconds
    attempts = 0
    timeline: list[dict[str, Any]] = []
    initial_count: int | None = None
    initial_exact = 0
    first_residual_at: datetime | None = None
    position_flat_at: datetime | None = None
    terminal_at: str | None = None
    try:
        while True:
            attempts += 1
            observed = utcnow()
            records = repository.load_demo_executions()
            matches = [
                item for item in records if str(item.id) == execution_id
            ]
            if len(matches) != 1:
                raise ResidualOrderVerificationError(
                    "OWNERSHIP_CONFLICT",
                    _residual_result(
                        execution_id=execution_id,
                        cached_count=cached_open_order_count,
                        initial_count=initial_count,
                        initial_exact=initial_exact,
                        attempts=attempts,
                        started=started,
                        monotonic=monotonic,
                        timeline=timeline,
                        final_orders=-1,
                        result="OWNERSHIP_CONFLICT",
                    ),
                )
            execution = matches[0]
            terminal_at = str(
                execution.terminalization_completed_at
                or execution.closed_at
                or execution.updated_at
            )
            positions = [
                item for item in client.get_usdt_positions()
                if _positive_decimal(item.get("size"))
            ]
            if positions:
                timeline.append({
                    "observed_at": observed.isoformat(),
                    "positions": [_position_summary(item) for item in positions],
                    "classification": "REMOTE_POSITION_REOPENED",
                })
                raise ResidualOrderVerificationError(
                    "REMOTE_POSITION_REOPENED",
                    _residual_result(
                        execution_id=execution_id,
                        cached_count=cached_open_order_count,
                        initial_count=initial_count,
                        initial_exact=initial_exact,
                        attempts=attempts,
                        started=started,
                        monotonic=monotonic,
                        timeline=timeline,
                        final_orders=-1,
                        result="REMOTE_POSITION_REOPENED",
                    ),
                )
            position_flat_at = position_flat_at or observed
            orders = client.get_open_orders()
            initial_count = len(orders) if initial_count is None else initial_count
            classification = classify_demo_order_ownership(
                orders,
                records,
                positions,
                now=observed,
                terminal_residual_timeout_seconds=timeout_seconds,
            )
            conflicts = list(classification["ownership_conflicts"])
            unrelated = list(classification["unrelated_external"])
            exact_owned = _validate_exact_residual_orders(
                orders=orders,
                execution=execution,
                conflicts=conflicts,
                unrelated=unrelated,
            )
            if initial_count == len(orders) and attempts == 1:
                initial_exact = len(exact_owned)
            timeline.append({
                "observed_at": observed.isoformat(),
                "authoritative_open_order_count": len(orders),
                "orders": [
                    _order_summary(item, execution) for item in orders
                ],
                "exact_owned_pending_cancel": len(exact_owned),
                "unrelated": len(unrelated),
                "conflicts": len(conflicts),
                "classification": (
                    "BOT_OWNED_PENDING_CANCEL" if exact_owned else "CLEAR"
                ),
            })
            if conflicts:
                raise ResidualOrderVerificationError(
                    "OWNERSHIP_CONFLICT",
                    _residual_result(
                        execution_id=execution_id,
                        cached_count=cached_open_order_count,
                        initial_count=initial_count,
                        initial_exact=initial_exact,
                        attempts=attempts,
                        started=started,
                        monotonic=monotonic,
                        timeline=timeline,
                        final_orders=len(orders),
                        result="OWNERSHIP_CONFLICT",
                    ),
                )
            if unrelated:
                raise ResidualOrderVerificationError(
                    "UNRELATED_EXTERNAL",
                    _residual_result(
                        execution_id=execution_id,
                        cached_count=cached_open_order_count,
                        initial_count=initial_count,
                        initial_exact=initial_exact,
                        attempts=attempts,
                        started=started,
                        monotonic=monotonic,
                        timeline=timeline,
                        final_orders=len(orders),
                        result="UNRELATED_EXTERNAL",
                    ),
                )
            if not orders:
                result = _residual_result(
                    execution_id=execution_id,
                    cached_count=cached_open_order_count,
                    initial_count=initial_count,
                    initial_exact=initial_exact,
                    attempts=attempts,
                    started=started,
                    monotonic=monotonic,
                    timeline=timeline,
                    final_orders=0,
                    result="PASS",
                )
                result.update({
                    "terminalization_timestamp": terminal_at,
                    "authoritative_position_flat_timestamp": (
                        position_flat_at.isoformat()
                        if position_flat_at else None
                    ),
                    "first_observed_residual_timestamp": (
                        first_residual_at.isoformat()
                        if first_residual_at else None
                    ),
                    "residual_deactivation_timestamp": observed.isoformat(),
                })
                return result
            first_residual_at = first_residual_at or observed
            remaining = deadline - monotonic()
            if remaining <= 0:
                raise ResidualOrderVerificationError(
                    "BOT_OWNED_TERMINAL_RESIDUAL_TIMEOUT",
                    _residual_result(
                        execution_id=execution_id,
                        cached_count=cached_open_order_count,
                        initial_count=initial_count,
                        initial_exact=initial_exact,
                        attempts=attempts,
                        started=started,
                        monotonic=monotonic,
                        timeline=timeline,
                        final_orders=len(orders),
                        result="BOT_OWNED_TERMINAL_RESIDUAL_TIMEOUT",
                    ),
                )
            sleeper(min(poll_seconds, remaining))
    finally:
        if owns_repository and hasattr(repository, "dispose"):
            repository.dispose()


def _validate_exact_residual_orders(
    *,
    orders: list[dict[str, Any]],
    execution: Any,
    conflicts: list[dict[str, Any]],
    unrelated: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    expected = {
        str(value): role
        for value, role in (
            (execution.tp_order_id, "TakeProfit"),
            (execution.sl_order_id, "StopLoss"),
        )
        if value
    }
    seen: set[str] = set()
    exact: list[dict[str, Any]] = []
    expected_side = "SELL" if execution.side.value == "BUY" else "BUY"
    for order in orders:
        order_id = str(order.get("orderId") or "")
        if not order_id or order_id in seen:
            conflicts.append(order)
            continue
        seen.add(order_id)
        role = expected.get(order_id)
        if role is None:
            if order not in unrelated and order not in conflicts:
                unrelated.append(order)
            continue
        if (
            str(order.get("symbol") or "") != execution.symbol.value
            or str(order.get("side") or "").upper() != expected_side
            or not _api_bool(order.get("reduceOnly"))
            or not _api_bool(order.get("closeOnTrigger"))
            or str(order.get("stopOrderType") or "") != role
            or int(order.get("positionIdx") or 0)
            != int(execution.protection_position_idx or 0)
        ):
            conflicts.append(order)
            continue
        exact.append(order)
    return exact


def _residual_result(
    *,
    execution_id: str,
    cached_count: int,
    initial_count: int | None,
    initial_exact: int,
    attempts: int,
    started: float,
    monotonic: Any,
    timeline: list[dict[str, Any]],
    final_orders: int,
    result: str,
) -> dict[str, Any]:
    elapsed_ms = max(0, int(round((monotonic() - started) * 1000)))
    last = timeline[-1] if timeline else {}
    return {
        "residual_execution_id": execution_id,
        "residual_orders_initial": initial_count or 0,
        "residual_orders_exact_owned": initial_exact,
        "residual_orders_unrelated": int(last.get("unrelated") or 0),
        "residual_orders_conflicting": int(last.get("conflicts") or 0),
        "residual_wait_attempts": attempts,
        "residual_wait_ms": elapsed_ms,
        "cached_open_orders_initial": cached_count,
        "final_authoritative_open_orders": final_orders,
        "residual_result": result,
        "residual_cancellation_timeline": timeline,
    }


def _api_bool(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    return str(value or "").strip().casefold() in {"1", "true", "yes"}


def _positive_decimal(value: Any) -> bool:
    try:
        return Decimal(str(value or "0")) > 0
    except Exception:
        return True


def _order_summary(order: dict[str, Any], execution: Any) -> dict[str, Any]:
    order_id = str(order.get("orderId") or "")
    role = (
        "TakeProfit" if order_id == str(execution.tp_order_id or "")
        else "StopLoss" if order_id == str(execution.sl_order_id or "")
        else "UNKNOWN"
    )
    return {
        "order_id": order_id,
        "order_link_id": str(order.get("orderLinkId") or ""),
        "symbol": str(order.get("symbol") or ""),
        "side": str(order.get("side") or ""),
        "status": str(order.get("orderStatus") or ""),
        "cancel_type": str(order.get("cancelType") or ""),
        "stop_order_type": str(order.get("stopOrderType") or ""),
        "classification": role,
        "reduce_only": _api_bool(order.get("reduceOnly")),
        "close_on_trigger": _api_bool(order.get("closeOnTrigger")),
    }


def _position_summary(position: dict[str, Any]) -> dict[str, Any]:
    return {
        "symbol": str(position.get("symbol") or ""),
        "side": str(position.get("side") or ""),
        "size": str(position.get("size") or ""),
        "position_idx": int(position.get("positionIdx") or 0),
    }


if __name__ == "__main__":
    raise SystemExit(main())
