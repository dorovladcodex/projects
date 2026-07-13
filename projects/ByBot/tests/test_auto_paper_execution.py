from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import delete
from sqlalchemy.orm import Session

from app.models import (
    CandidateLifecycleState,
    MarketConfirmation,
    MarketSnapshot,
    NewsSignalAction,
    NewsSignalCandidate,
    Sentiment,
    SignalRiskPreview,
    Side,
    SimpleTrend,
    Symbol,
)
from app.portfolio.paper_trading import PaperTradingService
from app.bybit.market_data import MarketDataService
from app.bybit.private import build_account_service
from app.db.persistence import PersistenceRepository, RiskDecisionRow, SignalCandidateRow
from app.models import NewsItem
from app.news.service import NewsService
from app.signals.service import SignalCandidateService
from tests.test_codex_cli_classifier import FakeRunner, VALID_RESPONSE
from tests.test_pipeline_persistence import BullishMarket, classifier, settings
from tests.test_signal_candidates import pipeline


def candidate(side: Side = Side.BUY) -> NewsSignalCandidate:
    return NewsSignalCandidate(
        news_id="00000000-0000-0000-0000-000000000001",
        symbol=Symbol.BTCUSDT,
        state=CandidateLifecycleState.READY,
        proposed_action=(
            NewsSignalAction.BUY if side == Side.BUY else NewsSignalAction.SELL
        ),
        final_action=(
            NewsSignalAction.BUY if side == Side.BUY else NewsSignalAction.SELL
        ),
        sentiment=Sentiment.BULLISH if side == Side.BUY else Sentiment.BEARISH,
        classification_confidence=0.9,
        news_importance=0.9,
        category="etf",
        urgency="high",
        market_confirmation=MarketConfirmation(
            available=True, fresh=True, direction_confirmed=True
        ),
        expected_edge_bps=30,
        proposed_stop_loss_pct=1,
        proposed_take_profit_pct=2,
        ttl_seconds=300,
        expires_at=datetime.now(timezone.utc) + timedelta(minutes=5),
    )


def preview(*, approved: bool = True) -> SignalRiskPreview:
    return SignalRiskPreview(
        preview_performed=True,
        approved=approved,
        capped_size=1.0,
        position_notional=100,
        max_allowed_notional=500,
        risk_decision_id=1,
    )


def snapshot(price: float = 100) -> MarketSnapshot:
    return MarketSnapshot(
        symbol=Symbol.BTCUSDT,
        timestamp=datetime.now(timezone.utc),
        last_price=price,
        bid_price=price - 0.01,
        ask_price=price + 0.01,
        price_change_1m_pct=0.5,
        simple_trend=SimpleTrend.BULLISH,
        trend_score=1,
        volatility_pct=0.1,
        liquidity_ok=True,
    )


def test_ready_approved_candidate_opens_once() -> None:
    service = PaperTradingService(starting_equity=10_000)
    item = candidate()

    opened = service.open_from_candidate(
        item, preview(), snapshot(), taker_fee_bps=6, slippage_bps=2
    )
    duplicate = service.open_from_candidate(
        item, preview(), snapshot(), taker_fee_bps=6, slippage_bps=2
    )

    assert opened is not None
    assert duplicate is opened
    assert len(service.positions) == 1
    assert service.paper_positions_opened == 1
    assert service.paper_execution_duplicates_blocked == 1
    assert service.last_execution_duplicate is True
    assert service.last_position_opened is False


def test_rejected_risk_opens_nothing() -> None:
    service = PaperTradingService()

    opened = service.open_from_candidate(
        candidate(), preview(approved=False), snapshot(),
        taker_fee_bps=6, slippage_bps=2,
    )

    assert opened is None
    assert service.open_position is None
    assert service.paper_execution_risk_blocked == 1


@pytest.mark.parametrize("side,exit_price,expected_gross", [
    (Side.BUY, 110.0, 9.99),
    (Side.SELL, 90.0, 9.99),
])
def test_buy_sell_net_pnl_costs_and_equity(
    side: Side, exit_price: float, expected_gross: float
) -> None:
    service = PaperTradingService(starting_equity=10_000)
    position = service.open_from_candidate(
        candidate(side), preview(), snapshot(), taker_fee_bps=6, slippage_bps=2
    )
    assert position is not None

    closed = service.close_position(exit_price, reason="manual_close")
    expected_fees = position.entry_price * 0.0006 + exit_price * 0.0006
    expected_slippage = position.entry_price * 0.0002 + exit_price * 0.0002

    assert closed.gross_pnl == pytest.approx(expected_gross)
    assert closed.fees_paid == pytest.approx(expected_fees)
    assert closed.slippage_paid == pytest.approx(expected_slippage)
    assert closed.realized_pnl == pytest.approx(
        expected_gross - expected_fees - expected_slippage
    )
    assert service.equity == pytest.approx(10_000 + closed.realized_pnl)
    assert service.as_status()["paper_fees_paid"] == pytest.approx(expected_fees)


def test_losing_buy_updates_equity_and_duplicate_close_does_not_credit_twice() -> None:
    service = PaperTradingService(starting_equity=10_000)
    position = service.open_from_candidate(
        candidate(Side.BUY), preview(), snapshot(), taker_fee_bps=6, slippage_bps=2
    )
    assert position is not None

    closed = service.close_position(90.0, reason="manual_close")
    equity_after_first_close = service.equity
    fees_after_first_close = service.fees_paid

    assert closed.realized_pnl < 0
    assert equity_after_first_close == pytest.approx(10_000 + closed.realized_pnl)
    with pytest.raises(RuntimeError, match="no open paper position"):
        service.close_position(90.0, reason="manual_close")
    assert service.equity == pytest.approx(equity_after_first_close)
    assert service.fees_paid == pytest.approx(fees_after_first_close)
    assert len(service.closed_trades) == 1


def test_equity_is_starting_plus_realized_and_current_unrealized_pnl() -> None:
    service = PaperTradingService(starting_equity=10_000)
    position = service.open_from_candidate(
        candidate(Side.BUY), preview(), snapshot(), taker_fee_bps=6, slippage_bps=2
    )
    assert position is not None

    service.update_from_market(snapshot(101.0))
    pnl = service.pnl()

    assert pnl.starting_equity == pytest.approx(10_000)
    assert pnl.realized_pnl == 0
    assert pnl.unrealized_pnl == pytest.approx(service.open_position.unrealized_pnl)
    assert pnl.equity == pytest.approx(
        pnl.starting_equity + pnl.realized_pnl + pnl.unrealized_pnl
    )
    assert pnl.total_pnl == pytest.approx(pnl.realized_pnl + pnl.unrealized_pnl)


def test_stop_loss_take_profit_and_timeout_monitoring() -> None:
    stop_service = PaperTradingService()
    stopped = stop_service.open_from_candidate(
        candidate(), preview(), snapshot(), taker_fee_bps=6, slippage_bps=2
    )
    assert stopped is not None
    stop_service.update_from_market(snapshot(stopped.stop_loss - 0.01))
    assert stop_service.closed_trades[0].reason == "stop_loss"

    take_service = PaperTradingService()
    taken = take_service.open_from_candidate(
        candidate(), preview(), snapshot(), taker_fee_bps=6, slippage_bps=2
    )
    assert taken is not None
    take_service.update_from_market(snapshot(taken.take_profit + 0.01))
    assert take_service.closed_trades[0].reason == "take_profit"

    timeout_service = PaperTradingService(timeout=timedelta(seconds=1))
    timed = timeout_service.open_from_candidate(
        candidate(), preview(), snapshot(), taker_fee_bps=6, slippage_bps=2
    )
    assert timed is not None
    timeout_service.update_from_market(
        snapshot(timed.entry_price), now=timed.opened_at + timedelta(seconds=2)
    )
    assert timeout_service.closed_trades[0].reason == "timeout"


def test_signal_execution_blocks_expired_and_stale_candidates() -> None:
    signals, paper, classification = pipeline(
        "SEC approves spot Bitcoin ETF from BlackRock",
        "ETF approval is confirmed.",
    )
    result = signals.process_news_id(classification.news_id)[0]
    result.candidate.state = CandidateLifecycleState.READY
    result.candidate.final_action = NewsSignalAction.BUY
    result.risk_preview = preview()
    result.candidate.expires_at = datetime.now(timezone.utc) - timedelta(seconds=1)
    signals.execute_ready_candidate(result.candidate.id, force=True)
    assert paper.open_position is None
    assert result.candidate.state == CandidateLifecycleState.EXECUTION_BLOCKED

    stale_signals, stale_paper, stale_classification = pipeline(
        "SEC approves spot Bitcoin ETF from BlackRock",
        "ETF approval is confirmed.",
    )
    stale_result = stale_signals.process_news_id(stale_classification.news_id)[0]
    stale_result.candidate.state = CandidateLifecycleState.READY
    stale_result.candidate.final_action = NewsSignalAction.BUY
    stale_result.risk_preview = preview()
    stale_signals._evaluation_snapshots[stale_result.candidate.id] = snapshot().model_copy(
        update={"timestamp": datetime.now(timezone.utc) - timedelta(minutes=5)}
    )
    stale_signals.execute_ready_candidate(stale_result.candidate.id, force=True)
    assert stale_paper.open_position is None
    assert stale_result.candidate.state == CandidateLifecycleState.EXECUTION_BLOCKED


def test_automatic_execution_and_restart_are_idempotent(tmp_path) -> None:
    database_url = f"sqlite:///{tmp_path / 'auto-paper.db'}"
    config = settings(database_url)
    config.auto_paper_execution = True
    repository = PersistenceRepository(database_url)
    news = NewsService(
        [], classifier(config, FakeRunner([VALID_RESPONSE])),
        max_item_age=timedelta(hours=1), repository=repository,
    )
    accepted, _, classified = news.ingest(NewsItem(
        title="SEC investigation examines Bitcoin market structure",
        summary="Material BTC institutional market investigation.", source="rss",
        url="https://example.test/auto-paper",
        published_at=datetime.now(timezone.utc),
    ))
    assert accepted and classified is not None
    market = MarketDataService(BullishMarket(), [Symbol.BTCUSDT])
    market.refresh_all()
    market.refresh_all()
    paper = PaperTradingService(
        starting_equity=10_000, repository=repository
    )
    signals = SignalCandidateService(
        config, news, market, build_account_service(config), paper, repository,
    )

    result = signals.process_news_id(classified.news_id)[0]

    assert result.candidate.state == CandidateLifecycleState.PAPER_OPENED
    assert paper.open_position is not None
    assert len(paper.positions) == 1

    restarted_repository = PersistenceRepository(database_url)
    restarted_news = NewsService(
        [], classifier(config, FakeRunner([])), max_item_age=timedelta(hours=1),
        repository=restarted_repository,
    )
    restarted_news.restore()
    restarted_paper = PaperTradingService(
        starting_equity=10_000, repository=restarted_repository
    )
    restarted_paper.restore()
    restarted_signals = SignalCandidateService(
        config, restarted_news, market, build_account_service(config),
        restarted_paper, restarted_repository,
    )
    restarted_signals.restore()
    restarted_signals.process_pending()

    assert len(restarted_paper.positions) == 1
    assert restarted_paper.open_position is not None
    assert restarted_signals.candidates[0].state == CandidateLifecycleState.PAPER_OPENED

    closed = restarted_paper.close_position(
        restarted_paper.open_position.take_profit + 0.01,
        reason="take_profit",
    )
    restarted_signals.sync_paper_states()
    expected_equity = 10_000 + closed.realized_pnl
    assert restarted_paper.equity == pytest.approx(expected_equity)

    duplicate = restarted_repository.persist_paper_close_transaction(closed, 10_000)
    assert duplicate["status"] == "EXISTING"
    assert restarted_paper.equity == pytest.approx(expected_equity)

    closed_repository = PersistenceRepository(database_url)
    closed_paper = PaperTradingService(
        starting_equity=1.0, repository=closed_repository
    )
    closed_paper.restore()
    closed_news = NewsService(
        [], classifier(config, FakeRunner([])), max_item_age=timedelta(hours=1),
        repository=closed_repository,
    )
    closed_news.restore()
    closed_signals = SignalCandidateService(
        config, closed_news, market, build_account_service(config),
        closed_paper, closed_repository,
    )
    closed_signals.restore()

    assert closed_paper.starting_equity == pytest.approx(10_000)
    assert closed_paper.realized_pnl == pytest.approx(closed.realized_pnl)
    assert closed_paper.fees_paid == pytest.approx(closed.fees_paid)
    assert closed_paper.equity == pytest.approx(expected_equity)
    assert closed_paper.open_position is None
    assert len(closed_paper.closed_trades) == 1
    assert closed_signals.candidates[0].state == CandidateLifecycleState.PAPER_CLOSED


def test_auto_disabled_creates_no_reservation_then_first_manual_execution_opens(
    tmp_path,
) -> None:
    database_url = f"sqlite:///{tmp_path / 'manual-paper.db'}"
    config = settings(database_url)
    assert config.auto_paper_execution is False
    repository = PersistenceRepository(database_url)
    news = NewsService(
        [], classifier(config, FakeRunner([VALID_RESPONSE])),
        max_item_age=timedelta(hours=1), repository=repository,
    )
    accepted, _, classified = news.ingest(NewsItem(
        title="SEC investigation examines Bitcoin market structure",
        summary="Material BTC institutional market investigation.", source="rss",
        url="https://example.test/manual-paper",
        published_at=datetime.now(timezone.utc),
    ))
    assert accepted and classified is not None
    market = MarketDataService(BullishMarket(), [Symbol.BTCUSDT])
    market.refresh_all()
    market.refresh_all()
    paper = PaperTradingService(starting_equity=10_000, repository=repository)
    signals = SignalCandidateService(
        config, news, market, build_account_service(config), paper, repository,
    )
    result = signals.process_news_id(classified.news_id)[0]

    assert result.candidate.state == CandidateLifecycleState.READY
    assert repository.paper_execution_details(str(result.candidate.id)) is None
    assert paper.open_position is None
    with Session(repository.engine) as session:
        session.execute(delete(RiskDecisionRow).where(
            RiskDecisionRow.candidate_id == str(result.candidate.id)
        ))
        session.commit()
    result.risk_preview.risk_decision_id = None
    repository.save_signal_result(result)

    first = signals.execute_ready_candidate(result.candidate.id, force=True)
    first_position = paper.open_position
    assert first.candidate.state == CandidateLifecycleState.PAPER_OPENED
    assert first.candidate.final_action == NewsSignalAction.BUY
    assert first.execution_attempted is True
    assert first.paper_position_opened is True
    assert first.risk_preview.risk_decision_id is not None
    assert first_position is not None
    assert first_position.risk_decision_id == first.risk_preview.risk_decision_id
    execution = repository.paper_execution_details(str(result.candidate.id))
    assert execution is not None
    assert execution["state"] == "PAPER_OPENED"
    with Session(repository.engine) as session:
        candidate_row = session.get(SignalCandidateRow, str(result.candidate.id))
        assert candidate_row is not None
        assert candidate_row.risk_decision_id == first.risk_preview.risk_decision_id
    assert len(paper.positions) == 1

    second = signals.execute_ready_candidate(result.candidate.id, force=True)
    assert second.candidate.state == CandidateLifecycleState.PAPER_OPENED
    assert second.candidate.final_action == NewsSignalAction.BUY
    assert second.execution_attempted is False
    assert second.paper_position_opened is False
    assert paper.open_position is first_position
    assert len(paper.positions) == 1


def test_orphaned_failed_reservation_is_resumed(tmp_path) -> None:
    database_url = f"sqlite:///{tmp_path / 'orphan.db'}"
    repository = PersistenceRepository(database_url)
    item = candidate()
    with Session(repository.engine) as session:
        session.add(SignalCandidateRow(
            id=str(item.id), news_id=str(item.news_id), symbol=item.symbol.value,
            state=item.state.value, active=False, expires_at=item.expires_at,
            payload=item.model_dump(mode="json"),
            risk_preview=preview().model_dump(mode="json"),
            risk_decision_id=None,
        ))
        session.commit()
    reservation = repository.reserve_paper_execution(str(item.id), 1)
    assert reservation is not None
    service = PaperTradingService(repository=repository)
    service.restore()

    opened = service.open_from_candidate(
        item, preview(), snapshot(), taker_fee_bps=6, slippage_bps=2
    )

    assert opened is not None
    assert service.open_position is opened
    assert len(service.positions) == 1
    details = repository.paper_execution_details(str(item.id))
    assert details is not None
    assert details["state"] == "PAPER_OPENED"


def test_persistence_failure_reports_sanitized_exception_type(
    tmp_path, caplog: pytest.LogCaptureFixture
) -> None:
    database_url = f"sqlite:///{tmp_path / 'diagnostic.db'}"
    repository = PersistenceRepository(database_url)

    result = repository.persist_paper_open_transaction(
        str(candidate().id), preview().model_copy(update={"risk_decision_id": None}),
        PaperTradingService().open_from_candidate(
            candidate(), preview(), snapshot(), taker_fee_bps=6, slippage_bps=2
        ),
    )

    assert result["status"] == "ERROR"
    assert result["error_code"] == "DB_VALUEERROR"
    assert "type=ValueError" in caplog.text
    assert "postgresql://" not in caplog.text
