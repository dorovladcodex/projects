from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path
from uuid import uuid4

from app.db.persistence import PersistenceRepository
from app.models import (
    DemoExecutionRecord,
    DemoExecutionState,
    DemoFill,
    Side,
    Symbol,
)


def _execution(state: DemoExecutionState) -> DemoExecutionRecord:
    now = datetime.now(timezone.utc)
    return DemoExecutionRecord(
        candidate_id=uuid4(),
        run_id="demo-v2-20260724T083214398Z",
        order_link_id="bybot-terminalization-regression",
        order_id="entry-order",
        state=state,
        symbol=Symbol.ETHUSDT,
        side=Side.SELL,
        requested_quantity=Decimal("0.1"),
        accepted_quantity=Decimal("0.1"),
        average_fill_price=Decimal("1855.84"),
        take_profit=Decimal("1850.33"),
        stop_loss=Decimal("1860"),
        protection_confirmed=state == DemoExecutionState.DEMO_POSITION_OPEN,
        position_confirmed_at=(
            now if state == DemoExecutionState.DEMO_POSITION_OPEN else None
        ),
        created_at=now,
        updated_at=now,
    )


def test_late_entry_fill_cannot_regress_confirmed_position(
    tmp_path: Path,
) -> None:
    repository = PersistenceRepository(
        f"sqlite:///{tmp_path / 'terminalization.db'}"
    )
    opened = _execution(DemoExecutionState.DEMO_POSITION_OPEN)
    assert repository.save_demo_execution(
        opened, event_type="DEMO_POSITION_OPEN"
    )
    stale = opened.model_copy(deep=True)
    stale.state = DemoExecutionState.DEMO_PROTECTION_PENDING
    stale.protection_confirmed = False
    stale.fills = [DemoFill(
        execution_id="entry-exec",
        order_id="entry-order",
        quantity=Decimal("0.1"),
        price=Decimal("1855.84"),
        fee=Decimal("0.1020712"),
        executed_at=opened.created_at,
    )]
    stale.updated_at = datetime.now(timezone.utc)

    assert repository.save_demo_execution(stale, event_type="EXECUTION_FILL")

    restored = repository.load_demo_executions()[0]
    assert restored.state == DemoExecutionState.DEMO_POSITION_OPEN
    assert restored.protection_confirmed is True
    assert [fill.execution_id for fill in restored.fills] == ["entry-exec"]


def test_runner_preserves_management_after_bounded_drain_timeout() -> None:
    source = (
        Path(__file__).resolve().parents[1] / "scripts" / "demo_v2_soak.ps1"
    ).read_text(encoding="utf-8")

    assert "while (-not $drainComplete)" in source
    assert "while (-not $drainComplete -and -not $drainTimedOut)" not in source
    assert "position_management_continues = $true" in source
    assert "drain-timeout-incident.json" in source


def test_guarded_terminalization_canary_combines_transition_and_drain() -> None:
    root = Path(__file__).resolve().parents[1]
    wrapper = (
        root / "scripts" / "demo_v2_terminalization_canary.ps1"
    ).read_text(encoding="utf-8")
    base = (root / "scripts" / "bybit_demo_canary.ps1").read_text(
        encoding="utf-8"
    )

    assert "-AllowDemoOrders is required" in wrapper
    assert "-ExerciseFlatDuringProtectionRace" in wrapper
    assert "-EnterDrainBeforeFlatRace" in wrapper
    assert 'Path "/v2/stop-new-entries"' in base
    assert "DEMO DRAIN TERMINALIZATION: PASS" in base
