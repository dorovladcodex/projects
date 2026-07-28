from __future__ import annotations

import argparse
import ctypes
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import socket
import sys
import time
from typing import Any
from urllib.request import urlopen
from urllib.parse import urlparse

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.bybit.demo_diagnostics import (  # noqa: E402
    DemoDiagnosticsConfig,
    ReadOnlyBybitDemoClient,
)
from app.db.persistence import PersistenceRepository  # noqa: E402
from app.v2.certification_monitor import (  # noqa: E402
    CertificationMonitorHealth,
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
    health = CertificationMonitorHealth(
        hard_timeout_seconds=args.hard_timeout_seconds
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
            health.record_status_success(now=now)
        except Exception as exc:
            persistence_ok, active = durable_fallback(
                repository, args.run_id
            )
            authoritative_safe = None
            if active:
                authoritative_safe = authoritative_protection_fallback(
                    read_client, active
                )
            decision = health.record_status_failure(
                now=now,
                runner_alive=runner_alive,
                uvicorn_alive=uvicorn_alive,
                port_listening=listener,
                persistence_ok=persistence_ok,
                authoritative_account_safe=authoritative_safe,
                error=f"{type(exc).__name__}: {exc}",
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
                "authoritative_account_safe": authoritative_safe,
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
            time.sleep(args.retry_poll_seconds)
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


def durable_fallback(
    repository: PersistenceRepository, run_id: str
) -> tuple[bool, list[Any]]:
    if not repository.available:
        return False, []
    runtime = repository.load_v2_run_runtime(run_id)
    records = [
        item
        for item in repository.load_demo_executions()
        if item.run_id == run_id and item.state.value not in TERMINAL_STATES
    ]
    return bool(runtime), records


def authoritative_protection_fallback(
    client: ReadOnlyBybitDemoClient, active: list[Any]
) -> bool:
    try:
        client.verify()
        positions = [
            item
            for item in client.get_usdt_positions()
            if _decimal(item.get("size")) > 0
        ]
    except Exception:
        return False
    expected = {item.symbol.value: item for item in active}
    for position in positions:
        symbol = str(position.get("symbol") or "")
        record = expected.get(symbol)
        if record is None:
            return False
        if (
            _decimal(position.get("size")) != record.accepted_quantity
            or str(position.get("side") or "").upper() != record.side.value
            or not str(position.get("takeProfit") or "")
            or not str(position.get("stopLoss") or "")
        ):
            return False
    return len(positions) == len(active)


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
