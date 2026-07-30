from __future__ import annotations

import argparse
import ctypes
from datetime import datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation
import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import time
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.bybit.demo_diagnostics import (  # noqa: E402
    DemoDiagnosticsConfig,
    ReadOnlyBybitDemoClient,
)
from app.db.persistence import PersistenceRepository  # noqa: E402


TERMINAL_STATES = {
    "DEMO_CLOSED",
    "DEMO_CLOSED_EXTERNALLY",
    "DEMO_CLOSED_AFTER_FAILURE",
    "DEMO_CLOSED_AFTER_INTERRUPTION",
    "DEMO_ORDER_CANCELLED",
    "DEMO_NOT_SUBMITTED",
    "DEMO_FAILED_FLAT_VERIFIED",
}


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def write_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(
        json.dumps(payload, indent=2, sort_keys=True, default=str),
        encoding="utf-8",
    )
    for attempt in range(20):
        try:
            os.replace(temporary, path)
            return
        except PermissionError:
            # A PowerShell 5.1 reader can briefly deny atomic replacement.
            if attempt == 19:
                raise
            time.sleep(0.05)


def last_uvicorn_pid(events_path: Path) -> int | None:
    if not events_path.exists():
        return None
    try:
        content = events_path.read_text(encoding="utf-8-sig")
    except OSError:
        # PowerShell Add-Content briefly holds an exclusive Windows file lock.
        # Missing one PID observation is safe; the next heartbeat retries.
        return None
    for raw in reversed(content.splitlines()):
        try:
            value = json.loads(raw).get("uvicorn_pid")
        except (json.JSONDecodeError, AttributeError):
            continue
        if value:
            return int(value)
    return None


def process_alive(pid: int | None) -> bool:
    if not pid:
        return False
    if os.name == "nt":
        process_query_limited_information = 0x1000
        still_active = 259
        handle = ctypes.windll.kernel32.OpenProcess(
            process_query_limited_information, False, pid
        )
        if not handle:
            return False
        try:
            exit_code = ctypes.c_ulong()
            if not ctypes.windll.kernel32.GetExitCodeProcess(
                handle, ctypes.byref(exit_code)
            ):
                return False
            return exit_code.value == still_active
        finally:
            ctypes.windll.kernel32.CloseHandle(handle)
    try:
        os.kill(pid, 0)
    except OSError:
        return False
    return True


def authoritative_flat_and_resolved() -> tuple[bool, dict[str, Any]]:
    """Use the strict read-only client; no mutation-capable client is loaded."""
    config = DemoDiagnosticsConfig.load(env_path=ROOT / ".env")
    client = ReadOnlyBybitDemoClient(
        config.api_key,
        config.api_secret,
        base_url=config.rest_url,
    )
    client.verify()
    def positive(value: Any) -> bool:
        try:
            return Decimal(str(value or "0")) > 0
        except InvalidOperation:
            return True

    positions = [
        row for row in client.get_usdt_positions() if positive(row.get("size"))
    ]
    orders = client.get_open_orders()
    repository = PersistenceRepository(config.database_url, create_schema=False)
    unresolved = [
        row
        for row in repository.load_demo_executions()
        if row.state.value not in TERMINAL_STATES
    ]
    evidence = {
        "remote_position_count": len(positions),
        "remote_open_order_count": len(orders),
        "unresolved_execution_count": len(unresolved),
    }
    return not positions and not orders and not unresolved, evidence


def terminate_managed_tree(pid: int) -> None:
    if os.name == "nt":
        subprocess.run(
            ["taskkill.exe", "/PID", str(pid), "/T", "/F"],
            check=False,
            capture_output=True,
            text=True,
            timeout=20,
        )
        return
    os.killpg(pid, signal.SIGTERM)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Detached controller for one bounded real-Demo canary."
    )
    parser.add_argument("--run-id", required=True)
    parser.add_argument("--artifact-dir", type=Path, required=True)
    parser.add_argument("--hard-timeout-seconds", type=int, default=600)
    parser.add_argument("--symbol", default="XRPUSDT")
    parser.add_argument("--max-notional-usdt", default="125")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    artifact_dir = args.artifact_dir.resolve()
    artifact_dir.mkdir(parents=True, exist_ok=True)
    control_dir = artifact_dir / "managed-control"
    control_dir.mkdir(parents=True, exist_ok=True)
    metadata_path = control_dir / "managed-canary.json"
    heartbeat_path = control_dir / "heartbeat.json"
    result_path = artifact_dir / "report.json"
    stdout_path = control_dir / "controller.stdout.log"
    stderr_path = control_dir / "controller.stderr.log"
    started_at = utc_now()
    deadline = started_at + timedelta(seconds=args.hard_timeout_seconds)
    metadata: dict[str, Any] = {
        "run_id": args.run_id,
        "controller_pid": os.getpid(),
        "runner_pid": None,
        "uvicorn_pid": None,
        "started_at": started_at.isoformat(),
        "hard_deadline": deadline.isoformat(),
        "phase": "STARTING",
        "heartbeat_at": started_at.isoformat(),
        "heartbeat_path": str(heartbeat_path),
        "result_path": str(result_path),
        "artifact_path": str(artifact_dir),
        "stdout_path": str(stdout_path),
        "stderr_path": str(stderr_path),
        "exit_code": None,
        "terminal_reason": None,
    }
    write_json(metadata_path, metadata)
    write_json(heartbeat_path, metadata)

    powershell = (
        Path(os.environ.get("SystemRoot", r"C:\Windows"))
        / "System32"
        / "WindowsPowerShell"
        / "v1.0"
        / "powershell.exe"
    )
    command = [
        str(powershell),
        "-ExecutionPolicy",
        "Bypass",
        "-File",
        str(ROOT / "scripts" / "demo_v2_protection_freshness_canary.ps1"),
        "-RunId",
        args.run_id,
        "-Symbol",
        args.symbol,
        "-MaxNotionalUSDT",
        str(args.max_notional_usdt),
        "-AllowDemoOrders",
    ]
    creationflags = 0
    if os.name == "nt":
        creationflags = subprocess.CREATE_NEW_PROCESS_GROUP
    with stdout_path.open("w", encoding="utf-8") as stdout, stderr_path.open(
        "w", encoding="utf-8"
    ) as stderr:
        child = subprocess.Popen(
            command,
            cwd=ROOT,
            stdout=stdout,
            stderr=stderr,
            stdin=subprocess.DEVNULL,
            creationflags=creationflags,
        )
        metadata["runner_pid"] = child.pid
        metadata["phase"] = "RUNNING"
        write_json(metadata_path, metadata)

        while child.poll() is None:
            now = utc_now()
            metadata["heartbeat_at"] = now.isoformat()
            metadata["uvicorn_pid"] = last_uvicorn_pid(
                artifact_dir / "controller-events.jsonl"
            )
            if now >= deadline:
                metadata["phase"] = "HARD_DEADLINE"
                try:
                    safe, evidence = authoritative_flat_and_resolved()
                except Exception as exc:  # fail closed; never expose credentials
                    safe, evidence = False, {
                        "verification_error": type(exc).__name__,
                    }
                metadata["deadline_safety_evidence"] = evidence
                if safe:
                    terminate_managed_tree(child.pid)
                    child.wait(timeout=30)
                    metadata["terminal_reason"] = (
                        "HARD_DEADLINE_SAFE_FLAT_TERMINATION"
                    )
                else:
                    metadata["terminal_reason"] = (
                        "HARD_DEADLINE_SAFETY_BLOCKED_PROCESS_RETAINED"
                    )
                write_json(metadata_path, metadata)
                write_json(heartbeat_path, metadata)
                if not safe:
                    return 3
                break
            write_json(metadata_path, metadata)
            write_json(heartbeat_path, metadata)
            time.sleep(1)

        exit_code = child.poll()
        metadata["exit_code"] = exit_code
        metadata["heartbeat_at"] = utc_now().isoformat()
        if metadata["terminal_reason"] is None:
            metadata["terminal_reason"] = (
                "CANARY_COMPLETED" if exit_code == 0 else "CANARY_FAILED"
            )
        metadata["phase"] = "FINISHED" if exit_code == 0 else "FAILED"
        metadata["uvicorn_pid"] = last_uvicorn_pid(
            artifact_dir / "controller-events.jsonl"
        )
        metadata["runner_alive"] = False
        metadata["uvicorn_alive"] = process_alive(metadata["uvicorn_pid"])
        write_json(metadata_path, metadata)
        write_json(heartbeat_path, metadata)
        return int(exit_code or 0)


if __name__ == "__main__":
    raise SystemExit(main())
