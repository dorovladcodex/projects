from __future__ import annotations

from datetime import datetime, timedelta, timezone
import os
from uuid import uuid4

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import create_engine, inspect, select
from sqlalchemy.sql.sqltypes import Integer
from sqlalchemy.orm import Session

from app.db.persistence import (
    Base,
    NewsItemRow,
    PaperAccountRow,
    PaperExecutionRow,
    PaperPositionRow,
    PaperRiskStateRow,
    PaperTradeRow,
    PersistenceRepository,
    RiskDecisionRow,
    SignalCandidateRow,
)
from app.models import (
    MarketSnapshot,
    PaperPosition,
    PositionStatus,
    Side,
    SignalRiskPreview,
    SimpleTrend,
    Symbol,
)
from app.db.url import normalize_database_url
from app.config import get_settings
from app.portfolio.paper_trading import PaperTradingService


POSTGRES_TEST_URL = os.getenv("BYBOT_TEST_POSTGRES_URL")
pytestmark = pytest.mark.skipif(
    not POSTGRES_TEST_URL,
    reason="set BYBOT_TEST_POSTGRES_URL to run PostgreSQL transaction tests",
)


def _seed_candidate(
    repository: PersistenceRepository, symbol: Symbol = Symbol.BTCUSDT
) -> tuple[str, str]:
    news_id, candidate_id = str(uuid4()), str(uuid4())
    now = datetime.now(timezone.utc)
    with Session(repository.engine) as session:
        session.add(NewsItemRow(
            id=news_id, normalized_url=f"https://example.invalid/{news_id}",
            content_hash=uuid4().hex + uuid4().hex,
            payload={"id": news_id}, received_at=now,
        ))
        session.add(SignalCandidateRow(
            id=candidate_id, news_id=news_id, symbol=symbol.value, state="READY",
            active=False, expires_at=now + timedelta(minutes=5), payload={},
            risk_preview={}, risk_decision_id=None,
        ))
        session.commit()
    return news_id, candidate_id


def _position(
    candidate_id: str,
    *,
    position_id: str | None = None,
    side: Side = Side.BUY,
    symbol: Symbol = Symbol.BTCUSDT,
) -> PaperPosition:
    return PaperPosition(
        id=position_id or uuid4(), symbol=symbol, side=side,
        size=0.1, entry_price=100, current_price=100, stop_loss=99,
        take_profit=102, status=PositionStatus.OPEN,
        candidate_id=candidate_id, position_notional=10,
        estimated_entry_fee=0.006, estimated_exit_fee=0.006,
        estimated_entry_slippage=0.002, estimated_exit_slippage=0.002,
        fees_paid=0.006, slippage_paid=0.002,
    )


def _preview() -> SignalRiskPreview:
    return SignalRiskPreview(
        preview_performed=True, approved=True, capped_size=0.1,
        position_notional=10, max_allowed_notional=500,
        estimated_fees=0.012, estimated_slippage=0.004,
        rejection_reasons=[], risk_decision_id=None,
    )


def _fresh_repository() -> PersistenceRepository:
    repository = PersistenceRepository(str(POSTGRES_TEST_URL), create_schema=True)
    assert repository.available
    Base.metadata.drop_all(repository.engine)
    Base.metadata.create_all(repository.engine)
    return repository


def _open_accounting_position(
    side: Side,
) -> tuple[PersistenceRepository, PaperTradingService, PaperPosition]:
    repository = _fresh_repository()
    _, candidate_id = _seed_candidate(repository)
    position = _position(candidate_id, side=side)
    opened = repository.persist_paper_open_transaction(
        candidate_id, _preview(), position
    )
    assert opened["status"] == "OPENED"
    service = PaperTradingService(starting_equity=10_000, repository=repository)
    service.restore()
    assert service.open_position is not None
    return repository, service, service.open_position


def test_postgres_alembic_upgrade_from_0001_uses_integer_risk_fk() -> None:
    url = normalize_database_url(str(POSTGRES_TEST_URL))
    config = Config("alembic.ini")
    config.set_main_option("sqlalchemy.url", url.replace("%", "%%"))
    # This variable is read by application Settings inside alembic/env.py.
    previous = os.environ.get("DATABASE_URL")
    os.environ["DATABASE_URL"] = url
    get_settings.cache_clear()
    try:
        command.downgrade(config, "base")
        command.upgrade(config, "20260713_0001")
        engine = create_engine(url)
        # Revision 0001 historically used live metadata. Remove this leaked table
        # to reproduce an actual database stamped at 0001 before revision 0002.
        with engine.begin() as connection:
            connection.exec_driver_sql("DROP TABLE IF EXISTS paper_executions")
        command.upgrade(config, "head")
        command.upgrade(config, "head")  # rerun is a safe no-op
        inspector = inspect(engine)
        assert "paper_accounts" in inspector.get_table_names()
        columns = {column["name"]: column for column in inspector.get_columns("paper_executions")}
        assert isinstance(columns["risk_decision_id"]["type"], Integer)
        risk_id = {column["name"]: column for column in inspector.get_columns("risk_decisions")}["id"]
        assert isinstance(risk_id["type"], Integer)
        foreign_keys = inspector.get_foreign_keys("paper_executions")
        assert any(
            fk["constrained_columns"] == ["risk_decision_id"]
            and fk["referred_table"] == "risk_decisions"
            and fk["referred_columns"] == ["id"]
            for fk in foreign_keys
        )
    finally:
        if previous is None:
            os.environ.pop("DATABASE_URL", None)
        else:
            os.environ["DATABASE_URL"] = previous
        get_settings.cache_clear()


def test_postgres_atomic_open_returns_integer_id_and_is_idempotent() -> None:
    repository = PersistenceRepository(str(POSTGRES_TEST_URL), create_schema=True)
    assert repository.available
    _, candidate_id = _seed_candidate(repository)
    preview = _preview()
    position = _position(candidate_id)

    first = repository.persist_paper_open_transaction(candidate_id, preview, position)
    second = repository.persist_paper_open_transaction(candidate_id, preview, _position(candidate_id))

    assert first["status"] == "OPENED"
    assert isinstance(first["risk_decision_id"], int)
    assert position.risk_decision_id == first["risk_decision_id"]
    assert second["status"] == "EXISTING"
    with Session(repository.engine) as session:
        risk = session.get(RiskDecisionRow, int(first["risk_decision_id"]))
        stored_position = session.get(PaperPositionRow, str(position.id))
        assert risk is not None and risk.approved is True
        assert risk.capped_size == pytest.approx(0.1)
        assert risk.rejection_reasons == []
        assert stored_position is not None
        assert stored_position.payload["risk_decision_id"] == first["risk_decision_id"]


def test_postgres_atomic_failure_rolls_back_risk_and_execution() -> None:
    repository = _fresh_repository()
    _, candidate_id = _seed_candidate(repository)
    duplicate_position_id = str(uuid4())
    with Session(repository.engine) as session:
        session.add(PaperPositionRow(
            id=duplicate_position_id, status="CLOSED", payload={"preexisting": True}
        ))
        session.commit()

    result = repository.persist_paper_open_transaction(
        candidate_id, _preview(), _position(candidate_id, position_id=duplicate_position_id)
    )

    assert result["status"] == "ERROR"
    assert result["error_code"] == "DB_INTEGRITY_ERROR"
    with Session(repository.engine) as session:
        assert session.scalar(select(RiskDecisionRow).where(
            RiskDecisionRow.candidate_id == candidate_id
        )) is None
        assert session.scalar(select(PaperExecutionRow).where(
            PaperExecutionRow.candidate_id == candidate_id
        )) is None


@pytest.mark.parametrize(
    "side,exit_price,expected_sign",
    [
        (Side.BUY, 110.0, 1),
        (Side.BUY, 90.0, -1),
        (Side.SELL, 90.0, 1),
    ],
)
def test_postgres_close_updates_authoritative_paper_equity(
    side: Side, exit_price: float, expected_sign: int
) -> None:
    repository, service, _ = _open_accounting_position(side)

    closed = service.close_position(exit_price, reason="manual_close")
    pnl = service.pnl()

    assert closed.realized_pnl * expected_sign > 0
    assert pnl.equity == pytest.approx(10_000 + closed.realized_pnl)
    assert pnl.equity == pytest.approx(
        pnl.starting_equity + pnl.realized_pnl + pnl.unrealized_pnl
    )
    with Session(repository.engine) as session:
        account = session.get(PaperAccountRow, 1)
        stored_position = session.get(PaperPositionRow, str(closed.id))
        trade = session.get(PaperTradeRow, str(closed.id))
        candidate = session.get(SignalCandidateRow, str(closed.candidate_id))
        execution = session.scalar(select(PaperExecutionRow).where(
            PaperExecutionRow.candidate_id == str(closed.candidate_id)
        ))
        assert account is not None
        assert account.realized_pnl == pytest.approx(closed.realized_pnl)
        assert account.fees_paid == pytest.approx(closed.fees_paid)
        assert account.equity == pytest.approx(10_000 + closed.realized_pnl)
        assert stored_position is not None and stored_position.status == "CLOSED"
        assert stored_position.payload["close_reason"] == "manual_close"
        assert trade is not None
        assert candidate is not None and candidate.state == "PAPER_CLOSED"
        assert execution is not None and execution.state == "PAPER_CLOSED"


def test_postgres_duplicate_close_does_not_double_credit_or_charge() -> None:
    repository, service, _ = _open_accounting_position(Side.BUY)
    closed = service.close_position(110.0, reason="manual_close")
    account_before = repository.load_or_create_paper_account(10_000)
    assert account_before is not None

    duplicate = repository.persist_paper_close_transaction(closed, 10_000)
    with pytest.raises(RuntimeError, match="no open paper position"):
        service.close_position(110.0, reason="manual_close")
    account_after = repository.load_or_create_paper_account(10_000)

    assert duplicate["status"] == "EXISTING"
    assert account_after == pytest.approx(account_before)
    with Session(repository.engine) as session:
        assert len(session.scalars(select(PaperTradeRow)).all()) == 1
        account = session.get(PaperAccountRow, 1)
        assert account is not None
        assert account.realized_pnl == pytest.approx(closed.realized_pnl)
        assert account.fees_paid == pytest.approx(closed.fees_paid)


def test_postgres_restart_restores_equity_realized_pnl_and_fees() -> None:
    repository, service, _ = _open_accounting_position(Side.SELL)
    closed = service.close_position(90.0, reason="take_profit")
    expected = service.pnl()

    restarted_repository = PersistenceRepository(
        str(POSTGRES_TEST_URL), create_schema=True
    )
    restarted = PaperTradingService(
        starting_equity=123.0, repository=restarted_repository
    )
    restarted.restore()
    actual = restarted.pnl()

    assert actual.starting_equity == pytest.approx(10_000)
    assert actual.realized_pnl == pytest.approx(closed.realized_pnl)
    assert actual.fees_paid == pytest.approx(closed.fees_paid)
    assert actual.equity == pytest.approx(expected.equity)
    assert actual.open_positions == 0
    assert actual.closed_trades == 1


def test_postgres_restart_restores_open_unrealized_equity_formula() -> None:
    repository, service, _ = _open_accounting_position(Side.BUY)
    market = MarketSnapshot(
        symbol=Symbol.BTCUSDT,
        timestamp=datetime.now(timezone.utc),
        last_price=101.0,
        bid_price=100.99,
        ask_price=101.01,
        price_change_1m_pct=0.1,
        simple_trend=SimpleTrend.BULLISH,
        trend_score=1,
        volatility_pct=0.1,
        liquidity_ok=True,
    )
    service.update_from_market(market)
    expected = service.pnl()
    assert expected.open_positions == 1
    assert expected.equity == pytest.approx(
        expected.starting_equity + expected.realized_pnl + expected.unrealized_pnl
    )

    restarted = PaperTradingService(
        starting_equity=1.0,
        repository=PersistenceRepository(str(POSTGRES_TEST_URL), create_schema=True),
    )
    restarted.restore()
    actual = restarted.pnl()

    assert actual.starting_equity == pytest.approx(10_000)
    assert actual.realized_pnl == pytest.approx(0)
    assert actual.unrealized_pnl == pytest.approx(expected.unrealized_pnl)
    assert actual.equity == pytest.approx(
        actual.starting_equity + actual.realized_pnl + actual.unrealized_pnl
    )
    assert actual.fees_paid == pytest.approx(expected.fees_paid)
    assert actual.open_positions == 1


def test_postgres_open_slot_enforces_symbol_and_total_position_limits() -> None:
    repository = _fresh_repository()
    _, btc_one = _seed_candidate(repository, Symbol.BTCUSDT)
    _, btc_two = _seed_candidate(repository, Symbol.BTCUSDT)
    _, eth_one = _seed_candidate(repository, Symbol.ETHUSDT)

    first = repository.persist_paper_open_transaction(
        btc_one, _preview(), _position(btc_one), max_total_open_positions=2
    )
    duplicate_symbol = repository.persist_paper_open_transaction(
        btc_two, _preview(), _position(btc_two), max_total_open_positions=2
    )
    second_symbol = repository.persist_paper_open_transaction(
        eth_one,
        _preview(),
        _position(eth_one, symbol=Symbol.ETHUSDT),
        max_total_open_positions=2,
    )

    assert first["status"] == "OPENED"
    assert duplicate_symbol == {
        "status": "BLOCKED",
        "reason": "maximum one open paper position per symbol reached",
    }
    assert second_symbol["status"] == "OPENED"

    repository = _fresh_repository()
    _, btc = _seed_candidate(repository, Symbol.BTCUSDT)
    _, eth = _seed_candidate(repository, Symbol.ETHUSDT)
    assert repository.persist_paper_open_transaction(
        btc, _preview(), _position(btc), max_total_open_positions=1
    )["status"] == "OPENED"
    limited = repository.persist_paper_open_transaction(
        eth,
        _preview(),
        _position(eth, symbol=Symbol.ETHUSDT),
        max_total_open_positions=1,
    )
    assert limited["status"] == "BLOCKED"
    assert "maximum total" in limited["reason"]


def test_postgres_cooldowns_and_kill_switch_survive_restart() -> None:
    repository = _fresh_repository()
    service = PaperTradingService(
        repository=repository,
        symbol_cooldown=timedelta(minutes=5),
        global_entry_cooldown=timedelta(minutes=5),
    )
    service.restore()
    now = datetime.now(timezone.utc)
    service.kill_switch_active = True
    service.kill_switch_reasons = ["persisted loss kill switch"]
    service.last_entry_at = now
    service.symbol_cooldown_until[Symbol.BTCUSDT] = now + timedelta(minutes=5)
    service.symbol_cooldown_until[Symbol.ETHUSDT] = now - timedelta(seconds=1)
    service._persist_risk_state(now)

    restarted = PaperTradingService(
        repository=PersistenceRepository(str(POSTGRES_TEST_URL), create_schema=True),
        symbol_cooldown=timedelta(minutes=5),
        global_entry_cooldown=timedelta(minutes=5),
    )
    restarted.restore()

    assert restarted.kill_switch_active is True
    assert restarted.kill_switch_reasons == ["persisted loss kill switch"]
    assert Symbol.BTCUSDT in restarted.symbol_cooldown_until
    assert Symbol.ETHUSDT not in restarted.symbol_cooldown_until
    assert restarted.risk_status()["cooldown_state"]["global_remaining_seconds"] > 0
    with Session(restarted.repository.engine) as session:
        assert session.get(PaperRiskStateRow, 1) is not None


def test_postgres_loss_kill_switch_allows_close_and_restores_totals() -> None:
    repository, service, opened = _open_accounting_position(Side.BUY)
    service.max_daily_net_loss_pct = 0.001
    service.max_weekly_net_loss_pct = 0.001
    service.max_account_drawdown_pct = 0.001
    service.kill_switch_active = True
    service.kill_switch_reasons = ["manual pre-close safety block"]
    service._persist_risk_state(datetime.now(timezone.utc))

    closed = service.close_position(
        90.0, reason="stop_loss", position_id=opened.id
    )
    equity = service.equity
    fees = service.fees_paid

    assert closed.realized_pnl < 0
    assert service.kill_switch_active is True
    assert "maximum daily net loss reached" in service.kill_switch_reasons
    assert "maximum weekly net loss reached" in service.kill_switch_reasons
    assert "maximum paper account drawdown reached" in service.kill_switch_reasons
    assert service.entry_block_reasons(Symbol.ETHUSDT)

    restarted = PaperTradingService(
        repository=PersistenceRepository(str(POSTGRES_TEST_URL), create_schema=True),
        max_daily_net_loss_pct=0.001,
        max_weekly_net_loss_pct=0.001,
        max_account_drawdown_pct=0.001,
    )
    restarted.restore()
    assert restarted.kill_switch_active is True
    assert restarted.open_positions == []
    assert restarted.equity == pytest.approx(equity)
    assert restarted.fees_paid == pytest.approx(fees)
    assert len(restarted.closed_trades) == 1
    duplicate = restarted.repository.persist_paper_close_transaction(closed, 10_000)
    assert duplicate["status"] == "EXISTING"
    assert restarted.equity == pytest.approx(equity)
    assert not hasattr(restarted, "bybit_order_adapter")
