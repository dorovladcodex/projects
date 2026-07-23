from __future__ import annotations

import asyncio
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
import subprocess
import time

import pytest

from app.models import Symbol
from app.startup import StartupDiagnostics, StartupStepTimeout
from app.v2.runtime import V2Runtime
from app.v2.models import UniverseInstrument
from app.v2.universe import SymbolUniverseService


def _diagnostics(tmp_path: Path, threshold: float = 0.01) -> StartupDiagnostics:
    return StartupDiagnostics(
        run_id="startup-test",
        output_directory=tmp_path,
        diagnostic_threshold_seconds=threshold,
    )


def test_bounded_startup_step_records_timestamp_duration_and_status(
    tmp_path: Path,
) -> None:
    diagnostic = _diagnostics(tmp_path)

    result = asyncio.run(diagnostic.run_blocking(
        "database_bootstrap", lambda: "ok", timeout_seconds=1
    ))

    assert result == "ok"
    step = diagnostic.payload()["steps"][0]
    assert step["name"] == "database_bootstrap"
    assert step["status"] == "PASS"
    assert step["started_at"]
    assert step["finished_at"]
    assert step["duration_seconds"] >= 0


def test_network_timeout_fails_with_exact_step_and_captures_stacks(
    tmp_path: Path,
) -> None:
    diagnostic = _diagnostics(tmp_path)

    with pytest.raises(StartupStepTimeout, match="public_network"):
        asyncio.run(diagnostic.run_blocking(
            "public_network", lambda: time.sleep(0.2), timeout_seconds=0.02
        ))

    assert diagnostic.payload()["steps"][0]["status"] == "TIMEOUT"
    assert diagnostic.stack_path.exists()


def test_database_timeout_is_classified_without_credentials(
    tmp_path: Path,
) -> None:
    diagnostic = _diagnostics(tmp_path)

    with pytest.raises(StartupStepTimeout, match="database_restore"):
        asyncio.run(diagnostic.run_blocking(
            "database_restore", lambda: time.sleep(0.1), timeout_seconds=0.01
        ))

    payload = diagnostic.payload()
    assert payload["steps"][0]["error_type"] == "TimeoutError"
    assert "database_restore" in diagnostic.stack_path.read_text("utf-8")


def test_noncritical_source_failure_is_degraded_not_fatal(tmp_path: Path) -> None:
    diagnostic = _diagnostics(tmp_path)

    result = asyncio.run(diagnostic.run_blocking(
        "optional_news_source",
        lambda: (_ for _ in ()).throw(ConnectionError("offline")),
        timeout_seconds=1,
        critical=False,
    ))

    assert result is None
    assert diagnostic.payload()["steps"][0]["status"] == "FAILED"
    assert diagnostic.payload()["steps"][0]["error_type"] == "ConnectionError"


def test_runtime_start_reuses_already_validated_universe() -> None:
    runtime = V2Runtime.__new__(V2Runtime)
    runtime.settings = SimpleNamespace(v2_enabled=True)
    runtime.run_id = "run"
    runtime.started_at = datetime.now(timezone.utc)
    runtime.repository = SimpleNamespace(begin_v2_run=lambda *_: True)
    runtime.universe = SimpleNamespace(
        last_refresh_at=datetime.now(timezone.utc),
        refresh=lambda: pytest.fail("duplicate universe refresh"),
    )
    runtime._refresh_status_snapshot = lambda: None

    runtime.start()


def test_runtime_only_start_refreshes_when_snapshot_is_absent() -> None:
    called: list[bool] = []
    runtime = V2Runtime.__new__(V2Runtime)
    runtime.settings = SimpleNamespace(v2_enabled=True)
    runtime.run_id = "run"
    runtime.started_at = datetime.now(timezone.utc)
    runtime.repository = SimpleNamespace(begin_v2_run=lambda *_: True)
    runtime.universe = SimpleNamespace(
        last_refresh_at=None, refresh=lambda: called.append(True)
    )
    runtime._refresh_status_snapshot = lambda: None

    runtime.start()

    assert called == [True]


def test_universe_inspection_is_parallel_and_one_failure_is_isolated() -> None:
    class Client:
        def inspect_symbol(self, symbol: Symbol):
            time.sleep(0.04)
            if symbol == Symbol.ETHUSDT:
                raise ValueError("bad symbol")
            return UniverseInstrument(
                symbol=symbol, exists=True, status="Trading",
                category="linear", settle_coin="USDT",
                min_order_qty="0.001", qty_step="0.001",
                min_notional_value="5", min_leverage="1",
                max_leverage="100", leverage_step="0.01",
                tick_size="0.1",
                turnover_24h=10_000_000, spread_bps=1,
                bid_depth_usdt=50_000, ask_depth_usdt=50_000,
                market_timestamp=datetime.now(timezone.utc),
            )

    settings = SimpleNamespace(
        v2_universe_symbols=("BTCUSDT", "ETHUSDT", "SOLUSDT", "XRPUSDT"),
        v2_startup_universe_workers=4,
        v2_min_turnover_24h_usdt=1,
        v2_max_spread_bps=10,
        v2_min_orderbook_depth_usdt=1,
        v2_market_stale_seconds=30,
        v2_leverage_for_symbol=lambda _symbol: 1,
    )
    service = SymbolUniverseService(settings, Client())
    started = time.perf_counter()

    rows = service.refresh()

    assert time.perf_counter() - started < 0.13
    assert rows[Symbol.BTCUSDT].accepted is True
    assert rows[Symbol.ETHUSDT].accepted is False


class _FakeWebSocket:
    def __init__(self, stopped: list[str]) -> None:
        self.stopped = stopped

    async def run(self, _symbols):
        try:
            await asyncio.Event().wait()
        finally:
            self.stopped.append("websocket")

    def stop(self) -> None:
        self.stopped.append("websocket-stop")


def _install_lifespan_fakes(monkeypatch, tmp_path: Path, *, fail_start=False):
    import app.main as main

    stopped: list[str] = []
    settings = SimpleNamespace(
        v2_enabled=True,
        startup_hard_timeout_seconds=2,
        startup_step_timeout_seconds=1,
        allowed_symbols=("BTCUSDT",),
    )
    universe = SimpleNamespace(
        accepted_symbols=(Symbol.BTCUSDT,),
        refresh=lambda: None,
    )
    runtime = SimpleNamespace(
        start=(
            (lambda: (_ for _ in ()).throw(RuntimeError("db bootstrap failed")))
            if fail_start else (lambda: None)
        ),
        websocket=_FakeWebSocket(stopped),
    )

    async def forever():
        try:
            await asyncio.Event().wait()
        finally:
            stopped.append(asyncio.current_task().get_name())

    monkeypatch.setattr(main, "settings", settings)
    monkeypatch.setattr(main, "v2_universe_service", universe)
    monkeypatch.setattr(main, "v2_runtime", runtime)
    monkeypatch.setattr(
        main, "demo_execution_service", SimpleNamespace(enabled=False)
    )
    monkeypatch.setattr(
        main, "account_service",
        SimpleNamespace(refresh_if_stale=lambda **_: None),
    )
    monkeypatch.setattr(
        main, "persistence",
        SimpleNamespace(recoverable_demo_canary_jobs=lambda: []),
    )
    monkeypatch.setattr(main, "demo_client", None)
    monkeypatch.setattr(main, "news_polling_loop", forever)
    monkeypatch.setattr(main, "signal_recheck_loop", forever)
    monkeypatch.setattr(main, "v2_cycle_loop", lambda _runtime: forever())
    monkeypatch.setattr(main, "startup_diagnostics", _diagnostics(tmp_path))
    return main, stopped


def test_lifespan_schedules_loops_without_awaiting_them(
    monkeypatch, tmp_path: Path
) -> None:
    main, _ = _install_lifespan_fakes(monkeypatch, tmp_path)

    async def scenario() -> set[str]:
        async with main.lifespan(main.app):
            return {
                task.get_name() for task in asyncio.all_tasks()
                if not task.done()
            }

    names = asyncio.run(scenario())
    assert {"v2-public-websocket", "v2-cycle-loop",
            "news-polling-loop", "signal-recheck-loop"} <= names


def test_lifespan_shutdown_cancels_and_joins_every_background_task(
    monkeypatch, tmp_path: Path
) -> None:
    main, stopped = _install_lifespan_fakes(monkeypatch, tmp_path)

    async def scenario() -> None:
        async with main.lifespan(main.app):
            await asyncio.sleep(0)
        lingering = [
            task.get_name() for task in asyncio.all_tasks()
            if task.get_name() in {
                "v2-public-websocket", "v2-cycle-loop",
                "news-polling-loop", "signal-recheck-loop",
            } and not task.done()
        ]
        assert lingering == []

    asyncio.run(scenario())
    assert "websocket-stop" in stopped


def test_partial_startup_failure_creates_no_background_loop(
    monkeypatch, tmp_path: Path
) -> None:
    main, _ = _install_lifespan_fakes(
        monkeypatch, tmp_path, fail_start=True
    )

    async def scenario() -> list[str]:
        with pytest.raises(RuntimeError, match="db bootstrap failed"):
            async with main.lifespan(main.app):
                pass
        return [
            task.get_name() for task in asyncio.all_tasks()
            if task.get_name().endswith("-loop") and not task.done()
        ]

    assert asyncio.run(scenario()) == []
    step = main.startup_diagnostics.payload()["steps"][-1]
    assert step["name"] == "v2_runtime_start"
    assert step["status"] == "FAILED"


def test_repeated_lifespan_does_not_leave_duplicate_loops(
    monkeypatch, tmp_path: Path
) -> None:
    main, _ = _install_lifespan_fakes(monkeypatch, tmp_path)

    async def scenario() -> None:
        for _ in range(2):
            async with main.lifespan(main.app):
                names = [
                    task.get_name() for task in asyncio.all_tasks()
                    if task.get_name() == "v2-cycle-loop" and not task.done()
                ]
                assert names == ["v2-cycle-loop"]
            await asyncio.sleep(0)

    asyncio.run(scenario())


def test_startup_smoke_windows_helper_and_readiness_bound() -> None:
    script = Path("scripts/demo_v2_startup_smoke.ps1")
    source = script.read_text("utf-8")

    assert "WaitForExit(); $native.Refresh()" in source
    assert "FastAPI readiness exceeded 60 seconds" in source
    assert "BYBIT_DEMO_TRADING_ENABLED' 'false" in source
    assert "DEMO_ORDER_EXECUTION_AUTHORIZED' 'false" in source
    completed = subprocess.run(
        [
            "powershell", "-ExecutionPolicy", "Bypass",
            "-File", str(script), "-InternalTest",
        ],
        capture_output=True, text=True, timeout=15, check=False,
    )
    assert completed.returncode == 0, completed.stderr
    assert "INTERNAL PROCESS HELPERS: PASS" in completed.stdout


def test_startup_diagnostics_identifies_exact_failed_step(tmp_path: Path) -> None:
    diagnostic = _diagnostics(tmp_path)

    with pytest.raises(RuntimeError, match="preflight failed"):
        asyncio.run(diagnostic.run_blocking(
            "demo_account_preflight",
            lambda: (_ for _ in ()).throw(RuntimeError("preflight failed")),
            timeout_seconds=1,
        ))

    step = diagnostic.payload()["steps"][0]
    assert step["name"] == "demo_account_preflight"
    assert step["status"] == "FAILED"
    assert step["error_type"] == "RuntimeError"
