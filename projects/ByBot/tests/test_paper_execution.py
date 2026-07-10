import pytest

from app.config import BotMode
from app.models import RiskDecision
from app.portfolio.paper import PaperExecutionEngine
from tests.test_risk_manager import valid_market, valid_signal


def test_paper_engine_sizes_position_from_approved_max_loss() -> None:
    engine = PaperExecutionEngine(BotMode.PAPER)
    risk = RiskDecision(approved=True, max_loss_amount=50)

    order = engine.execute(valid_signal(), risk, valid_market())

    stop_distance = abs(order.fill_price - order.stop_loss_price)
    assert order.quantity * stop_distance == pytest.approx(50)
    assert engine.position is not None
