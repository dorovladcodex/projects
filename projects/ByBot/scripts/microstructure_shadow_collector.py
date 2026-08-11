from __future__ import annotations

import argparse
import asyncio
from datetime import datetime, timezone
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

from app.config import Settings  # noqa: E402
from app.microstructure.collector import (  # noqa: E402
    CollectorConfiguration,
    MicrostructureCollector,
)
from app.microstructure.storage import write_json_atomic  # noqa: E402


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def read_json(path: Path) -> dict[str, Any] | None:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
        return payload if isinstance(payload, dict) else None
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        return None


def process_alive(pid: int | None) -> bool:
    if pid is None or pid <= 0:
        return False
    try:
        os.kill(pid, 0)
        return True
    except (OSError, PermissionError):
        return False


def default_paths(settings: Settings) -> tuple[Path, Path]:
    config = CollectorConfiguration.from_settings(settings, ROOT)
    return config.artifact_dir, config.artifact_dir / "control"


def start(args: argparse.Namespace) -> int:
    settings = Settings()
    artifact_default, control_default = default_paths(settings)
    artifact_dir = (args.artifact_dir or artifact_default).resolve()
    control_dir = (args.control_dir or control_default).resolve()
    control_dir.mkdir(parents=True, exist_ok=True)
    current_path = control_dir / "collector.json"
    existing = read_json(current_path)
    existing_pid = int(existing.get("pid") or 0) if existing else 0
    if existing and process_alive(existing_pid):
        print(json.dumps({
            "status": "ALREADY_RUNNING",
            "pid": existing_pid,
            "run_id": existing.get("run_id"),
            "heartbeat_path": existing.get("heartbeat_path"),
        }, indent=2))
        return 0

    run_id = f"microstructure-shadow-{utc_now().strftime('%Y%m%dT%H%M%S%fZ')}"
    run_dir = control_dir / "runs" / run_id
    run_dir.mkdir(parents=True, exist_ok=False)
    stdout_path = run_dir / "stdout.log"
    stderr_path = run_dir / "stderr.log"
    heartbeat_path = run_dir / "heartbeat.json"
    metadata_path = run_dir / "run.json"
    command = [
        sys.executable,
        str(Path(__file__).resolve()),
        "run",
        "--artifact-dir", str(artifact_dir),
        "--control-dir", str(control_dir),
        "--run-id", run_id,
        "--metadata-path", str(metadata_path),
        "--heartbeat-path", str(heartbeat_path),
    ]
    creation_flags = (
        getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
        | getattr(subprocess, "DETACHED_PROCESS", 0)
        | getattr(subprocess, "CREATE_NO_WINDOW", 0)
    )
    with stdout_path.open("ab", buffering=0) as stdout, stderr_path.open(
        "ab", buffering=0
    ) as stderr:
        child = subprocess.Popen(
            command,
            cwd=ROOT,
            stdin=subprocess.DEVNULL,
            stdout=stdout,
            stderr=stderr,
            close_fds=True,
            creationflags=creation_flags,
        )
    metadata = {
        "run_id": run_id,
        "pid": child.pid,
        "phase": "STARTING",
        "started_at": utc_now(),
        "artifact_dir": str(artifact_dir),
        "control_dir": str(control_dir),
        "metadata_path": str(metadata_path),
        "heartbeat_path": str(heartbeat_path),
        "stdout_path": str(stdout_path),
        "stderr_path": str(stderr_path),
        "command": command,
        "exchange_execution_capability": False,
    }
    write_json_atomic(metadata_path, metadata)
    write_json_atomic(current_path, metadata)
    deadline = time.monotonic() + args.startup_timeout_seconds
    while time.monotonic() < deadline:
        heartbeat = read_json(heartbeat_path)
        if heartbeat and heartbeat.get("phase") == "RUNNING":
            print(json.dumps({
                "status": "STARTED",
                "run_id": run_id,
                "pid": child.pid,
                "heartbeat_path": str(heartbeat_path),
                "artifact_dir": str(artifact_dir),
                "symbols": heartbeat.get("symbols"),
            }, indent=2))
            return 0
        if not process_alive(child.pid):
            print(json.dumps({
                "status": "FAILED_TO_START",
                "run_id": run_id,
                "pid": child.pid,
                "stderr_path": str(stderr_path),
            }, indent=2))
            return 1
        time.sleep(0.25)
    print(json.dumps({
        "status": "STARTUP_TIMEOUT",
        "run_id": run_id,
        "pid": child.pid,
        "heartbeat_path": str(heartbeat_path),
    }, indent=2))
    return 1


def run_worker(args: argparse.Namespace) -> int:
    settings = Settings()
    config = CollectorConfiguration.from_settings(settings, ROOT)
    if args.artifact_dir is not None:
        config = CollectorConfiguration(
            **{**config.__dict__, "artifact_dir": args.artifact_dir.resolve()}
        )
    control_dir = args.control_dir.resolve()
    control_dir.mkdir(parents=True, exist_ok=True)
    metadata_path = args.metadata_path.resolve()
    heartbeat_path = args.heartbeat_path.resolve()
    current_path = control_dir / "collector.json"
    stop_path = control_dir / "stop-request.json"
    stale_stop = read_json(stop_path)
    if stale_stop and stale_stop.get("run_id") != args.run_id:
        stop_path.unlink(missing_ok=True)

    stop_event = asyncio.Event()
    last_heartbeat = 0.0
    collector = MicrostructureCollector(config)
    metadata = read_json(metadata_path) or {
        "run_id": args.run_id,
        "pid": os.getpid(),
        "started_at": utc_now(),
    }

    def request_stop(*_: object) -> None:
        stop_event.set()

    for signum in (signal.SIGINT, signal.SIGTERM):
        try:
            signal.signal(signum, request_stop)
        except (ValueError, OSError):
            pass

    def heartbeat(status: dict[str, Any]) -> None:
        nonlocal last_heartbeat
        request = read_json(stop_path)
        if request and request.get("run_id") in (None, args.run_id):
            stop_event.set()
        now_monotonic = time.monotonic()
        if now_monotonic - last_heartbeat < 2 and not stop_event.is_set():
            return
        last_heartbeat = now_monotonic
        payload = {
            **status,
            "run_id": args.run_id,
            "pid": os.getpid(),
            "phase": "STOPPING" if stop_event.is_set() else "RUNNING",
            "heartbeat_at": utc_now(),
            "heartbeat_path": str(heartbeat_path),
            "artifact_dir": str(config.artifact_dir),
        }
        write_json_atomic(heartbeat_path, payload)
        write_json_atomic(current_path, {**metadata, **payload})

    async def main_loop() -> None:
        initialization = await asyncio.to_thread(collector.initialize)
        metadata.update({
            "pid": os.getpid(),
            "phase": "RUNNING",
            "initialized_at": utc_now(),
            "symbols": initialization["symbols"],
            "artifact_dir": str(config.artifact_dir),
            "heartbeat_path": str(heartbeat_path),
            "metadata_path": str(metadata_path),
            "exchange_execution_capability": False,
        })
        write_json_atomic(metadata_path, metadata)
        heartbeat(collector.status())
        await collector.run(
            stop_event,
            max_capture_cycles=args.capture_cycles,
            heartbeat=heartbeat,
        )

    exit_code = 0
    try:
        asyncio.run(main_loop())
        phase = "FINISHED" if args.capture_cycles is not None else "STOPPED"
        final = {**metadata, **collector.status(), "phase": phase, "ended_at": utc_now()}
    except Exception as exc:
        exit_code = 1
        final = {
            **metadata,
            "pid": os.getpid(),
            "phase": "FAILED",
            "ended_at": utc_now(),
            "error_type": type(exc).__name__,
            "error": str(exc),
            "exchange_execution_capability": False,
        }
    finally:
        collector.close()
    write_json_atomic(heartbeat_path, final)
    write_json_atomic(metadata_path, final)
    write_json_atomic(current_path, final)
    return exit_code


def status(args: argparse.Namespace) -> int:
    settings = Settings()
    _, control_default = default_paths(settings)
    control_dir = (args.control_dir or control_default).resolve()
    current = read_json(control_dir / "collector.json")
    if current is None:
        print(json.dumps({"status": "NOT_STARTED", "control_dir": str(control_dir)}))
        return 1
    pid = int(current.get("pid") or 0)
    heartbeat_path = Path(str(current.get("heartbeat_path") or ""))
    heartbeat = read_json(heartbeat_path) if str(heartbeat_path) else None
    print(json.dumps({
        "status": "RUNNING" if process_alive(pid) else current.get("phase", "STOPPED"),
        "process_alive": process_alive(pid),
        "control": current,
        "heartbeat": heartbeat,
    }, indent=2, default=str))
    return 0


def stop(args: argparse.Namespace) -> int:
    settings = Settings()
    _, control_default = default_paths(settings)
    control_dir = (args.control_dir or control_default).resolve()
    current = read_json(control_dir / "collector.json")
    if current is None:
        print(json.dumps({"status": "NOT_STARTED"}))
        return 0
    pid = int(current.get("pid") or 0)
    if not process_alive(pid):
        print(json.dumps({"status": "ALREADY_STOPPED", "pid": pid}))
        return 0
    stop_path = control_dir / "stop-request.json"
    write_json_atomic(stop_path, {
        "run_id": current.get("run_id"),
        "requested_at": utc_now(),
        "requested_by": "microstructure_shadow_collector.py stop",
    })
    deadline = time.monotonic() + args.timeout_seconds
    while time.monotonic() < deadline:
        if not process_alive(pid):
            print(json.dumps({"status": "STOPPED", "pid": pid}))
            return 0
        time.sleep(0.25)
    print(json.dumps({
        "status": "GRACEFUL_STOP_TIMEOUT",
        "pid": pid,
        "forced_termination_used": False,
    }))
    return 1


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Managed read-only Bybit microstructure shadow collector."
    )
    commands = parser.add_subparsers(dest="command", required=True)
    start_parser = commands.add_parser("start")
    start_parser.add_argument("--artifact-dir", type=Path)
    start_parser.add_argument("--control-dir", type=Path)
    start_parser.add_argument("--startup-timeout-seconds", type=float, default=30)
    start_parser.set_defaults(func=start)

    run_parser = commands.add_parser("run")
    run_parser.add_argument("--artifact-dir", type=Path, required=True)
    run_parser.add_argument("--control-dir", type=Path, required=True)
    run_parser.add_argument("--run-id", required=True)
    run_parser.add_argument("--metadata-path", type=Path, required=True)
    run_parser.add_argument("--heartbeat-path", type=Path, required=True)
    run_parser.add_argument("--capture-cycles", type=int)
    run_parser.set_defaults(func=run_worker)

    validate_parser = commands.add_parser("validate")
    validate_parser.add_argument("--artifact-dir", type=Path, required=True)
    validate_parser.add_argument("--control-dir", type=Path, required=True)
    validate_parser.add_argument("--run-id", default="microstructure-validation")
    validate_parser.add_argument("--capture-cycles", type=int, default=3)
    validate_parser.set_defaults(
        func=run_worker,
        metadata_path=None,
        heartbeat_path=None,
    )

    status_parser = commands.add_parser("status")
    status_parser.add_argument("--control-dir", type=Path)
    status_parser.set_defaults(func=status)

    stop_parser = commands.add_parser("stop")
    stop_parser.add_argument("--control-dir", type=Path)
    stop_parser.add_argument("--timeout-seconds", type=float, default=30)
    stop_parser.set_defaults(func=stop)
    return parser


def main() -> int:
    parser = build_parser()
    args = parser.parse_args()
    if args.command == "validate":
        args.control_dir = args.control_dir.resolve()
        args.control_dir.mkdir(parents=True, exist_ok=True)
        run_dir = args.control_dir / "runs" / args.run_id
        run_dir.mkdir(parents=True, exist_ok=True)
        args.metadata_path = run_dir / "run.json"
        args.heartbeat_path = run_dir / "heartbeat.json"
    return int(args.func(args))


if __name__ == "__main__":
    raise SystemExit(main())
