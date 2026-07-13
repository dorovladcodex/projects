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
    NewsItemRow,
    PaperExecutionRow,
    PaperPositionRow,
    PersistenceRepository,
    RiskDecisionRow,
    SignalCandidateRow,
)
from app.models import PaperPosition, PositionStatus, Side, SignalRiskPreview, Symbol
from app.db.url import normalize_database_url
from app.config import get_settings


POSTGRES_TEST_URL = os.getenv("BYBOT_TEST_POSTGRES_URL")
pytestmark = pytest.mark.skipif(
    not POSTGRES_TEST_URL,
    reason="set BYBOT_TEST_POSTGRES_URL to run PostgreSQL transaction tests",
)


def _seed_candidate(repository: PersistenceRepository) -> tuple[str, str]:
    news_id, candidate_id = str(uuid4()), str(uuid4())
    now = datetime.now(timezone.utc)
    with Session(repository.engine) as session:
        session.add(NewsItemRow(
            id=news_id, normalized_url=f"https://example.invalid/{news_id}",
            content_hash=uuid4().hex + uuid4().hex,
            payload={"id": news_id}, received_at=now,
        ))
        session.add(SignalCandidateRow(
            id=candidate_id, news_id=news_id, symbol="BTCUSDT", state="READY",
            active=False, expires_at=now + timedelta(minutes=5), payload={},
            risk_preview={}, risk_decision_id=None,
        ))
        session.commit()
    return news_id, candidate_id


def _position(candidate_id: str, *, position_id: str | None = None) -> PaperPosition:
    return PaperPosition(
        id=position_id or uuid4(), symbol=Symbol.BTCUSDT, side=Side.BUY,
        size=0.1, entry_price=100, current_price=100, stop_loss=99,
        take_profit=102, status=PositionStatus.OPEN,
        candidate_id=candidate_id, position_notional=10,
    )


def _preview() -> SignalRiskPreview:
    return SignalRiskPreview(
        preview_performed=True, approved=True, capped_size=0.1,
        position_notional=10, max_allowed_notional=500,
        estimated_fees=0.012, estimated_slippage=0.004,
        rejection_reasons=[], risk_decision_id=None,
    )


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
    repository = PersistenceRepository(str(POSTGRES_TEST_URL), create_schema=True)
    assert repository.available
    _, candidate_id = _seed_candidate(repository)
    duplicate_position_id = str(uuid4())
    with Session(repository.engine) as session:
        session.add(PaperPositionRow(
            id=duplicate_position_id, status="OPEN", payload={"preexisting": True}
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
