from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from app.db.readiness import PersistenceStartupError, wait_for_persistence
from app.startup import StartupDiagnostics


class _Clock:
    def __init__(self) -> None:
        self.value = 0.0

    def monotonic(self) -> float:
        return self.value

    def sleep(self, seconds: float) -> None:
        self.value += seconds


class _Repository:
    def __init__(
        self,
        *,
        available: bool,
        healthy: bool,
        error: str = "DB_OPERATIONALERROR",
        sqlstate: str | None = "08006",
    ) -> None:
        self.available = available
        self.healthy = healthy
        self.last_error_code = error
        self.last_error = error
        self.last_sqlstate = sqlstate
        self.disposed = 0
        self.health_calls = 0

    def health_check(self) -> bool:
        self.health_calls += 1
        return self.healthy

    def dispose(self) -> None:
        self.disposed += 1


class _Factory:
    def __init__(self, repositories: list[_Repository]) -> None:
        self.repositories = repositories
        self.created: list[_Repository] = []

    def __call__(self, *_args: Any, **_kwargs: Any) -> _Repository:
        repository = self.repositories[len(self.created)]
        self.created.append(repository)
        return repository


def _wait(
    factory: _Factory,
    clock: _Clock,
    statuses: list[dict[str, Any]],
    *,
    timeout: float = 5,
):
    return wait_for_persistence(
        "postgresql+psycopg://user:hidden@127.0.0.1:5432/bybot",
        timeout_seconds=timeout,
        repository_factory=factory,
        status_callback=lambda item: statuses.append(dict(item)),
        monotonic=clock.monotonic,
        sleeper=clock.sleep,
        jitter=lambda _low, _high: 0,
    )


def test_healthy_repository_passes_on_first_attempt() -> None:
    repository = _Repository(available=True, healthy=True)
    factory = _Factory([repository])
    clock = _Clock()
    statuses: list[dict[str, Any]] = []

    result = _wait(factory, clock, statuses)

    assert result.repository is repository
    assert result.attempts == 1
    assert repository.health_calls == 1
    assert repository.disposed == 0
    assert statuses[-1]["state"] == "PASS"
    assert statuses[-1]["repository_available"] is True
    assert statuses[-1]["health_query_passed"] is True


def test_unavailable_repository_retries_and_disposes_failed_pool() -> None:
    failed = _Repository(available=False, healthy=False)
    healthy = _Repository(available=True, healthy=True)
    factory = _Factory([failed, healthy])
    statuses: list[dict[str, Any]] = []

    result = _wait(factory, _Clock(), statuses)

    assert result.repository is healthy
    assert failed.health_calls == 0
    assert failed.disposed == 1
    assert healthy.disposed == 0
    assert [item["state"] for item in statuses] == [
        "PENDING", "RETRYING", "PASS",
    ]


def test_health_query_failure_retries_without_returning_degraded_repository() -> None:
    failed = _Repository(available=True, healthy=False)
    healthy = _Repository(available=True, healthy=True)
    factory = _Factory([failed, healthy])
    statuses: list[dict[str, Any]] = []

    result = _wait(factory, _Clock(), statuses)

    assert result.repository is healthy
    assert failed.disposed == 1
    assert not any(
        item["state"] == "PASS" and not item["repository_available"]
        for item in statuses
    )


def test_persistent_outage_fails_explicitly_and_disposes_every_repository() -> None:
    repositories = [
        _Repository(available=False, healthy=False) for _ in range(4)
    ]
    factory = _Factory(repositories)
    statuses: list[dict[str, Any]] = []

    with pytest.raises(PersistenceStartupError) as error:
        _wait(factory, _Clock(), statuses, timeout=3.5)

    assert error.value.report["state"] == "FAIL"
    assert error.value.report["repository_available"] is False
    assert error.value.report["health_query_passed"] is False
    assert error.value.report["last_sqlstate"] == "08006"
    assert all(item.disposed == 1 for item in factory.created)
    assert statuses[-1]["state"] == "FAIL"


def test_transient_recovery_returns_only_one_final_usable_repository() -> None:
    failed_one = _Repository(available=False, healthy=False)
    failed_two = _Repository(available=True, healthy=False)
    healthy = _Repository(available=True, healthy=True)
    factory = _Factory([failed_one, failed_two, healthy])

    result = _wait(factory, _Clock(), [])

    assert result.repository is healthy
    assert result.attempts == 3
    assert [item.disposed for item in factory.created] == [1, 1, 0]


def test_startup_status_reports_retrying_then_pass_monotonically(
    tmp_path: Path,
) -> None:
    diagnostics = StartupDiagnostics(
        run_id="readiness",
        output_directory=tmp_path,
    )
    diagnostics.update_persistence_readiness({
        "state": "RETRYING",
        "attempt_count": 1,
        "repository_available": False,
        "health_query_passed": False,
    })
    retrying = diagnostics.payload()["persistence_connect"]
    assert retrying["state"] == "RETRYING"
    assert retrying["attempt_count"] == 1

    diagnostics.update_persistence_readiness({
        "state": "PASS",
        "attempt_count": 2,
        "repository_available": True,
        "health_query_passed": True,
    })
    assert diagnostics.payload()["persistence_connect"]["state"] == "PASS"
    with pytest.raises(RuntimeError, match="PASS->FAIL"):
        diagnostics.update_persistence_readiness({"state": "FAIL"})


def test_startup_status_reports_persistence_failure_and_sanitized_endpoint(
    tmp_path: Path,
) -> None:
    diagnostics = StartupDiagnostics(
        run_id="readiness-failure",
        output_directory=tmp_path,
    )
    factory = _Factory([
        _Repository(available=False, healthy=False),
        _Repository(available=False, healthy=False),
    ])

    with pytest.raises(PersistenceStartupError):
        diagnostics.run_sync(
            "persistence_connect",
            lambda: _wait(factory, _Clock(), [], timeout=0.5),
            timeout_seconds=1,
        )

    payload = diagnostics.payload()
    assert payload["state"] == "FAILED"
    assert payload["startup_final_result"] == "FAIL"
    assert payload["persistence_connect"]["state"] == "FAIL"
    assert "hidden" not in str(payload)


def test_controller_has_distinct_bounded_startup_stages() -> None:
    source = (
        Path(__file__).parents[1]
        / "scripts"
        / "demo_v2_two_close_canary.py"
    ).read_text(encoding="utf-8")
    for stage in (
        "postgresql_readiness",
        "uvicorn_process_start",
        "application_startup_readiness",
        "startup_status_validation",
        "diagnostics",
        "preflight",
        "canary_execution",
    ):
        assert f'"{stage}"' in source
    assert "persistence_startup_failure" in source
    assert "uvicorn_crash" in source
    assert "readiness_timeout" in source
    assert "controller_steps" in source
