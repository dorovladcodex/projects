from __future__ import annotations

import asyncio
from collections.abc import Callable
from datetime import datetime, timezone
import json
from pathlib import Path
import sys
from threading import Lock, Timer, enumerate as enumerate_threads
from time import perf_counter
import traceback
from typing import Any, TypeVar


T = TypeVar("T")


class StartupStepTimeout(RuntimeError):
    """A bounded startup step exceeded its explicit deadline."""


class StartupDiagnostics:
    """Durable, credential-free FastAPI lifespan timing diagnostics."""

    def __init__(
        self,
        *,
        run_id: str,
        output_directory: str | Path,
        diagnostic_threshold_seconds: float = 10,
    ) -> None:
        self.run_id = run_id
        self.output_directory = Path(output_directory)
        self.diagnostic_threshold_seconds = diagnostic_threshold_seconds
        self.started_at = datetime.now(timezone.utc)
        self.finished_at: datetime | None = None
        self.state = "INITIALIZING"
        self.current_step: str | None = None
        self.steps: list[dict[str, Any]] = []
        self._lock = Lock()
        self._active: dict[str, dict[str, Any]] = {}
        self._write()

    @property
    def report_path(self) -> Path:
        return self.output_directory / "startup-diagnostics.json"

    @property
    def stack_path(self) -> Path:
        return self.output_directory / "startup-stacks.json"

    async def run_blocking(
        self,
        name: str,
        action: Callable[[], T],
        *,
        timeout_seconds: float,
        critical: bool = True,
    ) -> T | None:
        record = self._begin(name, timeout_seconds, critical)
        worker = asyncio.create_task(
            asyncio.to_thread(action), name=f"startup:{name}"
        )
        watcher = asyncio.create_task(
            self._watch_threshold(name, worker), name=f"startup-watch:{name}"
        )
        try:
            result = await asyncio.wait_for(
                asyncio.shield(worker), timeout=timeout_seconds
            )
        except TimeoutError as exc:
            self._finish(record, "TIMEOUT", error_type=type(exc).__name__)
            self.capture_stacks(name, reason="step_timeout")
            worker.cancel()
            if critical:
                raise StartupStepTimeout(
                    f"startup step timed out: step={name} "
                    f"timeout_seconds={timeout_seconds}"
                ) from exc
            return None
        except Exception as exc:
            self._finish(record, "FAILED", error_type=type(exc).__name__)
            if critical:
                self.mark_failed(exc)
                raise
            return None
        else:
            self._finish(record, "PASS")
            return result
        finally:
            watcher.cancel()
            try:
                await watcher
            except asyncio.CancelledError:
                pass

    def run_sync(
        self,
        name: str,
        action: Callable[[], T],
        *,
        timeout_seconds: float,
        critical: bool = True,
    ) -> T | None:
        """Time import/bootstrap work before an asyncio loop exists.

        PostgreSQL's driver-side statement timeout supplies the hard I/O bound;
        this watchdog supplies the exact Python stack when a synchronous
        bootstrap step crosses the diagnostic threshold.
        """
        record = self._begin(name, timeout_seconds, critical)
        watcher = Timer(
            self.diagnostic_threshold_seconds,
            self.capture_stacks,
            args=(name,),
            kwargs={"reason": "diagnostic_threshold_exceeded"},
        )
        watcher.daemon = True
        watcher.start()
        try:
            result = action()
        except Exception as exc:
            self._finish(record, "FAILED", error_type=type(exc).__name__)
            if critical:
                raise
            return None
        finally:
            watcher.cancel()
        duration = float(record.get("_timer", perf_counter()))
        elapsed = perf_counter() - duration
        if elapsed > timeout_seconds:
            self._finish(record, "TIMEOUT", error_type="StartupStepTimeout")
            if critical:
                exc = StartupStepTimeout(
                    f"startup step timed out: step={name} "
                    f"timeout_seconds={timeout_seconds}"
                )
                self.mark_failed(exc)
                raise exc
            return None
        self._finish(record, "PASS")
        return result

    def mark_ready(self) -> None:
        with self._lock:
            self.state = "READY"
            self.current_step = None
            self.finished_at = datetime.now(timezone.utc)
        self._write()

    def mark_failed(self, exc: BaseException) -> None:
        with self._lock:
            self.state = "FAILED"
            self.finished_at = datetime.now(timezone.utc)
            error_type = type(exc).__name__
        self.capture_stacks(self.current_step or "startup", reason=error_type)
        self._write()

    def mark_stopped(self) -> None:
        with self._lock:
            if self.state == "READY":
                self.state = "STOPPED"
            self.finished_at = datetime.now(timezone.utc)
        self._write()

    def payload(self) -> dict[str, Any]:
        with self._lock:
            now = self.finished_at or datetime.now(timezone.utc)
            return {
                "run_id": self.run_id,
                "state": self.state,
                "started_at": self.started_at.isoformat(),
                "finished_at": (
                    self.finished_at.isoformat() if self.finished_at else None
                ),
                "duration_seconds": round(
                    (now - self.started_at).total_seconds(), 6
                ),
                "current_step": self.current_step,
                "steps": [dict(item) for item in self.steps],
                "diagnostic_stack_path": str(self.stack_path),
            }

    def capture_stacks(self, step: str, *, reason: str) -> None:
        frames = sys._current_frames()
        thread_rows: list[dict[str, Any]] = []
        for thread in enumerate_threads():
            frame = frames.get(thread.ident or -1)
            thread_rows.append({
                "name": thread.name,
                "ident": thread.ident,
                "daemon": thread.daemon,
                "stack": traceback.format_stack(frame) if frame else [],
            })
        task_rows: list[dict[str, Any]] = []
        try:
            for task in asyncio.all_tasks():
                task_rows.append({
                    "name": task.get_name(),
                    "done": task.done(),
                    "cancelled": task.cancelled(),
                    "stack": [
                        line
                        for frame in task.get_stack(limit=30)
                        for line in traceback.format_stack(frame, limit=1)
                    ],
                })
        except RuntimeError:
            task_rows = []
        self._write_json(self.stack_path, {
            "run_id": self.run_id,
            "captured_at": datetime.now(timezone.utc).isoformat(),
            "step": step,
            "reason": reason,
            "threads": thread_rows,
            "asyncio_tasks": task_rows,
        })

    async def _watch_threshold(
        self, name: str, worker: asyncio.Task[Any]
    ) -> None:
        await asyncio.sleep(self.diagnostic_threshold_seconds)
        if not worker.done():
            self.capture_stacks(name, reason="diagnostic_threshold_exceeded")

    def _begin(
        self, name: str, timeout_seconds: float, critical: bool
    ) -> dict[str, Any]:
        record = {
            "name": name,
            "status": "RUNNING",
            "critical": critical,
            "timeout_seconds": timeout_seconds,
            "started_at": datetime.now(timezone.utc).isoformat(),
            "_timer": perf_counter(),
        }
        with self._lock:
            self.current_step = name
            self.steps.append(record)
            self._active[name] = record
        self._write()
        print(
            json.dumps({
                "event": "STARTUP_STEP_STARTED",
                "run_id": self.run_id,
                "step": name,
                "started_at": record["started_at"],
                "timeout_seconds": timeout_seconds,
                "critical": critical,
            }),
            flush=True,
        )
        return record

    def _finish(
        self, record: dict[str, Any], status: str, *, error_type: str | None = None
    ) -> None:
        timer = float(record.pop("_timer", perf_counter()))
        with self._lock:
            record["status"] = status
            record["finished_at"] = datetime.now(timezone.utc).isoformat()
            record["duration_seconds"] = round(perf_counter() - timer, 6)
            record["error_type"] = error_type
            self._active.pop(str(record["name"]), None)
            self.current_step = next(iter(self._active), None)
        self._write()
        print(
            json.dumps({
                "event": "STARTUP_STEP_FINISHED",
                "run_id": self.run_id,
                "step": record["name"],
                "status": status,
                "duration_seconds": record["duration_seconds"],
                "error_type": error_type,
            }),
            flush=True,
        )

    def _write(self) -> None:
        self._write_json(self.report_path, self.payload())

    @staticmethod
    def _write_json(path: Path, payload: dict[str, Any]) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary = path.with_suffix(path.suffix + ".tmp")
        temporary.write_text(
            json.dumps(payload, indent=2, sort_keys=True),
            encoding="utf-8",
        )
        temporary.replace(path)
