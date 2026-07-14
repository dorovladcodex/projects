from datetime import datetime, timedelta, timezone
from uuid import uuid4

from app.db.persistence import PersistenceRepository
from app.models import (
    Asset,
    CandidateLifecycleState,
    ClassificationStatus,
    ExecutionEnvironment,
    MarketConfirmation,
    NewsClassification,
    NewsItem,
    NewsSignalAction,
    NewsSignalCandidate,
    Sentiment,
    SignalDryRunResult,
    SignalRiskPreview,
    Symbol,
)


def _result(
    news: NewsItem,
    created_at: datetime,
    *,
    environment: ExecutionEnvironment,
    state: CandidateLifecycleState,
    run_id: str | None,
) -> SignalDryRunResult:
    return SignalDryRunResult(
        candidate=NewsSignalCandidate(
            news_id=news.id,
            execution_environment=environment,
            run_id=run_id,
            symbol=Symbol.BTCUSDT,
            state=state,
            proposed_action=NewsSignalAction.BUY,
            final_action=(
                NewsSignalAction.BUY
                if state == CandidateLifecycleState.READY
                else NewsSignalAction.NO_TRADE
            ),
            sentiment=Sentiment.BULLISH,
            classification_confidence=0.95,
            news_importance=0.9,
            category="etf",
            urgency="high",
            market_confirmation=MarketConfirmation(),
            expected_edge_bps=20,
            proposed_stop_loss_pct=0.5,
            proposed_take_profit_pct=1,
            ttl_seconds=300,
            created_at=created_at,
            expires_at=created_at + timedelta(minutes=5),
        ),
        risk_preview=SignalRiskPreview(),
    )


def _news(at: datetime, title: str) -> NewsItem:
    return NewsItem(
        id=uuid4(), title=title, summary="Bitcoin ETF update",
        source="test", published_at=at, received_at=at,
        asset_hint=Asset.BTC, importance=0.9,
    )


def test_demo_report_excludes_historical_paper_candidates(tmp_path) -> None:
    repository = PersistenceRepository(f"sqlite:///{tmp_path / 'report.db'}")
    before = datetime.now(timezone.utc) - timedelta(hours=1)
    old_news = _news(before, "Historical paper news")
    assert repository.save_news(old_news)
    repository.save_signal_result(_result(
        old_news, before, environment=ExecutionEnvironment.PAPER,
        state=CandidateLifecycleState.PAPER_CLOSED, run_id=None,
    ))

    started_at = datetime.now(timezone.utc)
    run_id = "demo-report-test"
    boundary = repository.begin_demo_soak_run(run_id, started_at)
    assert boundary is not None
    current_news = _news(started_at + timedelta(seconds=1), "Current Demo ETF news")
    assert repository.save_news(current_news)
    classification = NewsClassification(
        news_id=current_news.id, asset=Asset.BTC,
        sentiment=Sentiment.BULLISH, confidence=0.95,
        category="etf", urgency="high", reason="approval",
        model_name="mock", classification_status=ClassificationStatus.SUCCESS,
        trade_eligible=True,
        classified_at=started_at + timedelta(seconds=1),
    )
    repository.save_classification(
        current_news, classification, "mock-v1", started_at + timedelta(hours=1)
    )
    repository.save_signal_result(_result(
        current_news, started_at + timedelta(seconds=2),
        environment=ExecutionEnvironment.BYBIT_DEMO,
        state=CandidateLifecycleState.PENDING_CONFIRMATION, run_id=run_id,
    ))

    report = repository.demo_soak_report(run_id, finish=True)

    assert report is not None
    assert report["opening_state"]["preexisting_candidates"] == 1
    activity = report["activity_this_run"]
    assert activity["candidates_created_this_run"] == 1
    assert activity["candidate_final_states_this_run"] == {
        "PENDING_CONFIRMATION": 1
    }
    assert activity["classifications_this_run"] == 1
    assert activity["trade_eligible_classifications_this_run"] == 1
    assert report["current_run_candidates"][0]["news_title"] == "Current Demo ETF news"
    assert report["final_cumulative_state"]["cumulative_candidates"] == 2


def test_demo_run_boundary_is_idempotent_across_restart(tmp_path) -> None:
    database_url = f"sqlite:///{tmp_path / 'restart.db'}"
    started_at = datetime.now(timezone.utc)
    first = PersistenceRepository(database_url)
    original = first.begin_demo_soak_run("same-run", started_at)
    second = PersistenceRepository(database_url)
    restored = second.begin_demo_soak_run(
        "same-run", started_at + timedelta(minutes=10)
    )

    assert original is not None and restored is not None
    assert restored["started_at"] == original["started_at"]
    assert restored["opening_snapshot"] == original["opening_snapshot"]
