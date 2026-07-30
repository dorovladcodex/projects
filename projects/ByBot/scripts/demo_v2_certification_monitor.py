from __future__ import annotations

import argparse
from concurrent.futures import ThreadPoolExecutor
import ctypes
from datetime import datetime, timezone
from decimal import Decimal
import json
import os
from pathlib import Path
import socket
import sys
import time
from typing import Any
from urllib.request import Request, urlopen
from urllib.parse import urlparse

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.bybit.demo_diagnostics import (  # noqa: E402
    DemoDiagnosticsConfig,
    ReadOnlyBybitDemoClient,
)
from app.bybit.demo_execution_recovery import (  # noqa: E402
    diagnose_demo_execution,
    exact_close_reconciliation_blockers,
)
from app.db.persistence import PersistenceRepository  # noqa: E402
from app.v2.certification_monitor import (  # noqa: E402
    CertificationMonitorHealth,
    ExecutionFallbackEvidence,
    ProtectionEstablishmentState,
    StatusFallbackEvidence,
)


TERMINAL_STATES = {
    "DEMO_CLOSED",
    "DEMO_CLOSED_EXTERNALLY",
    "DEMO_CLOSED_AFTER_FAILURE",
    "DEMO_CLOSED_AFTER_INTERRUPTION",
    "DEMO_ORDER_CANCELLED",
    "DEMO_NOT_SUBMITTED",
    "DEMO_FAILED_FLAT_VERIFIED",
}


class DurableTerminalizationLag(RuntimeError):
    pass


class ProtectionEstablishmentCheck(RuntimeError):
    pass


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Read-only resilient monitor for one managed Demo V2 run."
    )
    parser.add_argument("--run-id", required=True)
    parser.add_argument("--runner-pid", type=int, required=True)
    parser.add_argument("--uvicorn-pid", type=int)
    parser.add_argument("--base-url", default="http://127.0.0.1:8137")
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument("--hard-timeout-seconds", type=float, default=90.0)
    parser.add_argument("--idle-poll-seconds", type=float, default=300.0)
    parser.add_argument("--active-poll-seconds", type=float, default=90.0)
    parser.add_argument("--drain-poll-seconds", type=float, default=45.0)
    parser.add_argument("--retry-poll-seconds", type=float, default=5.0)
    parser.add_argument(
        "--entry-attribution-poll-seconds", type=float, default=0.75
    )
    parser.add_argument("--protection-poll-seconds", type=float, default=2.0)
    parser.add_argument("--protection-timeout-seconds", type=float, default=30.0)
    parser.add_argument("--inject-status-timeouts", type=int, default=0)
    parser.add_argument("--max-polls", type=int)
    args = parser.parse_args()

    args.output_dir.mkdir(parents=True, exist_ok=True)
    samples_path = args.output_dir / "monitor.jsonl"
    events_path = args.output_dir / "monitor-events.jsonl"
    result_path = args.output_dir / "monitor-result.json"
    config = DemoDiagnosticsConfig.load(env_path=ROOT / ".env")
    repository = PersistenceRepository(config.database_url, create_schema=False)
    read_client = ReadOnlyBybitDemoClient(
        config.api_key, config.api_secret, base_url=config.rest_url
    )
    read_client.verify()
    health = CertificationMonitorHealth(
        hard_timeout_seconds=args.hard_timeout_seconds,
        protection_timeout_seconds=args.protection_timeout_seconds,
    )
    monitor_port = urlparse(args.base_url).port or 8137
    previous: dict[str, Any] | None = None
    last_phase: str | None = None
    polls = 0
    deep_events = 0

    while True:
        now = datetime.now(timezone.utc)
        runner_alive = process_alive(args.runner_pid)
        uvicorn_alive = (
            process_alive(args.uvicorn_pid)
            if args.uvicorn_pid is not None else port_listening(monitor_port)
        )
        listener = port_listening(monitor_port)
        try:
            if polls < args.inject_status_timeouts:
                raise TimeoutError("deterministic certification status timeout")
            v2 = get_json(args.base_url, "/v2/status")
            demo = get_json(args.base_url, "/demo/status")
            execution_payload = get_json(args.base_url, "/demo/executions")
            executions = [
                item
                for item in execution_payload.get("executions", [])
                if item.get("run_id") == args.run_id
            ]
            status_active = [
                item for item in executions
                if str(item.get("state") or "") not in TERMINAL_STATES
            ]
            pending_protection = [
                item for item in status_active
                if not bool(item.get("protection_confirmed"))
                and str(item.get("state") or "") not in {
                    "DEMO_CLOSING",
                    "DEMO_RECONCILIATION_REQUIRED",
                }
            ]
            if pending_protection:
                raise ProtectionEstablishmentCheck(
                    "active entry requires authoritative protection refresh"
                )
            if (
                status_active
                and int(demo.get("bot_owned_open_positions") or 0) == 0
            ):
                raise DurableTerminalizationLag(
                    "durable execution remains active after cached remote-flat state"
                )
            previous_health_state = health.state
            health.record_status_success(now=now)
            if previous_health_state.value != "HEALTHY":
                bounded_post(args.base_url, "/v2/supervisor/resume")
        except Exception as exc:
            persistence_ok, active, durable_runtime = durable_fallback(
                repository, args.run_id
            )
            fallback = collect_status_fallback_evidence(
                config=config,
                repository=repository,
                client=read_client,
                active=active,
                runner_alive=runner_alive,
                uvicorn_alive=uvicorn_alive,
                listener=listener,
                persistence_ok=persistence_ok,
                kill_switch_active=bool(
                    (durable_runtime.get("portfolio_risk") or {}).get(
                        "kill_switch_active"
                    )
                ),
                protection_timeout_seconds=args.protection_timeout_seconds,
            )
            decision = health.record_status_failure(
                now=now,
                evidence=fallback,
                error=f"{type(exc).__name__}: {exc}",
            )
            pause_result = bounded_post(
                args.base_url, "/v2/supervisor/pause"
            )
            reconciliation_result = None
            if decision.request_reconciliation:
                reconciliation_result = bounded_post(
                    args.base_url, "/demo/reconcile"
                )
            write_events(events_path, health)
            write_jsonl(samples_path, {
                "timestamp": now.isoformat(),
                "phase": last_phase,
                "runner_alive": runner_alive,
                "uvicorn_alive": uvicorn_alive,
                "port_listening": listener,
                "monitor_state": health.state.value,
                "status_available": False,
                "persistence_fallback_ok": persistence_ok,
                "durable_active_execution_ids": [
                    str(item.id) for item in active
                ],
                "remote_positions": sum(
                    item.remote_position_open for item in fallback.executions
                ),
                "exact_close_evidence": sum(
                    item.exact_close_evidence for item in fallback.executions
                ),
                "close_evidence_pending": sum(
                    item.close_evidence_pending for item in fallback.executions
                ),
                "unrelated_positions": fallback.unrelated_positions,
                "unrelated_orders": fallback.unrelated_orders,
                "ownership_conflicts": fallback.ownership_conflicts,
                "protection_pending": [
                    {
                        "execution_id": item.execution_id,
                        "state": (
                            item.protection_state.value
                            if item.protection_state else None
                        ),
                        "fill_at": item.fill_at,
                        "protection_started_at": item.protection_started_at,
                        "protection_requested_at": item.protection_requested_at,
                        "protection_rest_confirmed_at": (
                            item.protection_rest_confirmed_at
                        ),
                        "elapsed_ms": item.protection_elapsed_ms,
                        "remaining_deadline_ms": (
                            item.protection_remaining_deadline_ms
                        ),
                        "position_size": item.authoritative_position_size,
                        "take_profit": item.authoritative_take_profit,
                        "stop_loss": item.authoritative_stop_loss,
                        "protection_order_ids": list(item.protection_order_ids),
                        "entry_attribution_source": (
                            item.entry_attribution_source
                        ),
                        "entry_attribution_lookup_attempts": (
                            item.entry_attribution_lookup_attempts
                        ),
                        "realtime_order_id": item.realtime_order_id,
                        "realtime_order_link_id": item.realtime_order_link_id,
                        "realtime_order_status": item.realtime_order_status,
                        "realtime_order_quantity": item.realtime_order_quantity,
                        "realtime_cumulative_quantity": (
                            item.realtime_cumulative_quantity
                        ),
                        "realtime_identity_match": (
                            item.realtime_identity_match
                        ),
                    }
                    for item in fallback.executions
                    if item.protection_state in {
                        ProtectionEstablishmentState.EXACT_ENTRY_ATTRIBUTION_PENDING,
                        ProtectionEstablishmentState.ENTRY_ACKNOWLEDGED,
                        ProtectionEstablishmentState.ENTRY_PARTIALLY_FILLED,
                        ProtectionEstablishmentState.PROTECTION_PENDING,
                        ProtectionEstablishmentState.PROTECTED,
                    }
                ],
                "pause_result": pause_result,
                "reconciliation_requested": decision.request_reconciliation,
                "reconciliation_result": reconciliation_result,
                "keep_reconciler_alive": decision.keep_reconciler_alive,
                "shutdown_ready": decision.shutdown_ready,
            })
            polls += 1
            if decision.escalate:
                write_json(result_path, {
                    "result": "FAIL",
                    "reason": decision.reason,
                    "polls": polls,
                    "monitor_health": health.snapshot(now=now),
                })
                return 2
            delay = (
                args.entry_attribution_poll_seconds
                if decision.state.value == "EXACT_ENTRY_ATTRIBUTION_PENDING"
                else (
                    args.protection_poll_seconds
                    if decision.state.value == "PROTECTION_PENDING"
                    else args.retry_poll_seconds
                )
            )
            time.sleep(delay)
            continue

        last_phase = str(v2.get("run_phase") or "")
        active = [
            item for item in executions
            if str(item.get("state") or "") not in TERMINAL_STATES
        ]
        completed = [
            item for item in executions
            if str(item.get("state") or "") in TERMINAL_STATES
        ]
        for item in completed:
            started_at = item.get("terminalization_started_at")
            completed_at = item.get("terminalization_completed_at")
            execution_id = str(item.get("id") or "")
            if execution_id and started_at and completed_at:
                health.record_resolved_terminalization_evidence(
                    now=now,
                    execution_id=execution_id,
                    started_at=str(started_at),
                    completed_at=str(completed_at),
                )
        blockers = runtime_blockers(v2, demo, completed)
        snapshot = {
            "timestamp": now.isoformat(),
            "phase": last_phase,
            "runner_alive": runner_alive,
            "uvicorn_alive": uvicorn_alive,
            "port_listening": listener,
            "monitor_state": health.state.value,
            "active_executions": len(active),
            "completed_executions": len(completed),
            "remote_positions": int(demo.get("bot_owned_open_positions") or 0),
            "remote_orders": int(demo.get("bot_owned_open_orders") or 0),
            "confirmed_unrelated_orders": int(
                demo.get("confirmed_unrelated_orders") or 0
            ),
            "cycle_failures": int(v2.get("total_cycle_failures") or 0),
            "persistence_status": v2.get("persistence_status"),
            "kill_switch_active": bool(v2.get("kill_switch_active")),
            "submitted": int(
                (v2.get("signal_metrics") or {}).get("orders_submitted") or 0
            ),
            "filled": int(
                (v2.get("signal_metrics") or {}).get("orders_filled") or 0
            ),
            "durable_pnl": decimal_sum(
                item.get("realized_exchange_pnl") for item in completed
            ),
            "authoritative_pnl": decimal_sum(
                item.get("authoritative_closed_pnl") for item in completed
            ),
            "blockers": blockers,
        }
        changed = {
            key: value
            for key, value in snapshot.items()
            if key != "timestamp"
            and (previous is None or previous.get(key) != value)
        }
        write_jsonl(samples_path, {
            "timestamp": snapshot["timestamp"],
            "phase": snapshot["phase"],
            "runner_alive": runner_alive,
            "uvicorn_alive": uvicorn_alive,
            "active_executions": len(active),
            "completed_executions": len(completed),
            "remote_positions": snapshot["remote_positions"],
            "remote_orders": snapshot["remote_orders"],
            "cycle_failures": snapshot["cycle_failures"],
            "persistence_status": snapshot["persistence_status"],
            "durable_pnl": snapshot["durable_pnl"],
            "authoritative_pnl": snapshot["authoritative_pnl"],
            "changed": changed,
        })
        polls += 1
        if changed:
            write_jsonl(events_path, snapshot)
            deep_events += 1
        previous = snapshot
        write_events(events_path, health)
        if blockers:
            write_json(result_path, {
                "result": "FAIL",
                "blockers": blockers,
                "polls": polls,
                "deep_events": deep_events,
                "final": snapshot,
                "monitor_health": health.snapshot(now=now),
            })
            return 2
        if args.max_polls is not None and polls >= args.max_polls:
            write_json(result_path, {
                "result": "OBSERVATION_COMPLETE",
                "polls": polls,
                "deep_events": deep_events,
                "final": snapshot,
                "monitor_health": health.snapshot(now=now),
            })
            return 0
        if last_phase == "FINISHED" and not runner_alive:
            write_json(result_path, {
                "result": "FINISHED",
                "polls": polls,
                "deep_events": deep_events,
                "final": snapshot,
                "monitor_health": health.snapshot(now=now),
            })
            return 0
        delay = (
            args.drain_poll_seconds
            if last_phase in {"DRAINING", "RECONCILING"}
            else args.active_poll_seconds if active else args.idle_poll_seconds
        )
        time.sleep(delay)


def get_json(base_url: str, path: str) -> dict[str, Any]:
    with urlopen(base_url.rstrip("/") + path, timeout=15) as response:
        payload = json.loads(response.read().decode("utf-8"))
    if not isinstance(payload, dict):
        raise ValueError("status endpoint returned a non-object")
    return payload


def bounded_post(base_url: str, path: str) -> dict[str, Any]:
    try:
        request = Request(
            base_url.rstrip("/") + path,
            method="POST",
            headers={"Content-Type": "application/json"},
            data=b"{}",
        )
        with urlopen(request, timeout=15) as response:
            payload = json.loads(response.read().decode("utf-8"))
        return {
            "ok": True,
            "status": int(response.status),
            "payload": payload if isinstance(payload, dict) else {},
        }
    except Exception as exc:
        return {
            "ok": False,
            "error": f"{type(exc).__name__}: {' '.join(str(exc).split())[:240]}",
        }


def durable_fallback(
    repository: PersistenceRepository, run_id: str
) -> tuple[bool, list[Any], dict[str, Any]]:
    if not repository.available:
        return False, [], {}
    runtime = repository.load_v2_run_runtime(run_id)
    records = [
        item
        for item in repository.load_demo_executions()
        if item.run_id == run_id and item.state.value not in TERMINAL_STATES
    ]
    return bool(runtime), records, runtime


def collect_status_fallback_evidence(
    *,
    config: DemoDiagnosticsConfig,
    repository: PersistenceRepository,
    client: ReadOnlyBybitDemoClient,
    active: list[Any],
    runner_alive: bool,
    uvicorn_alive: bool,
    listener: bool,
    persistence_ok: bool,
    kill_switch_active: bool,
    observed_at: datetime | None = None,
    protection_timeout_seconds: float = 30.0,
) -> StatusFallbackEvidence:
    """Collect exact durable/REST evidence without symbol-only attribution."""
    symbols = {record.symbol.value: record.symbol for record in active}
    observed_at = observed_at or datetime.now(timezone.utc)
    try:
        with ThreadPoolExecutor(
            max_workers=max(2, 2 + len(symbols) * 2 + len(active))
        ) as pool:
            positions_future = pool.submit(client.get_usdt_positions)
            orders_future = pool.submit(client.get_open_orders)
            execution_futures = {
                symbol: pool.submit(client.get_executions, symbol_value)
                for symbol, symbol_value in symbols.items()
            }
            history_futures = {
                symbol: pool.submit(client.get_order_history, symbol_value)
                for symbol, symbol_value in symbols.items()
            }
            realtime_futures = {
                str(record.id): pool.submit(
                    client.get_realtime_order,
                    record.symbol,
                    order_id=(
                        str(record.order_id) if record.order_id else None
                    ),
                    order_link_id=(
                        str(record.order_link_id)
                        if record.order_link_id else None
                    ),
                )
                for record in active
                if hasattr(client, "get_realtime_order")
                and (record.order_id or record.order_link_id)
            }
            positions = [
                item
                for item in positions_future.result()
                if _decimal(item.get("size")) > 0
            ]
            open_orders = orders_future.result()
            executions_by_symbol = {
                symbol: future.result()
                for symbol, future in execution_futures.items()
            }
            order_history_by_symbol = {
                symbol: future.result()
                for symbol, future in history_futures.items()
            }
            realtime_by_execution = {
                execution_id: future.result()
                for execution_id, future in realtime_futures.items()
            }
    except Exception as exc:
        return StatusFallbackEvidence(
            runner_alive=runner_alive,
            uvicorn_alive=uvicorn_alive,
            port_listening=listener,
            persistence_ok=persistence_ok,
            kill_switch_active=kill_switch_active,
            executions=tuple(
                ExecutionFallbackEvidence(
                    execution_id=str(record.id),
                    symbol=record.symbol.value,
                    durable_state=record.state.value,
                    remote_position_open=False,
                    protection_confirmed=False,
                    remote_flat=False,
                    close_evidence_pending=True,
                    evidence_error=(
                        f"authoritative fallback unavailable: "
                        f"{type(exc).__name__}"
                    ),
                )
                for record in active
            ),
            authoritative_check_complete=False,
        )

    all_records = repository.load_demo_executions()
    owners_by_order_id: dict[str, list[str]] = {}
    owners_by_link_id: dict[str, list[str]] = {}
    for record in all_records:
        for order_id in {
            record.order_id,
            record.close_order_id,
            record.tp_order_id,
            record.sl_order_id,
            *(fill.order_id for fill in [*record.fills, *record.close_fills]),
        }:
            if order_id:
                owners_by_order_id.setdefault(str(order_id), []).append(
                    str(record.id)
                )
        for link_id in {record.order_link_id, record.close_order_link_id}:
            if link_id:
                owners_by_link_id.setdefault(str(link_id), []).append(
                    str(record.id)
                )
    conflicts = {
        identity
        for identity, owners in {
            **owners_by_order_id,
            **owners_by_link_id,
        }.items()
        if len(set(owners)) > 1
    }
    attributed_orders: dict[str, list[dict[str, Any]]] = {
        str(record.id): [] for record in active
    }
    unrelated_orders = 0
    for order in open_orders:
        order_id = str(order.get("orderId") or "")
        link_id = str(order.get("orderLinkId") or "")
        owners = set(owners_by_order_id.get(order_id, []))
        owners.update(owners_by_link_id.get(link_id, []))
        if len(owners) == 1:
            attributed_orders[next(iter(owners))].append(order)
        elif len(owners) > 1:
            conflicts.add(order_id or link_id or "empty-order-identity")
        else:
            unrelated_orders += 1

    active_by_symbol: dict[str, list[Any]] = {}
    for record in active:
        active_by_symbol.setdefault(record.symbol.value, []).append(record)
    unrelated_positions = sum(
        1
        for position in positions
        if len(active_by_symbol.get(str(position.get("symbol") or ""), [])) != 1
    )

    execution_evidence: list[ExecutionFallbackEvidence] = []
    for record in active:
        matching_positions = [
            position for position in positions
            if str(position.get("symbol") or "") == record.symbol.value
        ]
        record_conflict = (
            len(active_by_symbol.get(record.symbol.value, [])) != 1
            or len(matching_positions) > 1
            or any(
                str(identity) in conflicts
                for identity in (
                    record.order_id,
                    record.close_order_id,
                    record.tp_order_id,
                    record.sl_order_id,
                    record.order_link_id,
                    record.close_order_link_id,
                )
                if identity
            )
        )
        if matching_positions:
            position = matching_positions[0]
            history = order_history_by_symbol[record.symbol.value]
            executions = executions_by_symbol[record.symbol.value]
            entry_orders = [
                item for item in history
                if _matches_entry_identity(item, record)
                and str(item.get("symbol") or "") == record.symbol.value
                and str(item.get("side") or "").upper() == record.side.value
            ]
            entry_fills = [
                item for item in executions
                if _matches_entry_identity(item, record)
                and str(item.get("symbol") or "") == record.symbol.value
                and str(item.get("side") or "").upper() == record.side.value
            ]
            realtime_rows = realtime_by_execution.get(str(record.id), [])
            realtime_order, realtime_error, realtime_conflict = (
                _evaluate_exact_realtime_entry(
                    rows=realtime_rows,
                    record=record,
                    position=position,
                    observed_at=observed_at,
                    globally_conflicting_identities=conflicts,
                )
            )
            record_conflict = record_conflict or realtime_conflict
            entry_fill_order_ids = {
                str(item.get("orderId") or "") for item in entry_fills
                if item.get("orderId")
            }
            entry_fill_link_ids = {
                str(item.get("orderLinkId") or "") for item in entry_fills
                if item.get("orderLinkId")
            }
            entry_order_ids = {
                str(item.get("orderId") or "") for item in entry_orders
                if item.get("orderId")
            }
            entry_order_link_ids = {
                str(item.get("orderLinkId") or "") for item in entry_orders
                if item.get("orderLinkId")
            }
            entry_fill_qty = sum(
                (_decimal(item.get("execQty")) for item in entry_fills),
                Decimal("0"),
            )
            if entry_fill_qty <= 0 and entry_orders:
                entry_fill_qty = max(
                    (
                        _decimal(item.get("cumExecQty"))
                        for item in entry_orders
                    ),
                    default=Decimal("0"),
                )
            if entry_fill_qty <= 0 and realtime_order is not None:
                entry_fill_qty = _decimal(
                    realtime_order.get("cumExecQty")
                )
            remote_size = _decimal(position.get("size"))
            expected_order_id = str(record.order_id or "")
            expected_link_id = str(record.order_link_id or "")
            exact_fill_identity = bool(
                (
                    expected_order_id
                    and entry_fill_order_ids == {expected_order_id}
                    and expected_order_id not in conflicts
                )
                or (
                    expected_link_id
                    and entry_fill_link_ids == {expected_link_id}
                    and expected_link_id not in conflicts
                    and len(entry_fill_order_ids) == 1
                )
            )
            exact_order_identity = bool(
                (
                    expected_order_id
                    and entry_order_ids == {expected_order_id}
                    and expected_order_id not in conflicts
                )
                or (
                    expected_link_id
                    and entry_order_link_ids == {expected_link_id}
                    and expected_link_id not in conflicts
                    and len(entry_order_ids) == 1
                )
            )
            exact_realtime_identity = realtime_order is not None
            entry_owned = bool(
                (
                    exact_fill_identity
                    or exact_order_identity
                    or exact_realtime_identity
                )
                and entry_fill_qty == remote_size
            )
            position_matches = (
                entry_owned
                and remote_size > 0
                and str(position.get("side") or "").upper() == record.side.value
                and (
                    record.accepted_quantity <= 0
                    or record.accepted_quantity == remote_size
                )
            )
            remote_tp = position.get("takeProfit")
            remote_sl = position.get("stopLoss")
            tp_matches = _same_decimal(remote_tp, record.take_profit)
            sl_matches = _same_decimal(remote_sl, record.stop_loss)
            protection_confirmed = bool(
                position_matches
                and tp_matches
                and sl_matches
            )
            has_remote_protection = bool(
                _decimal(remote_tp) > 0 or _decimal(remote_sl) > 0
            )
            invalid_protection = bool(
                position_matches
                and has_remote_protection
                and not protection_confirmed
            )
            fill_ms = min(
                (
                    int(str(item.get("execTime") or "0"))
                    for item in entry_fills
                    if int(str(item.get("execTime") or "0")) > 0
                ),
                default=0,
            )
            if not fill_ms:
                fill_ms = min(
                    (
                        int(str(
                            item.get("updatedTime")
                            or item.get("createdTime")
                            or "0"
                        ))
                        for item in entry_orders
                        if str(item.get("orderStatus") or "")
                        in {"Filled", "PartiallyFilled"}
                    ),
                    default=0,
                )
            if not fill_ms:
                fill_ms = (
                    int(record.exchange_fill_at.timestamp() * 1000)
                    if record.exchange_fill_at else 0
                )
            if not fill_ms and realtime_order is not None:
                fill_ms = _exchange_ms(
                    realtime_order.get("updatedTime")
                    or realtime_order.get("createdTime")
                )
            fill_at = _from_exchange_ms(fill_ms)
            attribution_anchor = (
                fill_at
                or getattr(record, "order_acknowledged_at", None)
                or getattr(record, "order_submitted_at", None)
                or getattr(record, "exchange_submit_started_at", None)
            )
            elapsed_ms = (
                max(
                    0.0,
                    (observed_at - attribution_anchor).total_seconds() * 1000,
                )
                if attribution_anchor else None
            )
            remaining_ms = (
                max(0.0, protection_timeout_seconds * 1000 - elapsed_ms)
                if elapsed_ms is not None else None
            )
            durable_events = repository.load_demo_execution_events(
                str(record.id)
            )
            protection_started_at = _first_event_time(
                durable_events,
                {"PROTECTION_PENDING", "DEMO_PROTECTION_PENDING"},
            )
            protection_requested_at = _first_event_time(
                durable_events,
                {
                    "DEMO_PROTECTION_REQUESTED",
                    "DEMO_TRADING_STOP_REQUESTED",
                    "PROTECTION_REQUESTED",
                },
            )
            attachment_started = bool(
                protection_started_at
                or protection_requested_at
                or record.state.value
                in {"DEMO_FULLY_FILLED", "DEMO_PROTECTION_PENDING"}
            )
            requested_qty = record.requested_quantity
            partial_entry = bool(
                entry_owned
                and requested_qty > 0
                and entry_fill_qty < requested_qty
            )
            if protection_confirmed:
                protection_state = ProtectionEstablishmentState.PROTECTED
            elif invalid_protection:
                protection_state = (
                    ProtectionEstablishmentState.PROTECTION_INVALIDATED_BY_MARKET
                )
            elif entry_owned and remaining_ms is not None and remaining_ms > 0:
                protection_state = (
                    ProtectionEstablishmentState.ENTRY_PARTIALLY_FILLED
                    if partial_entry else
                    ProtectionEstablishmentState.PROTECTION_PENDING
                    if attachment_started else
                    ProtectionEstablishmentState.ENTRY_ACKNOWLEDGED
                )
            elif entry_owned:
                protection_state = (
                    ProtectionEstablishmentState.UNPROTECTED_CONFIRMED
                )
            elif (
                not record_conflict
                and realtime_error is None
                and (record.order_id or record.order_link_id)
                and remaining_ms is not None
                and remaining_ms > 0
            ):
                protection_state = (
                    ProtectionEstablishmentState.EXACT_ENTRY_ATTRIBUTION_PENDING
                )
            else:
                protection_state = ProtectionEstablishmentState.SAFETY_AMBIGUOUS
            execution_evidence.append(ExecutionFallbackEvidence(
                execution_id=str(record.id),
                symbol=record.symbol.value,
                durable_state=record.state.value,
                remote_position_open=True,
                protection_confirmed=protection_confirmed,
                remote_flat=False,
                exact_owned_residual_orders=len(
                    attributed_orders[str(record.id)]
                ),
                ownership_conflict=record_conflict,
                evidence_error=(
                    None if position_matches else (
                        realtime_error
                        or "exact entry order/fill ownership is not established"
                    )
                ),
                entry_owned=entry_owned,
                protection_state=protection_state,
                fill_at=fill_at.isoformat() if fill_at else None,
                protection_started_at=protection_started_at,
                protection_requested_at=protection_requested_at,
                protection_rest_confirmed_at=(
                    record.protection_confirmed_at.isoformat()
                    if record.protection_confirmed_at else None
                ),
                protection_elapsed_ms=elapsed_ms,
                protection_remaining_deadline_ms=remaining_ms,
                protection_attachment_started=attachment_started,
                authoritative_position_size=str(remote_size),
                authoritative_take_profit=(
                    str(remote_tp) if remote_tp not in {None, ""} else None
                ),
                authoritative_stop_loss=(
                    str(remote_sl) if remote_sl not in {None, ""} else None
                ),
                protection_order_ids=tuple(
                    str(item.get("orderId"))
                    for item in attributed_orders[str(record.id)]
                    if item.get("orderId")
                ),
                invalid_protection_reason=(
                    "authoritative TP/SL differs from durable protection plan"
                    if invalid_protection else None
                ),
                entry_attribution_source=(
                    "execution_history"
                    if exact_fill_identity else
                    "order_history"
                    if exact_order_identity else
                    "realtime_order"
                    if exact_realtime_identity else
                    "realtime_order_pending"
                ),
                entry_attribution_lookup_attempts=(
                    1 if str(record.id) in realtime_by_execution else 0
                ),
                realtime_order_id=(
                    str(realtime_order.get("orderId") or "")
                    if realtime_order is not None else None
                ),
                realtime_order_link_id=(
                    str(realtime_order.get("orderLinkId") or "")
                    if realtime_order is not None else None
                ),
                realtime_order_status=(
                    str(realtime_order.get("orderStatus") or "")
                    if realtime_order is not None else None
                ),
                realtime_order_quantity=(
                    str(realtime_order.get("qty") or "")
                    if realtime_order is not None else None
                ),
                realtime_cumulative_quantity=(
                    str(realtime_order.get("cumExecQty") or "")
                    if realtime_order is not None else None
                ),
                realtime_order_created_at=(
                    _exchange_iso(realtime_order.get("createdTime"))
                    if realtime_order is not None else None
                ),
                realtime_order_updated_at=(
                    _exchange_iso(realtime_order.get("updatedTime"))
                    if realtime_order is not None else None
                ),
                realtime_identity_match=(
                    True if realtime_order is not None else
                    False if realtime_conflict else None
                ),
                first_exact_attribution_at=(
                    observed_at.isoformat() if entry_owned else None
                ),
                fill_history_observed_at=(
                    observed_at.isoformat() if exact_fill_identity else None
                ),
            ))
            continue
        history = order_history_by_symbol[record.symbol.value]
        executions = executions_by_symbol[record.symbol.value]
        entry_orders = [
            item for item in history
            if _matches_entry_identity(item, record)
            and str(item.get("symbol") or "") == record.symbol.value
            and str(item.get("side") or "").upper() == record.side.value
        ]
        entry_fills = [
            item for item in executions
            if _matches_entry_identity(item, record)
            and str(item.get("symbol") or "") == record.symbol.value
            and str(item.get("side") or "").upper() == record.side.value
        ]
        expected_order_id = str(record.order_id or "")
        expected_link_id = str(record.order_link_id or "")
        entry_fill_order_ids = {
            str(item.get("orderId") or "") for item in entry_fills
            if item.get("orderId")
        }
        entry_fill_link_ids = {
            str(item.get("orderLinkId") or "") for item in entry_fills
            if item.get("orderLinkId")
        }
        entry_order_ids = {
            str(item.get("orderId") or "") for item in entry_orders
            if item.get("orderId")
        }
        entry_order_link_ids = {
            str(item.get("orderLinkId") or "") for item in entry_orders
            if item.get("orderLinkId")
        }
        entry_owned = bool(
            (
                expected_order_id
                and entry_fill_order_ids == {expected_order_id}
                and expected_order_id not in conflicts
            )
            or (
                expected_link_id
                and entry_fill_link_ids == {expected_link_id}
                and expected_link_id not in conflicts
                and len(entry_fill_order_ids) == 1
            )
            or (
                expected_order_id
                and entry_order_ids == {expected_order_id}
                and expected_order_id not in conflicts
            )
            or (
                expected_link_id
                and entry_order_link_ids == {expected_link_id}
                and expected_link_id not in conflicts
                and len(entry_order_ids) == 1
            )
        )
        fill_qty = sum(
            (_decimal(item.get("execQty")) for item in entry_fills),
            Decimal("0"),
        )
        if fill_qty <= 0 and entry_orders:
            fill_qty = max(
                (_decimal(item.get("cumExecQty")) for item in entry_orders),
                default=Decimal("0"),
            )
        fill_ms = min(
            (
                int(str(item.get("execTime") or "0"))
                for item in entry_fills
                if int(str(item.get("execTime") or "0")) > 0
            ),
            default=0,
        )
        if not fill_ms:
            fill_ms = min(
                (
                    int(str(
                        item.get("updatedTime")
                        or item.get("createdTime")
                        or "0"
                    ))
                    for item in entry_orders
                    if str(item.get("orderStatus") or "")
                    in {"Filled", "PartiallyFilled"}
                ),
                default=0,
            )
        fill_at = _from_exchange_ms(fill_ms)
        elapsed_ms = (
            max(0.0, (observed_at - fill_at).total_seconds() * 1000)
            if fill_at else None
        )
        remaining_ms = (
            max(0.0, protection_timeout_seconds * 1000 - elapsed_ms)
            if elapsed_ms is not None else None
        )
        if entry_owned and fill_at and remaining_ms and remaining_ms > 0:
            durable_events = repository.load_demo_execution_events(
                str(record.id)
            )
            protection_started_at = _first_event_time(
                durable_events,
                {"PROTECTION_PENDING", "DEMO_PROTECTION_PENDING"},
            )
            partial_entry = bool(
                record.requested_quantity > 0
                and fill_qty < record.requested_quantity
            )
            execution_evidence.append(ExecutionFallbackEvidence(
                execution_id=str(record.id),
                symbol=record.symbol.value,
                durable_state=record.state.value,
                remote_position_open=False,
                protection_confirmed=False,
                remote_flat=False,
                entry_owned=True,
                protection_state=(
                    ProtectionEstablishmentState.ENTRY_PARTIALLY_FILLED
                    if partial_entry else
                    ProtectionEstablishmentState.PROTECTION_PENDING
                    if protection_started_at else
                    ProtectionEstablishmentState.ENTRY_ACKNOWLEDGED
                ),
                fill_at=fill_at.isoformat(),
                protection_started_at=protection_started_at,
                protection_elapsed_ms=elapsed_ms,
                protection_remaining_deadline_ms=remaining_ms,
                protection_attachment_started=bool(protection_started_at),
                evidence_error=(
                    "entry fill is authoritative; position/protection "
                    "snapshot is still propagating"
                ),
            ))
            continue
        if (
            entry_owned
            and not fill_at
            and record.state.value
            in {
                "DEMO_SUBMITTING",
                "DEMO_ORDER_SUBMITTED",
                "DEMO_ORDER_ACKNOWLEDGED",
            }
        ):
            execution_evidence.append(ExecutionFallbackEvidence(
                execution_id=str(record.id),
                symbol=record.symbol.value,
                durable_state=record.state.value,
                remote_position_open=False,
                protection_confirmed=False,
                remote_flat=False,
                entry_owned=True,
                protection_state=ProtectionEstablishmentState.SAFETY_AMBIGUOUS,
                evidence_error=(
                    "entry is acknowledged; authoritative fill is pending"
                ),
            ))
            continue
        try:
            diagnosis = diagnose_demo_execution(
                config,
                str(record.id),
                repository=repository,
                client=client,
            )
            blockers = exact_close_reconciliation_blockers(diagnosis)
            exact_close = not blockers
            partial = (
                "partial" in diagnosis.conclusion.lower()
                or any("quantity" in item and "does not equal" in item for item in blockers)
            )
            conflict = bool(
                record_conflict
                or diagnosis.conflicting_order_ids
                or diagnosis.conflicting_execution_ids
            )
            pending = bool(
                not exact_close
                and not partial
                and not conflict
                and diagnosis.conclusion
                in {
                    "fully filled with unresolved close state",
                    "filled entry; close attribution unavailable; repeatedly flat",
                }
            )
            execution_evidence.append(ExecutionFallbackEvidence(
                execution_id=str(record.id),
                symbol=record.symbol.value,
                durable_state=record.state.value,
                remote_position_open=False,
                protection_confirmed=False,
                remote_flat=True,
                exact_close_evidence=exact_close,
                close_evidence_pending=pending,
                exact_owned_residual_orders=len(
                    attributed_orders[str(record.id)]
                ),
                partial_close=partial,
                ownership_conflict=conflict,
                evidence_error=None if exact_close or pending else "; ".join(blockers),
            ))
        except Exception as exc:
            execution_evidence.append(ExecutionFallbackEvidence(
                execution_id=str(record.id),
                symbol=record.symbol.value,
                durable_state=record.state.value,
                remote_position_open=False,
                protection_confirmed=False,
                remote_flat=True,
                close_evidence_pending=True,
                evidence_error=f"close evidence fetch pending: {type(exc).__name__}",
            ))

    return StatusFallbackEvidence(
        runner_alive=runner_alive,
        uvicorn_alive=uvicorn_alive,
        port_listening=listener,
        persistence_ok=persistence_ok,
        kill_switch_active=kill_switch_active,
        executions=tuple(execution_evidence),
        unrelated_positions=unrelated_positions,
        unrelated_orders=unrelated_orders,
        ownership_conflicts=len(conflicts),
        authoritative_check_complete=True,
    )


def _same_decimal(remote: Any, durable: Any) -> bool:
    if durable is None or remote in {None, ""}:
        return False
    return _decimal(remote) == _decimal(durable)


def runtime_blockers(
    v2: dict[str, Any],
    demo: dict[str, Any],
    completed: list[dict[str, Any]],
) -> list[str]:
    blockers: list[str] = []
    if int(v2.get("total_cycle_failures") or 0):
        blockers.append("cycle failure")
    if v2.get("persistence_status") != "OK":
        blockers.append("persistence status is not OK")
    if bool(v2.get("kill_switch_active")) or bool(demo.get("kill_switch_active")):
        blockers.append("kill switch active")
    if int(demo.get("confirmed_unrelated_orders") or 0):
        blockers.append("confirmed unrelated order")
    if int(demo.get("ownership_conflicts") or 0):
        blockers.append("ownership conflict")
    for execution in completed:
        durable = execution.get("realized_exchange_pnl")
        authoritative = execution.get("authoritative_closed_pnl")
        if (
            execution.get("accounting_status") != "FINAL"
            or durable is None
            or authoritative is None
            or _decimal(durable) != _decimal(authoritative)
        ):
            blockers.append(
                f"durable/exchange PnL mismatch execution={execution.get('id')}"
            )
    return sorted(set(blockers))


def process_alive(pid: int | None) -> bool:
    if pid is None or pid <= 0:
        return False
    if os.name != "nt":
        try:
            os.kill(pid, 0)
            return True
        except OSError:
            return False
    process = ctypes.windll.kernel32.OpenProcess(0x1000, False, pid)
    if not process:
        return False
    code = ctypes.c_ulong()
    try:
        return bool(
            ctypes.windll.kernel32.GetExitCodeProcess(
                process, ctypes.byref(code)
            )
            and code.value == 259
        )
    finally:
        ctypes.windll.kernel32.CloseHandle(process)


def port_listening(port: int) -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as connection:
        connection.settimeout(0.3)
        return connection.connect_ex(("127.0.0.1", port)) == 0


def decimal_sum(values: Any) -> str:
    from decimal import Decimal

    return str(sum((Decimal(str(value)) for value in values), Decimal("0")))


def _matches_entry_identity(item: dict[str, Any], record: Any) -> bool:
    order_id = str(item.get("orderId") or "")
    order_link_id = str(item.get("orderLinkId") or "")
    return bool(
        (record.order_id and order_id == str(record.order_id))
        or (
            record.order_link_id
            and order_link_id == str(record.order_link_id)
        )
    )


def _evaluate_exact_realtime_entry(
    *,
    rows: list[dict[str, Any]],
    record: Any,
    position: dict[str, Any],
    observed_at: datetime,
    globally_conflicting_identities: set[str],
) -> tuple[dict[str, Any] | None, str | None, bool]:
    """Return one exact, compatible realtime entry or fail-closed evidence."""
    if not rows:
        return None, None, False
    if len(rows) != 1:
        return None, "multiple conflicting exact realtime orders returned", True
    order = rows[0]
    expected_order_id = str(record.order_id or "")
    expected_link_id = str(record.order_link_id or "")
    returned_order_id = str(order.get("orderId") or "")
    returned_link_id = str(order.get("orderLinkId") or "")
    if expected_order_id:
        if returned_order_id != expected_order_id:
            return None, "realtime orderId conflicts with durable entry", True
    elif not expected_link_id or returned_link_id != expected_link_id:
        return None, "realtime orderLinkId conflicts with durable entry", True
    if (
        expected_link_id
        and returned_link_id
        and returned_link_id != expected_link_id
    ):
        return None, "realtime orderLinkId conflicts with durable entry", True
    for identity in (returned_order_id, returned_link_id):
        if identity and identity in globally_conflicting_identities:
            return None, "realtime entry identity is not globally unique", True
    if str(order.get("symbol") or "") != record.symbol.value:
        return None, "realtime entry symbol conflicts with durable entry", True
    if str(order.get("side") or "").upper() != record.side.value:
        return None, "realtime entry side conflicts with durable entry", True
    expected_position_idx = int(
        getattr(record, "protection_position_idx", 0) or 0
    )
    if int(order.get("positionIdx") or 0) != expected_position_idx:
        return None, "realtime entry positionIdx conflicts with durable entry", True
    if int(position.get("positionIdx") or 0) != expected_position_idx:
        return None, "remote positionIdx conflicts with durable entry", True
    category = str(order.get("category") or "")
    if category and category != "linear":
        return None, "realtime entry category is not linear", True
    if _api_bool(order.get("reduceOnly")):
        return None, "reduce-only realtime order cannot prove entry", True
    if _api_bool(order.get("closeOnTrigger")):
        return None, "close-on-trigger realtime order cannot prove entry", True
    order_qty = _decimal(order.get("qty"))
    if order_qty <= 0 or order_qty != record.requested_quantity:
        return None, "realtime entry quantity conflicts with durable submission", True
    cumulative_qty = _decimal(order.get("cumExecQty"))
    if cumulative_qty <= 0:
        return None, "realtime entry has zero cumulative executed quantity", True
    remote_size = _decimal(position.get("size"))
    if cumulative_qty != remote_size:
        return None, "realtime executed quantity conflicts with remote position", True
    if str(position.get("side") or "").upper() != record.side.value:
        return None, "remote position side conflicts with durable entry", True
    if str(order.get("orderStatus") or "") not in {"PartiallyFilled", "Filled"}:
        return None, "realtime entry status is not executed", True
    created_ms = _exchange_ms(order.get("createdTime"))
    updated_ms = _exchange_ms(order.get("updatedTime"))
    if created_ms <= 0 or updated_ms <= 0 or updated_ms < created_ms:
        return None, "realtime entry timestamps are invalid", True
    submitted_at = (
        getattr(record, "exchange_submit_started_at", None)
        or getattr(record, "order_submitted_at", None)
    )
    if submitted_at is not None:
        submitted_ms = int(submitted_at.timestamp() * 1000)
        if created_ms < submitted_ms - 10:
            return None, "realtime entry predates durable submission", True
    if updated_ms > int(observed_at.timestamp() * 1000) + 10_000:
        return None, "realtime entry timestamp is implausibly in the future", True
    return order, None, False


def _api_bool(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    return str(value or "").strip().casefold() in {"1", "true", "yes"}


def _exchange_ms(value: Any) -> int:
    try:
        return int(str(value or "0"))
    except (TypeError, ValueError):
        return 0


def _exchange_iso(value: Any) -> str | None:
    parsed = _from_exchange_ms(_exchange_ms(value))
    return parsed.isoformat() if parsed else None


def _from_exchange_ms(value: int) -> datetime | None:
    if value <= 0:
        return None
    return datetime.fromtimestamp(value / 1000, tz=timezone.utc)


def _first_event_time(
    events: list[dict[str, Any]], event_types: set[str],
) -> str | None:
    for event in events:
        if str(event.get("event_type") or "") in event_types:
            value = event.get("occurred_at")
            return str(value) if value else None
    return None


def _decimal(value: Any):
    from decimal import Decimal

    return Decimal(str(value or "0"))


def write_jsonl(path: Path, value: dict[str, Any]) -> None:
    with path.open("a", encoding="utf-8", newline="\n") as stream:
        stream.write(json.dumps(value, separators=(",", ":"), default=str))
        stream.write("\n")


def write_json(path: Path, value: dict[str, Any]) -> None:
    path.write_text(
        json.dumps(value, indent=2, default=str), encoding="utf-8"
    )


def write_events(path: Path, health: CertificationMonitorHealth) -> None:
    while health.events:
        write_jsonl(path, health.events.pop(0))


if __name__ == "__main__":
    raise SystemExit(main())
