from __future__ import annotations

from dataclasses import dataclass
import random
import time
from typing import Any, Callable

from sqlalchemy.engine import make_url

from app.db.persistence import PersistenceRepository


@dataclass(frozen=True)
class PersistenceReadinessResult:
    repository: PersistenceRepository
    attempts: int
    elapsed_ms: int
    host: str
    port: int | None
    database: str


class PersistenceStartupError(RuntimeError):
    def __init__(self, report: dict[str, Any]) -> None:
        super().__init__(
            "PostgreSQL persistence readiness failed within the bounded window"
        )
        self.report = report


def wait_for_persistence(
    database_url: str,
    *,
    timeout_seconds: float = 35,
    connection_timeout_seconds: int = 4,
    create_schema: bool = False,
    repository_factory: Callable[..., PersistenceRepository] = (
        PersistenceRepository
    ),
    status_callback: Callable[[dict[str, Any]], None] | None = None,
    monotonic: Callable[[], float] = time.monotonic,
    sleeper: Callable[[float], None] = time.sleep,
    jitter: Callable[[float, float], float] = random.uniform,
) -> PersistenceReadinessResult:
    """Return exactly one healthy repository or fail after bounded retries."""
    url = make_url(database_url)
    endpoint = {
        "host": str(url.host or ""),
        "port": url.port,
        "database": str(url.database or ""),
    }
    started = monotonic()
    attempts = 0
    last_error_category: str | None = None
    last_sqlstate: str | None = None
    cleanup_completed = True
    backoff = 0.5
    _notify(status_callback, {
        "state": "PENDING",
        "attempt_count": 0,
        "elapsed_ms": 0,
        "last_error_category": None,
        "last_sqlstate": None,
        "last_exception": None,
        "repository_available": False,
        "health_query_passed": False,
        "cleanup_completed": True,
        **endpoint,
    })
    while True:
        attempts += 1
        repository: PersistenceRepository | None = None
        health_query_passed = False
        try:
            repository = repository_factory(
                database_url,
                create_schema=create_schema,
                connect_timeout_seconds=connection_timeout_seconds,
            )
            if repository.available:
                health_query_passed = bool(repository.health_check())
            if repository.available and health_query_passed:
                elapsed_ms = round((monotonic() - started) * 1000)
                _notify(status_callback, {
                    "state": "PASS",
                    "attempt_count": attempts,
                    "elapsed_ms": elapsed_ms,
                    "last_error_category": last_error_category,
                    "last_sqlstate": last_sqlstate,
                    "last_exception": last_error_category,
                    "repository_available": True,
                    "health_query_passed": True,
                    "cleanup_completed": cleanup_completed,
                    **endpoint,
                })
                return PersistenceReadinessResult(
                    repository=repository,
                    attempts=attempts,
                    elapsed_ms=elapsed_ms,
                    **endpoint,
                )
            last_error_category = (
                repository.last_error_code
                or repository.last_error
                or "DB_REPOSITORY_UNAVAILABLE"
            )
            last_sqlstate = repository.last_sqlstate
        except Exception as exc:
            last_error_category = type(exc).__name__
            last_sqlstate = _exception_sqlstate(exc)
        if repository is not None:
            try:
                repository.dispose()
            except Exception:
                cleanup_completed = False
        elapsed = monotonic() - started
        if elapsed >= timeout_seconds:
            report = {
                "state": "FAIL",
                "attempt_count": attempts,
                "elapsed_ms": round(elapsed * 1000),
                "last_error_category": last_error_category,
                "last_sqlstate": last_sqlstate,
                "last_exception": last_error_category,
                "repository_available": False,
                "health_query_passed": False,
                "cleanup_completed": cleanup_completed,
                **endpoint,
            }
            _notify(status_callback, report)
            raise PersistenceStartupError(report)
        delay = min(3.0, backoff) + max(
            0.0, jitter(0.0, min(0.2, backoff * 0.2))
        )
        delay = min(delay, max(0.0, timeout_seconds - elapsed))
        _notify(status_callback, {
            "state": "RETRYING",
            "attempt_count": attempts,
            "elapsed_ms": round(elapsed * 1000),
            "last_error_category": last_error_category,
            "last_sqlstate": last_sqlstate,
            "last_exception": last_error_category,
            "repository_available": False,
            "health_query_passed": False,
            "cleanup_completed": cleanup_completed,
            "next_retry_seconds": round(delay, 3),
            **endpoint,
        })
        sleeper(delay)
        backoff = min(3.0, backoff * 2)


def _notify(
    callback: Callable[[dict[str, Any]], None] | None,
    payload: dict[str, Any],
) -> None:
    if callback is not None:
        callback(payload)


def _exception_sqlstate(exc: BaseException) -> str | None:
    current: BaseException | None = exc
    while current is not None:
        value = getattr(current, "sqlstate", None) or getattr(
            current, "pgcode", None
        )
        if value:
            return str(value)[:10]
        current = current.__cause__ or current.__context__
    return None
