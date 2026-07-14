from __future__ import annotations

from datetime import datetime, timedelta, timezone
from decimal import Decimal

from sqlalchemy.orm import Session

from app.db.persistence import (
    DemoExecutionRow,
    PaperExecutionRow,
    PersistenceRepository,
    SignalCandidateRow,
)
from app.models import (
    CandidateLifecycleState,
    DemoExecutionRecord,
    DemoExecutionState,
    ExecutionEnvironment,
    MarketConfirmation,
    NewsItem,
    NewsSignalAction,
    NewsSignalCandidate,
    Sentiment,
    Side,
    SignalDryRunResult,
    SignalRiskPreview,
    Symbol,
)


def _result(item: NewsItem, environment: ExecutionEnvironment) -> SignalDryRunResult:
    candidate = NewsSignalCandidate(
        news_id=item.id,
        execution_environment=environment,
        symbol=Symbol.BTCUSDT,
        state=CandidateLifecycleState.READY,
        proposed_action=NewsSignalAction.BUY,
        final_action=NewsSignalAction.BUY,
        sentiment=Sentiment.BULLISH,
        classification_confidence=0.9,
        news_importance=0.9,
        category="etf",
        urgency="high",
        market_confirmation=MarketConfirmation(
            available=True, fresh=True, direction_confirmed=True
        ),
        expected_edge_bps=25,
        proposed_stop_loss_pct=1,
        proposed_take_profit_pct=2,
        ttl_seconds=300,
        expires_at=datetime.now(timezone.utc) + timedelta(minutes=5),
    )
    return SignalDryRunResult(
        candidate=candidate,
        risk_preview=SignalRiskPreview(preview_performed=False),
    )


def _news(suffix: str) -> NewsItem:
    return NewsItem(
        title=f"SEC approves BTC ETF {suffix}",
        summary="A complete test news summary.",
        source="test",
        url=f"https://example.test/{suffix}",
        published_at=datetime.now(timezone.utc),
    )


def test_paper_candidate_and_execution_are_durably_scoped(tmp_path) -> None:
    repository = PersistenceRepository(f"sqlite:///{tmp_path / 'paper.db'}")
    item = _news("paper")
    assert repository.save_news(item)
    result = _result(item, ExecutionEnvironment.PAPER)
    repository.save_signal_result(result)
    repository.reserve_paper_execution(str(result.candidate.id), None)

    with Session(repository.engine) as session:
        candidate = session.get(SignalCandidateRow, str(result.candidate.id))
        execution = session.query(PaperExecutionRow).one()
        assert candidate.execution_environment == "PAPER"
        assert execution.execution_environment == "PAPER"

    restored = repository.load_signal_results()[0]
    assert restored.candidate.execution_environment == ExecutionEnvironment.PAPER


def test_demo_candidate_and_execution_are_durably_scoped(tmp_path) -> None:
    repository = PersistenceRepository(f"sqlite:///{tmp_path / 'demo.db'}")
    item = _news("demo")
    assert repository.save_news(item)
    result = _result(item, ExecutionEnvironment.BYBIT_DEMO)
    repository.save_signal_result(result)
    execution = DemoExecutionRecord(
        candidate_id=result.candidate.id,
        run_id="demo-run-test",
        order_link_id="bybot-demo-test-entry",
        state=DemoExecutionState.DEMO_SUBMITTING,
        symbol=Symbol.BTCUSDT,
        side=Side.BUY,
        requested_quantity=Decimal("0.001"),
    )
    assert repository.reserve_demo_execution(execution) is not None

    with Session(repository.engine) as session:
        candidate = session.get(SignalCandidateRow, str(result.candidate.id))
        durable_execution = session.query(DemoExecutionRow).one()
        assert candidate.execution_environment == "BYBIT_DEMO"
        assert durable_execution.execution_environment == "BYBIT_DEMO"

    restored = repository.load_signal_results()[0]
    assert (
        restored.candidate.execution_environment
        == ExecutionEnvironment.BYBIT_DEMO
    )


def test_durable_environment_overrides_legacy_payload_default(tmp_path) -> None:
    repository = PersistenceRepository(f"sqlite:///{tmp_path / 'legacy.db'}")
    item = _news("legacy")
    assert repository.save_news(item)
    result = _result(item, ExecutionEnvironment.BYBIT_DEMO)
    repository.save_signal_result(result)

    with Session(repository.engine) as session:
        row = session.get(SignalCandidateRow, str(result.candidate.id))
        payload = dict(row.payload)
        payload.pop("execution_environment", None)
        row.payload = payload
        session.commit()

    restored = repository.load_signal_results()[0]
    assert (
        restored.candidate.execution_environment
        == ExecutionEnvironment.BYBIT_DEMO
    )
