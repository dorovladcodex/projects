from pathlib import Path

import pytest

from app.bybit.demo_replay import DemoReplayFixture, DemoV2ReplayHarness
from app.models import DemoExecutionState


FIXTURES = Path(__file__).parent / "fixtures" / "demo_replay"


@pytest.mark.parametrize(
    "name,attribution",
    [
        ("ada_take_profit.json", "take_profit"),
        ("link_stop_loss.json", "stop_loss"),
        ("wif_stop_loss.json", "stop_loss"),
        ("eth_stop_loss.json", "stop_loss"),
    ],
)
def test_incident_replay_terminalizes_once_without_exchange_mutation(
    name: str, attribution: str,
) -> None:
    fixture = DemoReplayFixture.load(FIXTURES / name)
    result = DemoV2ReplayHarness(fixture).run()

    assert result.record.state == DemoExecutionState.DEMO_CLOSED
    assert result.record.exit_attribution == attribution
    assert result.completed_trades == 1
    assert result.open_count == 0
    assert result.reservation_release_count == 1
    assert result.cooldown_update_count == 1
    assert result.unresolved_execution_ids == []
    assert result.terminal_event_count == 1
    assert result.exchange_mutation_attempts == 0


def test_sparse_lifecycle_event_cannot_erase_exact_entry_identity() -> None:
    fixture = DemoReplayFixture.load(FIXTURES / "ada_take_profit.json")
    assert fixture.sparse_entry_fill_order_id is True

    result = DemoV2ReplayHarness(fixture).run()

    assert result.record.order_id == fixture.entry_order_id
    assert result.record.order_link_id == fixture.order_link_id
