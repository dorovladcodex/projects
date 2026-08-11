from __future__ import annotations

from datetime import datetime, timedelta, timezone
from decimal import Decimal
from hashlib import sha256
import logging
import re
from typing import Any
from uuid import uuid4

from sqlalchemy import JSON, Boolean, DateTime, Float, ForeignKey, Index, Numeric, String, UniqueConstraint, create_engine, event, func, select
from sqlalchemy.exc import IntegrityError, SQLAlchemyError
from sqlalchemy.orm import DeclarativeBase, Mapped, Session, mapped_column

from app.models import (
    CandidateLifecycleState,
    ClassificationStatus,
    DemoExecutionRecord,
    DemoExecutionState,
    ExecutionEnvironment,
    NewsClassification,
    NewsItem,
    PaperPosition,
    PositionStatus,
    RiskDecision,
    SignalRiskPreview,
    SignalDryRunResult,
)
from app.v2.models import (
    PortfolioReservation, ReservationState, UniverseStatus, V2Incident,
    V2SignalCandidate,
)
from app.v4.models import V4ForwardLabel, V4Opportunity
from app.db.url import normalize_database_url
from app.db.news_repair import audit_news_row, audit_persistence_payload, inspect_or_repair_news_rows, sanitized_validation_error
from pydantic import ValidationError

LOGGER = logging.getLogger(__name__)


class Base(DeclarativeBase):
    pass


class NewsItemRow(Base):
    __tablename__ = "news_items"
    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    normalized_url: Mapped[str | None] = mapped_column(String(1000), unique=True, nullable=True)
    content_hash: Mapped[str] = mapped_column(String(64), unique=True, nullable=False)
    title: Mapped[str | None] = mapped_column(String(300), nullable=True)
    summary: Mapped[str | None] = mapped_column(String(1000), nullable=True)
    source: Mapped[str | None] = mapped_column(String(100), nullable=True)
    published_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    asset_hint: Mapped[str | None] = mapped_column(String(20), nullable=True)
    raw_category: Mapped[str | None] = mapped_column(String(100), nullable=True)
    importance: Mapped[float | None] = mapped_column(Float, nullable=True)
    is_quarantined: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    payload: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False)
    received_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class PersistenceQuarantineRow(Base):
    __tablename__ = "persistence_quarantine"
    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    original_table: Mapped[str] = mapped_column(String(100), nullable=False)
    original_row_id: Mapped[str] = mapped_column(String(100), nullable=False)
    original_payload: Mapped[dict[str, Any] | None] = mapped_column(JSON, nullable=True)
    validation_error: Mapped[str] = mapped_column(String(1000), nullable=False)
    repair_status: Mapped[str] = mapped_column(String(30), nullable=False)
    quarantined_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    __table_args__ = (
        UniqueConstraint("original_table", "original_row_id", name="uq_persistence_quarantine_origin"),
    )


@event.listens_for(NewsItemRow, "before_insert")
@event.listens_for(NewsItemRow, "before_update")
def _enforce_complete_news_payload(_mapper: object, _connection: object, target: NewsItemRow) -> None:
    if target.is_quarantined:
        return
    item = NewsItem.model_validate(target.payload)
    if any(value in (None, "") for value in (
        target.title, target.summary, target.source, target.published_at
    )):
        raise ValueError("non-quarantined NewsItem rows require complete dedicated fields")
    if str(item.id) != str(target.id):
        raise ValueError("NewsItem payload ID must match the database row ID")


class NewsClassificationRow(Base):
    __tablename__ = "news_classifications"
    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    news_id: Mapped[str] = mapped_column(ForeignKey("news_items.id"), unique=True)
    payload: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False)
    classified_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class ClassifierCacheRow(Base):
    __tablename__ = "classifier_cache"
    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    cache_key: Mapped[str] = mapped_column(String(64), nullable=False)
    classifier_version: Mapped[str] = mapped_column(String(100), nullable=False)
    payload: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    __table_args__ = (UniqueConstraint("cache_key", "classifier_version"),)


class SignalCandidateRow(Base):
    __tablename__ = "signal_candidates"
    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    news_id: Mapped[str] = mapped_column(ForeignKey("news_items.id"), nullable=False)
    execution_environment: Mapped[str] = mapped_column(
        String(20), nullable=False, default=ExecutionEnvironment.PAPER.value
    )
    symbol: Mapped[str] = mapped_column(String(20), nullable=False)
    state: Mapped[str] = mapped_column(String(30), nullable=False)
    active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    payload: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False)
    risk_preview: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False)
    risk_decision_id: Mapped[int | None] = mapped_column(nullable=True)
    run_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False,
        default=lambda: datetime.now(timezone.utc),
    )
    __table_args__ = (UniqueConstraint("news_id", "symbol", "active"),)


class SignalEvaluationRow(Base):
    __tablename__ = "signal_evaluations"
    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    candidate_id: Mapped[str] = mapped_column(ForeignKey("signal_candidates.id"), nullable=False)
    evaluated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    payload: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False)
    __table_args__ = (UniqueConstraint("candidate_id", "evaluated_at"),)


class RiskDecisionRow(Base):
    __tablename__ = "risk_decisions"
    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    candidate_id: Mapped[str] = mapped_column(ForeignKey("signal_candidates.id"), nullable=False)
    approved: Mapped[bool | None] = mapped_column(Boolean, nullable=True)
    capped_size: Mapped[float] = mapped_column(Float, nullable=False, default=0)
    position_notional: Mapped[float] = mapped_column(Float, nullable=False, default=0)
    max_allowed_notional: Mapped[float] = mapped_column(Float, nullable=False, default=0)
    estimated_fees: Mapped[float] = mapped_column(Float, nullable=False, default=0)
    estimated_slippage: Mapped[float] = mapped_column(Float, nullable=False, default=0)
    rejection_reasons: Mapped[list[str]] = mapped_column(JSON, nullable=False, default=list)
    payload: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class PaperPositionRow(Base):
    __tablename__ = "paper_positions"
    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    status: Mapped[str] = mapped_column(String(20), nullable=False)
    symbol: Mapped[str | None] = mapped_column(String(20), nullable=True)
    candidate_id: Mapped[str | None] = mapped_column(String(36), nullable=True)
    open_slot: Mapped[str | None] = mapped_column(String(20), nullable=True)
    payload: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False)
    __table_args__ = (
        Index("uq_paper_positions_open_slot", "open_slot", unique=True),
    )


class PaperTradeRow(Base):
    __tablename__ = "paper_trades"
    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    realized_pnl: Mapped[float] = mapped_column(Float, nullable=False, default=0)
    payload: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False)


class PaperAccountRow(Base):
    __tablename__ = "paper_accounts"
    id: Mapped[int] = mapped_column(primary_key=True)
    starting_equity: Mapped[float] = mapped_column(Float, nullable=False)
    realized_pnl: Mapped[float] = mapped_column(Float, nullable=False, default=0)
    fees_paid: Mapped[float] = mapped_column(Float, nullable=False, default=0)
    equity: Mapped[float] = mapped_column(Float, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )


class PaperRiskStateRow(Base):
    __tablename__ = "paper_risk_state"
    id: Mapped[int] = mapped_column(primary_key=True)
    kill_switch_active: Mapped[bool] = mapped_column(Boolean, nullable=False)
    kill_switch_reasons: Mapped[list[str]] = mapped_column(JSON, nullable=False)
    peak_equity: Mapped[float] = mapped_column(Float, nullable=False)
    daily_pnl: Mapped[float] = mapped_column(Float, nullable=False, default=0)
    weekly_pnl: Mapped[float] = mapped_column(Float, nullable=False, default=0)
    current_drawdown_pct: Mapped[float] = mapped_column(Float, nullable=False, default=0)
    last_entry_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    symbol_cooldowns: Mapped[dict[str, str]] = mapped_column(JSON, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )


class PaperExecutionRow(Base):
    __tablename__ = "paper_executions"
    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    execution_key: Mapped[str] = mapped_column(String(100), unique=True, nullable=False)
    candidate_id: Mapped[str] = mapped_column(
        ForeignKey("signal_candidates.id"), unique=True, nullable=False
    )
    execution_environment: Mapped[str] = mapped_column(
        String(20), nullable=False, default=ExecutionEnvironment.PAPER.value
    )
    risk_decision_id: Mapped[int | None] = mapped_column(
        ForeignKey("risk_decisions.id"), nullable=True
    )
    state: Mapped[str] = mapped_column(String(30), nullable=False)
    position_id: Mapped[str | None] = mapped_column(String(36), nullable=True)
    payload: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class DemoExecutionRow(Base):
    __tablename__ = "demo_executions"
    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    candidate_id: Mapped[str] = mapped_column(
        ForeignKey("signal_candidates.id"), unique=True, nullable=False
    )
    execution_environment: Mapped[str] = mapped_column(
        String(20), nullable=False, default=ExecutionEnvironment.BYBIT_DEMO.value
    )
    risk_decision_id: Mapped[int | None] = mapped_column(
        ForeignKey("risk_decisions.id"), nullable=True
    )
    run_id: Mapped[str] = mapped_column(String(64), nullable=False)
    order_link_id: Mapped[str] = mapped_column(String(36), unique=True, nullable=False)
    order_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    symbol: Mapped[str] = mapped_column(String(20), nullable=False)
    side: Mapped[str] = mapped_column(String(8), nullable=False)
    state: Mapped[str] = mapped_column(String(40), nullable=False)
    requested_quantity: Mapped[Any] = mapped_column(Numeric(36, 18), nullable=False)
    accepted_quantity: Mapped[Any] = mapped_column(Numeric(36, 18), nullable=False)
    average_fill_price: Mapped[Any | None] = mapped_column(Numeric(36, 18), nullable=True)
    close_order_link_id: Mapped[str | None] = mapped_column(String(36), unique=True, nullable=True)
    close_order_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    realized_exchange_pnl: Mapped[Any | None] = mapped_column(Numeric(36, 18), nullable=True)
    payload: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class DemoExecutionEventRow(Base):
    __tablename__ = "demo_execution_events"
    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    execution_id: Mapped[str | None] = mapped_column(
        ForeignKey("demo_executions.id"), nullable=True
    )
    event_key: Mapped[str] = mapped_column(String(200), unique=True, nullable=False)
    event_type: Mapped[str] = mapped_column(String(80), nullable=False)
    payload: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False)
    occurred_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class DemoKillSwitchRow(Base):
    __tablename__ = "demo_kill_switch"
    id: Mapped[int] = mapped_column(primary_key=True)
    active: Mapped[bool] = mapped_column(Boolean, nullable=False)
    reasons: Mapped[list[str]] = mapped_column(JSON, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class DemoKillSwitchEventRow(Base):
    __tablename__ = "demo_kill_switch_events"
    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    event_type: Mapped[str] = mapped_column(String(40), nullable=False)
    active: Mapped[bool] = mapped_column(Boolean, nullable=False)
    reasons: Mapped[list[str]] = mapped_column(JSON, nullable=False)
    execution_id: Mapped[str | None] = mapped_column(
        ForeignKey("demo_executions.id"), nullable=True
    )
    payload: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class DemoSoakRunRow(Base):
    __tablename__ = "demo_soak_runs"
    run_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    status: Mapped[str] = mapped_column(String(20), nullable=False)
    opening_snapshot: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False)
    final_snapshot: Mapped[dict[str, Any] | None] = mapped_column(JSON, nullable=True)


class DemoCanaryJobRow(Base):
    __tablename__ = "demo_canary_jobs"
    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    run_id: Mapped[str] = mapped_column(String(64), unique=True, nullable=False)
    symbol: Mapped[str] = mapped_column(String(20), nullable=False)
    status: Mapped[str] = mapped_column(String(20), nullable=False)
    execution_id: Mapped[str | None] = mapped_column(
        ForeignKey("demo_executions.id"), nullable=True
    )
    request_payload: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False)
    result_payload: Mapped[dict[str, Any] | None] = mapped_column(JSON, nullable=True)
    error_code: Mapped[str | None] = mapped_column(String(80), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class V2UniverseStateRow(Base):
    __tablename__ = "v2_symbol_universe"
    symbol: Mapped[str] = mapped_column(String(20), primary_key=True)
    state: Mapped[str] = mapped_column(String(30), nullable=False)
    accepted: Mapped[bool] = mapped_column(Boolean, nullable=False)
    rejection_reasons: Mapped[list[str]] = mapped_column(JSON, nullable=False)
    payload: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False)
    checked_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class V2MarketFeatureRow(Base):
    __tablename__ = "v2_market_feature_snapshots"
    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    symbol: Mapped[str] = mapped_column(String(20), nullable=False)
    captured_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    fresh: Mapped[bool] = mapped_column(Boolean, nullable=False)
    payload: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False)
    __table_args__ = (UniqueConstraint("symbol", "captured_at", name="uq_v2_feature_symbol_time"),)


class V4OpportunityRow(Base):
    __tablename__ = "v4_opportunities"
    opportunity_id: Mapped[str] = mapped_column(String(36), primary_key=True)
    cycle_id: Mapped[str] = mapped_column(String(160), nullable=False)
    run_id: Mapped[str] = mapped_column(String(64), nullable=False)
    symbol: Mapped[str] = mapped_column(String(20), nullable=False)
    side: Mapped[str] = mapped_column(String(8), nullable=False)
    source: Mapped[str] = mapped_column(String(80), nullable=False)
    candidate_type: Mapped[str] = mapped_column(String(80), nullable=False)
    decision: Mapped[str] = mapped_column(String(30), nullable=False)
    rejected: Mapped[bool] = mapped_column(Boolean, nullable=False)
    feature_snapshot_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    decision_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    payload: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    __table_args__ = (
        Index("ix_v4_opportunity_run_decision", "run_id", "decision_at"),
        UniqueConstraint(
            "run_id", "cycle_id", "symbol", "candidate_type",
            name="uq_v4_opportunity_cycle_symbol_type",
        ),
    )


class V4ForwardLabelRow(Base):
    __tablename__ = "v4_forward_labels"
    opportunity_id: Mapped[str] = mapped_column(
        ForeignKey("v4_opportunities.opportunity_id"), primary_key=True
    )
    symbol: Mapped[str] = mapped_column(String(20), nullable=False)
    decision_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    maximum_horizon_seconds: Mapped[int] = mapped_column(nullable=False)
    complete: Mapped[bool] = mapped_column(Boolean, nullable=False)
    payload: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    __table_args__ = (Index("ix_v4_label_decision", "decision_at"),)


class V2SignalCandidateRow(Base):
    __tablename__ = "v2_signal_candidates"
    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    run_id: Mapped[str] = mapped_column(String(64), nullable=False)
    strategy_name: Mapped[str] = mapped_column(String(80), nullable=False)
    strategy_version: Mapped[str] = mapped_column(String(30), nullable=False)
    symbol: Mapped[str] = mapped_column(String(20), nullable=False)
    side: Mapped[str] = mapped_column(String(8), nullable=False)
    state: Mapped[str] = mapped_column(String(40), nullable=False)
    admitted: Mapped[bool] = mapped_column(Boolean, nullable=False)
    final_score: Mapped[Any | None] = mapped_column(Numeric(18, 8), nullable=True)
    rejection_reason: Mapped[str | None] = mapped_column(String(1000), nullable=True)
    payload: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    __table_args__ = (
        Index("ix_v2_candidate_run_created", "run_id", "created_at"),
        UniqueConstraint("run_id", "strategy_name", "symbol", "created_at", name="uq_v2_candidate_generation"),
    )


class V2PortfolioReservationRow(Base):
    __tablename__ = "v2_portfolio_reservations"
    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    run_id: Mapped[str] = mapped_column(String(64), nullable=False)
    candidate_id: Mapped[str] = mapped_column(
        ForeignKey("v2_signal_candidates.id"), unique=True, nullable=False
    )
    execution_id: Mapped[str | None] = mapped_column(String(36), nullable=True)
    symbol: Mapped[str] = mapped_column(String(20), nullable=False)
    active_symbol: Mapped[str | None] = mapped_column(String(20), unique=True, nullable=True)
    correlation_group: Mapped[str] = mapped_column(String(40), nullable=False)
    strategy_name: Mapped[str] = mapped_column(String(80), nullable=False)
    state: Mapped[str] = mapped_column(String(30), nullable=False)
    notional_usdt: Mapped[Any] = mapped_column(Numeric(36, 18), nullable=False)
    risk_usdt: Mapped[Any] = mapped_column(Numeric(36, 18), nullable=False)
    payload: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    released_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class V2PortfolioStateRow(Base):
    __tablename__ = "v2_portfolio_state"
    id: Mapped[int] = mapped_column(primary_key=True)
    payload: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class V2RejectionRow(Base):
    __tablename__ = "v2_signal_rejections"
    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    run_id: Mapped[str] = mapped_column(String(64), nullable=False)
    candidate_id: Mapped[str | None] = mapped_column(String(36), nullable=True)
    strategy_name: Mapped[str] = mapped_column(String(80), nullable=False)
    symbol: Mapped[str] = mapped_column(String(20), nullable=False)
    reason: Mapped[str] = mapped_column(String(1000), nullable=False)
    payload: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class V2IncidentRow(Base):
    __tablename__ = "v2_incidents"
    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    run_id: Mapped[str] = mapped_column(String(64), nullable=False)
    event_type: Mapped[str] = mapped_column(String(80), nullable=False)
    symbol: Mapped[str | None] = mapped_column(String(20), nullable=True)
    execution_id: Mapped[str | None] = mapped_column(String(36), nullable=True)
    candidate_id: Mapped[str | None] = mapped_column(String(36), nullable=True)
    payload: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False)
    occurred_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class V2RunRow(Base):
    __tablename__ = "v2_runs"
    run_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    status: Mapped[str] = mapped_column(String(30), nullable=False)
    payload: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False)


class PersistenceRepository:
    """Small synchronous repository. Failures degrade persistence, never trading safety."""

    def __init__(
        self,
        database_url: str,
        *,
        create_schema: bool = True,
        connect_timeout_seconds: int = 2,
    ) -> None:
        self.database_url = database_url
        sqlalchemy_url = normalize_database_url(database_url)
        connect_args = (
            {"connect_timeout": max(1, int(connect_timeout_seconds))}
            if sqlalchemy_url.startswith("postgresql") else {}
        )
        self.available = False
        self.last_error: str | None = None
        self.last_error_code: str | None = None
        self.last_sqlstate: str | None = None
        self.news_restore_valid_count = 0
        self.news_restore_repaired_count = 0
        self.news_restore_quarantined_count = 0
        self.news_restore_last_error: str | None = None
        try:
            self.engine = create_engine(
                sqlalchemy_url, pool_pre_ping=True, connect_args=connect_args
            )
            if create_schema:
                Base.metadata.create_all(self.engine)
            with self.engine.connect() as connection:
                connection.exec_driver_sql("SELECT 1")
            self.available = True
        except (SQLAlchemyError, ImportError) as exc:
            self.last_error = type(exc).__name__
            self.last_error_code = (
                _database_error_code(exc)
                if isinstance(exc, SQLAlchemyError) else "DB_DRIVER_UNAVAILABLE"
            )
            self.last_sqlstate = _sqlstate(exc)

    def health_check(self) -> bool:
        """Prove both the connection and migration schema are readable."""
        if not self.available:
            return False
        try:
            with self.engine.connect() as connection:
                connection.exec_driver_sql("SELECT 1").scalar_one()
                if self.engine.dialect.name == "postgresql":
                    revision = connection.exec_driver_sql(
                        "SELECT version_num FROM alembic_version LIMIT 1"
                    ).scalar_one_or_none()
                    if not revision:
                        raise RuntimeError("Alembic schema revision is unavailable")
            return True
        except (SQLAlchemyError, RuntimeError) as exc:
            self.available = False
            self.last_error = type(exc).__name__
            self.last_error_code = (
                _database_error_code(exc)
                if isinstance(exc, SQLAlchemyError)
                else "DB_SCHEMA_UNAVAILABLE"
            )
            self.last_sqlstate = _sqlstate(exc)
            return False

    def dispose(self) -> None:
        engine = getattr(self, "engine", None)
        if engine is not None:
            engine.dispose()

    def save_news(self, item: NewsItem) -> bool:
        if not self.available:
            return False
        # Revalidate the serialized boundary as well as the typed input. This
        # prevents future producer changes from persisting partial payloads.
        payload = item.model_dump(mode="json")
        validated = NewsItem.model_validate(payload)
        row = NewsItemRow(
            id=str(validated.id), normalized_url=normalize_url(validated.url),
            content_hash=news_content_hash(validated),
            title=validated.title, summary=validated.summary, source=validated.source,
            published_at=validated.published_at, asset_hint=validated.asset_hint.value,
            raw_category=validated.raw_category, importance=validated.importance,
            is_quarantined=False, payload=payload, received_at=validated.received_at,
        )
        try:
            with Session(self.engine) as session:
                session.add(row)
                session.commit()
            return True
        except IntegrityError as exc:
            return False
        except SQLAlchemyError as exc:
            self._failed(exc)
            return False

    def save_classification(self, item: NewsItem, classification: NewsClassification, version: str, expires_at: datetime) -> None:
        if not self.available:
            return
        payload = classification.model_dump(mode="json")
        try:
            with Session(self.engine) as session:
                classification_row = session.scalar(select(NewsClassificationRow).where(
                    NewsClassificationRow.news_id == str(item.id)
                ))
                if classification_row:
                    classification_row.payload = payload
                    classification_row.classified_at = classification.classified_at
                else:
                    session.add(NewsClassificationRow(
                        news_id=str(item.id), payload=payload,
                        classified_at=classification.classified_at,
                    ))
                if classification.classification_status in {
                    ClassificationStatus.SUCCESS, ClassificationStatus.CACHE_HIT
                }:
                    existing = session.scalar(select(ClassifierCacheRow).where(
                        ClassifierCacheRow.cache_key == classifier_cache_key(item),
                        ClassifierCacheRow.classifier_version == version,
                    ))
                    if existing:
                        existing.payload, existing.expires_at = payload, expires_at
                    else:
                        session.add(ClassifierCacheRow(
                            cache_key=classifier_cache_key(item), classifier_version=version,
                            payload=payload, expires_at=expires_at,
                        ))
                session.commit()
        except SQLAlchemyError as exc:
            self._failed(exc)

    def cached_classification(self, item: NewsItem, version: str, now: datetime) -> NewsClassification | None:
        if not self.available:
            return None
        try:
            with Session(self.engine) as session:
                row = session.scalar(select(ClassifierCacheRow).where(
                    ClassifierCacheRow.cache_key == classifier_cache_key(item),
                    ClassifierCacheRow.classifier_version == version,
                    ClassifierCacheRow.expires_at > now,
                ))
                if not row:
                    return None
                payload = dict(row.payload)
                payload.update({"news_id": str(item.id), "classification_status": "CACHE_HIT", "cache_hit": True})
                return NewsClassification.model_validate(payload)
        except SQLAlchemyError as exc:
            self._failed(exc)
            return None

    def save_signal_result(self, result: SignalDryRunResult) -> None:
        if not self.available:
            return
        candidate = result.candidate
        active = candidate.state == CandidateLifecycleState.PENDING_CONFIRMATION
        try:
            with Session(self.engine) as session:
                row = session.get(SignalCandidateRow, str(candidate.id))
                values = dict(
                    news_id=str(candidate.news_id),
                    execution_environment=candidate.execution_environment.value,
                    symbol=candidate.symbol.value if candidate.symbol else "NONE",
                    state=candidate.state.value, active=active, expires_at=candidate.expires_at,
                    payload=candidate.model_dump(mode="json"), risk_preview=result.risk_preview.model_dump(mode="json"),
                    risk_decision_id=result.risk_preview.risk_decision_id,
                    run_id=candidate.run_id,
                    created_at=candidate.created_at,
                )
                if row:
                    for key, value in values.items():
                        setattr(row, key, value)
                else:
                    session.add(SignalCandidateRow(id=str(candidate.id), **values))
                for evaluation in candidate.evaluation_history:
                    exists = session.scalar(select(SignalEvaluationRow.id).where(
                        SignalEvaluationRow.candidate_id == str(candidate.id),
                        SignalEvaluationRow.evaluated_at == evaluation.evaluated_at,
                    ))
                    if not exists:
                        session.add(SignalEvaluationRow(
                            candidate_id=str(candidate.id), evaluated_at=evaluation.evaluated_at,
                            payload=evaluation.model_dump(mode="json"),
                        ))
                session.commit()
        except IntegrityError:
            pass
        except SQLAlchemyError as exc:
            self._failed(exc)

    def save_risk_decision(self, candidate_id: str, decision: RiskDecision) -> int | None:
        if not self.available:
            return None
        try:
            with Session(self.engine) as session:
                row = RiskDecisionRow(
                    candidate_id=candidate_id, approved=decision.approved,
                    capped_size=float(decision.capped_size),
                    position_notional=float(decision.position_notional),
                    max_allowed_notional=float(decision.max_allowed_notional),
                    estimated_fees=float(decision.estimated_fees),
                    estimated_slippage=float(decision.estimated_slippage),
                    rejection_reasons=list(decision.reasons),
                    payload=decision.model_dump(mode="json"), created_at=decision.decided_at,
                )
                session.add(row)
                session.flush()
                decision_id = row.id
                session.commit()
                return decision_id
        except SQLAlchemyError as exc:
            self._failed(exc)
            return None

    def persist_v2_compatibility_bundle(
        self,
        news: NewsItem,
        classification: NewsClassification,
        result: SignalDryRunResult,
        decision: RiskDecision,
        *,
        classifier_version: str,
        cache_expires_at: datetime,
    ) -> int | None:
        """Persist the V1-compatible V2 execution inputs atomically.

        The Demo execution state machine still consumes the proven V1 models.
        A candidate must never reach it with only a subset of its synthetic
        news, classification, signal and approved risk decision committed.
        """
        if not self.available:
            self.last_error_code = "DB_UNAVAILABLE"
            return None
        self.last_error = None
        self.last_error_code = None
        validated_news = NewsItem.model_validate(news.model_dump(mode="json"))
        candidate = result.candidate
        payload = validated_news.model_dump(mode="json")
        content_hash = news_content_hash(validated_news)
        try:
            with Session(self.engine) as session, session.begin():
                news_row = session.get(NewsItemRow, str(validated_news.id))
                hash_owner = session.scalar(
                    select(NewsItemRow).where(NewsItemRow.content_hash == content_hash)
                )
                if hash_owner is not None and hash_owner.id != str(validated_news.id):
                    self.last_error = "NewsContentCollision"
                    self.last_error_code = "DB_NEWS_CONTENT_COLLISION"
                    return None
                if news_row is None:
                    session.add(NewsItemRow(
                        id=str(validated_news.id),
                        normalized_url=normalize_url(validated_news.url),
                        content_hash=content_hash,
                        title=validated_news.title,
                        summary=validated_news.summary,
                        source=validated_news.source,
                        published_at=validated_news.published_at,
                        asset_hint=validated_news.asset_hint.value,
                        raw_category=validated_news.raw_category,
                        importance=validated_news.importance,
                        is_quarantined=False,
                        payload=payload,
                        received_at=validated_news.received_at,
                    ))

                classification_payload = classification.model_dump(mode="json")
                classification_row = session.scalar(
                    select(NewsClassificationRow).where(
                        NewsClassificationRow.news_id == str(validated_news.id)
                    )
                )
                if classification_row is None:
                    session.add(NewsClassificationRow(
                        news_id=str(validated_news.id),
                        payload=classification_payload,
                        classified_at=classification.classified_at,
                    ))
                else:
                    classification_row.payload = classification_payload
                    classification_row.classified_at = classification.classified_at

                cache_row = session.scalar(select(ClassifierCacheRow).where(
                    ClassifierCacheRow.cache_key == classifier_cache_key(validated_news),
                    ClassifierCacheRow.classifier_version == classifier_version,
                ))
                if cache_row is None:
                    session.add(ClassifierCacheRow(
                        cache_key=classifier_cache_key(validated_news),
                        classifier_version=classifier_version,
                        payload=classification_payload,
                        expires_at=cache_expires_at,
                    ))
                else:
                    cache_row.payload = classification_payload
                    cache_row.expires_at = cache_expires_at

                signal_row = session.get(SignalCandidateRow, str(candidate.id))
                signal_values = {
                    "news_id": str(candidate.news_id),
                    "execution_environment": candidate.execution_environment.value,
                    "symbol": candidate.symbol.value if candidate.symbol else "NONE",
                    "state": candidate.state.value,
                    "active": candidate.state == CandidateLifecycleState.PENDING_CONFIRMATION,
                    "expires_at": candidate.expires_at,
                    "payload": candidate.model_dump(mode="json"),
                    "risk_preview": result.risk_preview.model_dump(mode="json"),
                    "risk_decision_id": result.risk_preview.risk_decision_id,
                    "run_id": candidate.run_id,
                    "created_at": candidate.created_at,
                }
                if signal_row is None:
                    signal_row = SignalCandidateRow(id=str(candidate.id), **signal_values)
                    session.add(signal_row)
                    session.flush()
                else:
                    for key, value in signal_values.items():
                        setattr(signal_row, key, value)

                risk_row = session.scalar(
                    select(RiskDecisionRow)
                    .where(
                        RiskDecisionRow.candidate_id == str(candidate.id),
                        RiskDecisionRow.approved.is_(True),
                    )
                    .order_by(RiskDecisionRow.id.desc())
                )
                if risk_row is None:
                    risk_row = RiskDecisionRow(
                        candidate_id=str(candidate.id),
                        approved=decision.approved,
                        capped_size=float(decision.capped_size),
                        position_notional=float(decision.position_notional),
                        max_allowed_notional=float(decision.max_allowed_notional),
                        estimated_fees=float(decision.estimated_fees),
                        estimated_slippage=float(decision.estimated_slippage),
                        rejection_reasons=list(decision.reasons),
                        payload=decision.model_dump(mode="json"),
                        created_at=decision.decided_at,
                    )
                    session.add(risk_row)
                    session.flush()

                result.risk_preview = SignalRiskPreview(
                    preview_performed=True,
                    approved=True,
                    capped_size=float(decision.capped_size),
                    position_notional=float(decision.position_notional),
                    max_allowed_notional=float(decision.max_allowed_notional),
                    rejection_reasons=[],
                    risk_decision_id=risk_row.id,
                    estimated_fees=float(decision.estimated_fees),
                    estimated_slippage=float(decision.estimated_slippage),
                )
                signal_row.risk_preview = result.risk_preview.model_dump(mode="json")
                signal_row.risk_decision_id = risk_row.id
                session.flush()
                return risk_row.id
        except SQLAlchemyError as exc:
            self._failed(exc)
            return None

    def ensure_approved_risk_decision(
        self, candidate_id: str, preview: SignalRiskPreview
    ) -> int | None:
        if not self.available or not preview.preview_performed or not preview.approved:
            return None
        try:
            with Session(self.engine) as session:
                existing = session.scalar(
                    select(RiskDecisionRow)
                    .where(
                        RiskDecisionRow.candidate_id == candidate_id,
                        RiskDecisionRow.approved.is_(True),
                    )
                    .order_by(RiskDecisionRow.id.desc())
                )
                if existing:
                    return existing.id
        except SQLAlchemyError as exc:
            self._failed(exc)
            return None
        decision = RiskDecision(
            approved=True,
            reasons=list(preview.rejection_reasons),
            capped_size=preview.capped_size,
            position_notional=preview.position_notional,
            max_allowed_notional=preview.max_allowed_notional,
            estimated_fees=preview.estimated_fees,
            estimated_slippage=preview.estimated_slippage,
        )
        return self.save_risk_decision(candidate_id, decision)

    def persist_paper_open_transaction(
        self,
        candidate_id: str,
        preview: SignalRiskPreview,
        position: PaperPosition,
        *,
        max_total_open_positions: int = 1,
        starting_equity: float = 10_000.0,
    ) -> dict[str, Any]:
        """Atomically persist risk, execution reservation, and opened position."""
        if not self.available:
            return {"status": "ERROR", "error_code": "DB_UNAVAILABLE", "retryable": True}
        session = Session(self.engine)
        try:
            with session.begin():
                candidate = session.get(SignalCandidateRow, candidate_id)
                if candidate is None:
                    raise ValueError("signal candidate row is missing")
                existing_execution = session.scalar(
                    select(PaperExecutionRow)
                    .where(PaperExecutionRow.candidate_id == candidate_id)
                    .with_for_update()
                )
                if existing_execution and existing_execution.position_id:
                    return {
                        "status": "EXISTING",
                        "execution_id": existing_execution.id,
                        "execution_key": existing_execution.execution_key,
                        "position_id": existing_execution.position_id,
                        "state": existing_execution.state,
                        "payload": existing_execution.payload,
                    }

                # Lock the singleton account row to serialize all paper entry
                # reservations, including the empty-position case.
                self._paper_account_for_update(session, starting_equity)
                open_rows = session.scalars(
                    select(PaperPositionRow)
                    .where(PaperPositionRow.status == PositionStatus.OPEN.value)
                    .with_for_update()
                ).all()
                if any(row.open_slot == position.symbol.value for row in open_rows):
                    return {
                        "status": "BLOCKED",
                        "reason": "maximum one open paper position per symbol reached",
                    }
                if len(open_rows) >= max_total_open_positions:
                    return {
                        "status": "BLOCKED",
                        "reason": "maximum total open paper positions reached",
                    }

                risk_row = None
                if preview.risk_decision_id:
                    risk_row = session.get(RiskDecisionRow, preview.risk_decision_id)
                if risk_row is None:
                    risk_row = session.scalar(
                        select(RiskDecisionRow)
                        .where(
                            RiskDecisionRow.candidate_id == candidate_id,
                            RiskDecisionRow.approved.is_(True),
                        )
                        .order_by(RiskDecisionRow.created_at.desc())
                    )
                if risk_row is None:
                    risk_payload = {
                        "approved": True,
                        "capped_size": float(preview.capped_size),
                        "position_notional": float(preview.position_notional),
                        "max_allowed_notional": float(preview.max_allowed_notional),
                        "estimated_fees": float(preview.estimated_fees),
                        "estimated_slippage": float(preview.estimated_slippage),
                        "rejection_reasons": list(preview.rejection_reasons),
                        "created_at": datetime.now(timezone.utc).isoformat(),
                    }
                    risk_row = RiskDecisionRow(
                        candidate_id=candidate_id,
                        approved=True,
                        capped_size=float(preview.capped_size),
                        position_notional=float(preview.position_notional),
                        max_allowed_notional=float(preview.max_allowed_notional),
                        estimated_fees=float(preview.estimated_fees),
                        estimated_slippage=float(preview.estimated_slippage),
                        rejection_reasons=list(preview.rejection_reasons),
                        payload=risk_payload,
                        created_at=datetime.now(timezone.utc),
                    )
                    session.add(risk_row)
                    session.flush()
                preview.risk_decision_id = risk_row.id
                candidate.risk_decision_id = risk_row.id
                candidate.risk_preview = preview.model_dump(mode="json")

                execution_key = f"paper:{candidate_id}"
                execution = existing_execution or PaperExecutionRow(
                    id=str(uuid4()), execution_key=execution_key,
                    candidate_id=candidate_id, risk_decision_id=risk_row.id,
                    execution_environment=ExecutionEnvironment.PAPER.value,
                    state="EXECUTING_PAPER", position_id=None,
                    payload={"candidate_id": candidate_id},
                    created_at=datetime.now(timezone.utc),
                    updated_at=datetime.now(timezone.utc),
                )
                if existing_execution is None:
                    session.add(execution)
                execution.risk_decision_id = risk_row.id
                session.flush()

                position.risk_decision_id = risk_row.id
                position_payload = _paper_execution_payload(position)
                session.add(PaperPositionRow(
                    id=str(position.id), status=position.status.value,
                    symbol=position.symbol.value,
                    candidate_id=candidate_id,
                    open_slot=position.symbol.value,
                    payload=position.model_dump(mode="json"),
                ))
                session.flush()
                execution.position_id = str(position.id)
                execution.state = "PAPER_OPENED"
                execution.payload = position_payload
                execution.updated_at = datetime.now(timezone.utc)
            return {
                "status": "OPENED",
                "execution_id": execution.id,
                "execution_key": execution.execution_key,
                "position_id": str(position.id),
                "risk_decision_id": risk_row.id,
                "payload": position_payload,
            }
        except IntegrityError as exc:
            session.rollback()
            existing = self.paper_execution_details(candidate_id)
            if existing and existing.get("position_id"):
                return existing
            return self._transaction_error("DB_INTEGRITY_ERROR", exc)
        except (SQLAlchemyError, ValueError) as exc:
            session.rollback()
            return self._transaction_error(_database_error_code(exc), exc)
        finally:
            session.close()

    def _transaction_error(self, error_code: str, exc: object) -> dict[str, Any]:
        self.last_error_code = error_code
        self.last_error = type(exc).__name__ if not isinstance(exc, type) else exc.__name__
        message = _sanitize_database_error(str(exc))
        LOGGER.error("paper persistence failed: type=%s message=%s", self.last_error, message)
        return {"status": "ERROR", "error_code": error_code, "retryable": True}

    def reserve_paper_execution(
        self, candidate_id: str, risk_decision_id: int | None
    ) -> dict[str, Any] | None:
        if not self.available:
            return None
        execution_key = f"paper:{candidate_id}"
        execution_id = str(uuid4())
        now = datetime.now(timezone.utc)
        try:
            with Session(self.engine) as session:
                existing = session.scalar(select(PaperExecutionRow).where(
                    PaperExecutionRow.candidate_id == candidate_id
                ))
                if existing:
                    has_position = existing.position_id is not None
                    terminal = existing.state in {"PAPER_OPENED", "PAPER_CLOSED"}
                    updated_at = existing.updated_at
                    if updated_at.tzinfo is None:
                        updated_at = updated_at.replace(tzinfo=timezone.utc)
                    recent_in_progress = (
                        existing.state == "EXECUTING_PAPER"
                        and now - updated_at < timedelta(seconds=30)
                    )
                    if has_position or terminal or recent_in_progress:
                        return {
                            "status": "EXISTING",
                            "execution_id": existing.id,
                            "execution_key": existing.execution_key,
                            "position_id": existing.position_id,
                            "state": existing.state,
                            "payload": existing.payload,
                        }
                    existing.state = "EXECUTING_PAPER"
                    existing.risk_decision_id = risk_decision_id
                    existing.payload = {"candidate_id": candidate_id, "resumed": True}
                    existing.updated_at = now
                    session.commit()
                    return {
                        "status": "RESUMED",
                        "execution_id": existing.id,
                        "execution_key": existing.execution_key,
                        "position_id": None,
                        "state": existing.state,
                        "payload": existing.payload,
                    }
                session.add(PaperExecutionRow(
                    id=execution_id, execution_key=execution_key,
                    candidate_id=candidate_id, risk_decision_id=risk_decision_id,
                    execution_environment=ExecutionEnvironment.PAPER.value,
                    state="EXECUTING_PAPER", payload={"candidate_id": candidate_id},
                    created_at=now, updated_at=now,
                ))
                session.commit()
            return {
                "status": "RESERVED",
                "execution_id": execution_id,
                "execution_key": execution_key,
                "position_id": None,
                "state": "EXECUTING_PAPER",
                "payload": {"candidate_id": candidate_id},
            }
        except IntegrityError:
            return self.paper_execution_details(candidate_id)
        except SQLAlchemyError as exc:
            self._failed(exc)
            return None

    def paper_execution_details(self, candidate_id: str) -> dict[str, Any] | None:
        if not self.available:
            return None
        try:
            with Session(self.engine) as session:
                row = session.scalar(select(PaperExecutionRow).where(
                    PaperExecutionRow.candidate_id == candidate_id
                ))
                if not row:
                    return None
                return {
                    "status": "EXISTING",
                    "execution_id": row.id,
                    "execution_key": row.execution_key,
                    "position_id": row.position_id,
                    "state": row.state,
                    "payload": row.payload,
                }
        except SQLAlchemyError as exc:
            self._failed(exc)
            return None

    def update_paper_execution(
        self,
        candidate_id: str,
        state: str,
        payload: dict[str, Any],
        *,
        position_id: str | None = None,
    ) -> None:
        if not self.available:
            return
        try:
            with Session(self.engine) as session:
                row = session.scalar(select(PaperExecutionRow).where(
                    PaperExecutionRow.candidate_id == candidate_id
                ))
                if row:
                    row.state = state
                    row.payload = payload
                    row.position_id = position_id or row.position_id
                    row.updated_at = datetime.now(timezone.utc)
                    session.commit()
        except SQLAlchemyError as exc:
            self._failed(exc)

    def executed_candidate_ids(self) -> set[str]:
        if not self.available:
            return set()
        try:
            with Session(self.engine) as session:
                return set(session.scalars(select(PaperExecutionRow.candidate_id).where(
                    (PaperExecutionRow.position_id.is_not(None))
                    | (PaperExecutionRow.state.in_(("PAPER_OPENED", "PAPER_CLOSED")))
                )).all())
        except SQLAlchemyError as exc:
            self._failed(exc)
            return set()

    def recover_orphaned_paper_executions(self) -> int:
        """Mark pre-restart reservations without positions retryable."""
        if not self.available:
            return 0
        try:
            with Session(self.engine) as session:
                rows = session.scalars(select(PaperExecutionRow).where(
                    PaperExecutionRow.state == "EXECUTING_PAPER",
                    PaperExecutionRow.position_id.is_(None),
                )).all()
                now = datetime.now(timezone.utc)
                for row in rows:
                    row.state = "EXECUTION_FAILED"
                    row.payload = {
                        **row.payload,
                        "recovery_reason": "orphaned reservation recovered on restart",
                    }
                    row.updated_at = now
                session.commit()
                return len(rows)
        except SQLAlchemyError as exc:
            self._failed(exc)
            return 0

    def load_news(self) -> tuple[list[NewsItem], list[NewsClassification]]:
        if not self.available:
            return [], []
        try:
            news: list[NewsItem] = []
            classifications: list[NewsClassification] = []
            with Session(self.engine) as session, session.begin():
                report = inspect_or_repair_news_rows(session, apply=True)
                rows = session.scalars(select(NewsItemRow).where(
                    NewsItemRow.is_quarantined.is_(False)
                ).order_by(NewsItemRow.received_at, NewsItemRow.id)).all()
                for row in rows:
                    try:
                        news.append(NewsItem.model_validate(row.payload))
                    except (ValidationError, ValueError, TypeError) as exc:
                        # A concurrent/manual malformed row still cannot take down
                        # startup. The next repair pass quarantines it atomically.
                        error = sanitized_validation_error(exc)
                        LOGGER.warning(
                            "news restore rejected row: table=news_items row_id=%s error=%s",
                            row.id, error,
                        )
                        audit_news_row(
                            session, row, validation_error=error,
                            repair_status="QUARANTINED",
                            now=datetime.now(timezone.utc),
                        )
                        row.is_quarantined = True
                valid_ids = {str(item.id) for item in news}
                for row in session.scalars(select(NewsClassificationRow)).all():
                    if row.news_id not in valid_ids:
                        continue
                    try:
                        classifications.append(NewsClassification.model_validate(row.payload))
                    except (ValidationError, ValueError, TypeError) as exc:
                        LOGGER.warning(
                            "classification restore rejected row: table=news_classifications row_id=%s error=%s",
                            row.id, sanitized_validation_error(exc),
                        )

                quarantine_rows = session.scalars(select(PersistenceQuarantineRow).where(
                    PersistenceQuarantineRow.original_table == "news_items"
                )).all()
                self.news_restore_valid_count = len(news)
                self.news_restore_repaired_count = sum(
                    row.repair_status == "REPAIRED" for row in quarantine_rows
                )
                self.news_restore_quarantined_count = sum(
                    row.repair_status == "QUARANTINED" for row in quarantine_rows
                )
                latest_quarantine = next(
                    (
                        row for row in sorted(
                            quarantine_rows, key=lambda item: item.updated_at, reverse=True
                        ) if row.repair_status == "QUARANTINED"
                    ),
                    None,
                )
                self.news_restore_last_error = (
                    latest_quarantine.validation_error if latest_quarantine else None
                )
                for row_id in report.quarantined_row_ids:
                    LOGGER.warning(
                        "news row quarantined during restore: table=news_items row_id=%s",
                        row_id,
                    )
            return news, classifications
        except SQLAlchemyError as exc:
            self._failed(exc)
            return [], []

    def quarantined_news_ids(self) -> list[str]:
        if not self.available:
            return []
        try:
            with Session(self.engine) as session:
                return list(session.scalars(select(NewsItemRow.id).where(
                    NewsItemRow.is_quarantined.is_(True)
                )).all())
        except SQLAlchemyError as exc:
            self._failed(exc)
            return []

    def load_signal_results(
        self, execution_environment: ExecutionEnvironment | None = None
    ) -> list[SignalDryRunResult]:
        if not self.available:
            return []
        try:
            results: list[SignalDryRunResult] = []
            with Session(self.engine) as session, session.begin():
                query = (
                    select(SignalCandidateRow)
                    .join(NewsItemRow, SignalCandidateRow.news_id == NewsItemRow.id)
                    .where(NewsItemRow.is_quarantined.is_(False))
                )
                if execution_environment is not None:
                    query = query.where(
                        SignalCandidateRow.execution_environment
                        == execution_environment.value
                    )
                rows = session.scalars(query).all()
                for row in rows:
                    try:
                        candidate_payload = dict(row.payload)
                        candidate_payload["execution_environment"] = (
                            row.execution_environment
                        )
                        results.append(SignalDryRunResult.model_validate({
                            "candidate": candidate_payload,
                            "risk_preview": row.risk_preview,
                        }))
                    except (ValidationError, ValueError, TypeError) as exc:
                        error = sanitized_validation_error(exc)
                        audit_persistence_payload(
                            session,
                            original_table="signal_candidates",
                            original_row_id=str(row.id),
                            original_payload=(dict(row.payload) if isinstance(row.payload, dict) else None),
                            validation_error=error,
                            repair_status="QUARANTINED",
                            now=datetime.now(timezone.utc),
                        )
                        LOGGER.warning(
                            "signal candidate quarantined during restore: row_id=%s error=%s",
                            row.id, error,
                        )
            return results
        except SQLAlchemyError as exc:
            self._failed(exc)
            return []

    def load_or_create_paper_account(
        self, starting_equity: float
    ) -> dict[str, float] | None:
        """Restore and reconcile the singleton paper account from closed trades."""
        if not self.available:
            return None
        try:
            with Session(self.engine) as session, session.begin():
                account = self._paper_account_for_update(session, starting_equity)
                trades = session.scalars(select(PaperTradeRow)).all()
                realized_pnl = 0.0
                fees_paid = 0.0
                for row in trades:
                    trade = PaperPosition.model_validate(row.payload)
                    realized_pnl += float(trade.realized_pnl)
                    fees_paid += float(trade.fees_paid)
                account.realized_pnl = realized_pnl
                account.fees_paid = fees_paid
                account.equity = account.starting_equity + realized_pnl
                account.updated_at = datetime.now(timezone.utc)
                session.flush()
                return _paper_account_payload(account)
        except (SQLAlchemyError, ValueError) as exc:
            self._failed(exc) if isinstance(exc, SQLAlchemyError) else self._transaction_error(
                _database_error_code(exc), exc
            )
            return None

    def persist_paper_close_transaction(
        self,
        position: PaperPosition,
        starting_equity: float,
    ) -> dict[str, Any]:
        """Atomically close a position and credit its net PnL exactly once."""
        if not self.available:
            return {"status": "ERROR", "error_code": "DB_UNAVAILABLE", "retryable": True}
        session = Session(self.engine)
        try:
            with session.begin():
                position_id = str(position.id)
                row = session.scalar(
                    select(PaperPositionRow)
                    .where(PaperPositionRow.id == position_id)
                    .with_for_update()
                )
                if row is None:
                    raise ValueError("paper position row is missing")

                account = self._paper_account_for_update(session, starting_equity)
                existing_trade = session.get(PaperTradeRow, position_id)
                if row.status == PositionStatus.CLOSED.value or existing_trade is not None:
                    stored_payload = (
                        existing_trade.payload if existing_trade is not None else row.payload
                    )
                    return {
                        "status": "EXISTING",
                        "position": stored_payload,
                        "account": _paper_account_payload(account),
                    }

                if position.status != PositionStatus.CLOSED:
                    raise ValueError("paper position must be CLOSED before persistence")

                payload = position.model_dump(mode="json")
                row.status = PositionStatus.CLOSED.value
                row.symbol = position.symbol.value
                row.candidate_id = (
                    str(position.candidate_id) if position.candidate_id else None
                )
                row.open_slot = None
                row.payload = payload
                session.add(PaperTradeRow(
                    id=position_id,
                    realized_pnl=float(position.realized_pnl),
                    payload=payload,
                ))

                candidate_id = str(position.candidate_id) if position.candidate_id else None
                if candidate_id:
                    candidate = session.scalar(
                        select(SignalCandidateRow)
                        .where(SignalCandidateRow.id == candidate_id)
                        .with_for_update()
                    )
                    if candidate is None:
                        raise ValueError("signal candidate row is missing")
                    candidate_payload = dict(candidate.payload)
                    candidate_payload["state"] = CandidateLifecycleState.PAPER_CLOSED.value
                    candidate.state = CandidateLifecycleState.PAPER_CLOSED.value
                    candidate.active = False
                    candidate.payload = candidate_payload

                    execution = session.scalar(
                        select(PaperExecutionRow)
                        .where(PaperExecutionRow.candidate_id == candidate_id)
                        .with_for_update()
                    )
                    if execution is None:
                        raise ValueError("paper execution row is missing")
                    execution.state = CandidateLifecycleState.PAPER_CLOSED.value
                    execution.position_id = position_id
                    execution.payload = _paper_execution_payload(position)
                    execution.updated_at = datetime.now(timezone.utc)

                account.realized_pnl += float(position.realized_pnl)
                account.fees_paid += float(position.fees_paid)
                account.equity = account.starting_equity + account.realized_pnl
                account.updated_at = datetime.now(timezone.utc)
                session.flush()
                return {
                    "status": "CLOSED",
                    "position": payload,
                    "account": _paper_account_payload(account),
                }
        except IntegrityError as exc:
            session.rollback()
            return self._transaction_error("DB_INTEGRITY_ERROR", exc)
        except (SQLAlchemyError, ValueError) as exc:
            session.rollback()
            return self._transaction_error(_database_error_code(exc), exc)
        finally:
            session.close()

    def _paper_account_for_update(
        self, session: Session, starting_equity: float
    ) -> PaperAccountRow:
        account = session.scalar(
            select(PaperAccountRow)
            .where(PaperAccountRow.id == 1)
            .with_for_update()
        )
        if account is not None:
            return account

        trades = session.scalars(select(PaperTradeRow)).all()
        realized_pnl = 0.0
        fees_paid = 0.0
        for row in trades:
            trade = PaperPosition.model_validate(row.payload)
            realized_pnl += float(trade.realized_pnl)
            fees_paid += float(trade.fees_paid)
        account = PaperAccountRow(
            id=1,
            starting_equity=float(starting_equity),
            realized_pnl=realized_pnl,
            fees_paid=fees_paid,
            equity=float(starting_equity) + realized_pnl,
            updated_at=datetime.now(timezone.utc),
        )
        session.add(account)
        session.flush()
        return account

    def load_or_create_paper_risk_state(
        self, starting_equity: float
    ) -> dict[str, Any] | None:
        if not self.available:
            return None
        try:
            with Session(self.engine) as session, session.begin():
                row = session.get(PaperRiskStateRow, 1)
                if row is None:
                    now = datetime.now(timezone.utc)
                    row = PaperRiskStateRow(
                        id=1,
                        kill_switch_active=False,
                        kill_switch_reasons=[],
                        peak_equity=float(starting_equity),
                        daily_pnl=0.0,
                        weekly_pnl=0.0,
                        current_drawdown_pct=0.0,
                        last_entry_at=None,
                        symbol_cooldowns={},
                        updated_at=now,
                    )
                    session.add(row)
                    session.flush()
                return _paper_risk_state_payload(row)
        except SQLAlchemyError as exc:
            self._failed(exc)
            return None

    def save_paper_risk_state(self, state: dict[str, Any]) -> None:
        if not self.available:
            return
        try:
            with Session(self.engine) as session:
                session.merge(PaperRiskStateRow(
                    id=1,
                    kill_switch_active=bool(state["kill_switch_active"]),
                    kill_switch_reasons=list(state["kill_switch_reasons"]),
                    peak_equity=float(state["peak_equity"]),
                    daily_pnl=float(state["daily_pnl"]),
                    weekly_pnl=float(state["weekly_pnl"]),
                    current_drawdown_pct=float(state["current_drawdown_pct"]),
                    last_entry_at=state.get("last_entry_at"),
                    symbol_cooldowns=dict(state["symbol_cooldowns"]),
                    updated_at=state.get("updated_at") or datetime.now(timezone.utc),
                ))
                session.commit()
        except SQLAlchemyError as exc:
            self._failed(exc)

    def save_paper_position(self, position: PaperPosition) -> None:
        if not self.available:
            return
        try:
            with Session(self.engine) as session:
                payload = position.model_dump(mode="json")
                session.merge(PaperPositionRow(
                    id=str(position.id),
                    status=position.status.value,
                    symbol=position.symbol.value,
                    candidate_id=(
                        str(position.candidate_id) if position.candidate_id else None
                    ),
                    open_slot=(
                        position.symbol.value
                        if position.status == PositionStatus.OPEN else None
                    ),
                    payload=payload,
                ))
                if position.closed_at:
                    session.merge(PaperTradeRow(id=str(position.id), realized_pnl=position.realized_pnl, payload=payload))
                session.commit()
        except SQLAlchemyError as exc:
            self._failed(exc)

    def load_paper_positions(self) -> list[PaperPosition]:
        if not self.available:
            return []
        try:
            with Session(self.engine) as session:
                return [PaperPosition.model_validate(row.payload) for row in session.scalars(select(PaperPositionRow)).all()]
        except SQLAlchemyError as exc:
            self._failed(exc)
            return []

    def reserve_demo_execution(
        self, record: DemoExecutionRecord
    ) -> DemoExecutionRecord | None:
        """Durably reserve a candidate before any exchange create-order call."""
        if not self.available:
            return None
        try:
            with Session(self.engine) as session, session.begin():
                existing = session.scalar(
                    select(DemoExecutionRow)
                    .where(DemoExecutionRow.candidate_id == str(record.candidate_id))
                    .with_for_update()
                )
                if existing is not None:
                    return DemoExecutionRecord.model_validate(existing.payload)
                candidate = session.get(SignalCandidateRow, str(record.candidate_id))
                if candidate is None:
                    raise ValueError("signal candidate row is missing")
                row = _demo_execution_row(record)
                session.add(row)
                _set_candidate_demo_state(candidate, record.state)
                session.flush()
            return record
        except IntegrityError:
            return self.get_demo_execution(str(record.candidate_id))
        except SQLAlchemyError as exc:
            self._failed(exc)
            return None
        except ValueError as exc:
            self.last_error = type(exc).__name__
            self.last_error_code = "DB_DEMO_RESERVATION_INVALID"
            LOGGER.error("Demo reservation failed: type=%s", type(exc).__name__)
            return None

    def save_demo_execution(
        self, record: DemoExecutionRecord, *, event_type: str
    ) -> bool:
        if not self.available:
            return False
        try:
            with Session(self.engine) as session, session.begin():
                existing = session.scalar(
                    select(DemoExecutionRow)
                    .where(DemoExecutionRow.id == str(record.id))
                    .with_for_update()
                )
                if existing is None:
                    session.add(_demo_execution_row(record))
                else:
                    terminal_values = {
                        DemoExecutionState.DEMO_CLOSED.value,
                        DemoExecutionState.DEMO_CLOSED_AFTER_FAILURE.value,
                        DemoExecutionState.DEMO_FAILED.value,
                        DemoExecutionState.DEMO_NOT_SUBMITTED.value,
                        DemoExecutionState.DEMO_ORDER_CANCELLED.value,
                        DemoExecutionState.DEMO_CLOSED_AFTER_INTERRUPTION.value,
                        DemoExecutionState.DEMO_CLOSED_EXTERNALLY.value,
                        DemoExecutionState.DEMO_FAILED_FLAT_VERIFIED.value,
                    }
                    if (
                        existing.state in terminal_values
                        and record.state.value not in terminal_values
                    ):
                        return True
                    _preserve_demo_execution_exchange_identity(record, existing)
                    _preserve_demo_execution_monotonic_state(record, existing)
                    row = _demo_execution_row(record)
                    for column in (
                        "execution_environment", "risk_decision_id", "run_id",
                        "order_link_id", "order_id", "symbol", "side", "state",
                        "requested_quantity", "accepted_quantity",
                        "average_fill_price", "close_order_link_id", "close_order_id",
                        "realized_exchange_pnl", "payload", "updated_at",
                    ):
                        setattr(existing, column, getattr(row, column))
                candidate = session.get(SignalCandidateRow, str(record.candidate_id))
                if candidate is not None:
                    _set_candidate_demo_state(candidate, record.state)
                session.add(DemoExecutionEventRow(
                    id=str(uuid4()), execution_id=str(record.id),
                    event_key=(f"local:{record.id}:{event_type}:"
                               f"{record.updated_at.isoformat()}"),
                    event_type=event_type,
                    payload=record.model_dump(mode="json"),
                    occurred_at=record.updated_at,
                ))
                session.flush()
            return True
        except IntegrityError:
            return True
        except SQLAlchemyError as exc:
            self._failed(exc)
            return False

    def terminalize_demo_execution(
        self, record: DemoExecutionRecord, *, event_type: str
    ) -> str:
        """Atomically persist one terminal transition and its unique audit event."""
        terminal_states = {
            DemoExecutionState.DEMO_CLOSED,
            DemoExecutionState.DEMO_CLOSED_AFTER_FAILURE,
            DemoExecutionState.DEMO_CLOSED_EXTERNALLY,
        }
        if not self.available or record.state not in terminal_states:
            return "FAILED"
        try:
            with Session(self.engine) as session, session.begin():
                existing = session.scalar(
                    select(DemoExecutionRow)
                    .where(DemoExecutionRow.id == str(record.id))
                    .with_for_update()
                )
                if existing is None:
                    return "FAILED"
                if DemoExecutionState(existing.state) in terminal_states:
                    return "ALREADY_TERMINAL"
                _preserve_demo_execution_exchange_identity(record, existing)
                row = _demo_execution_row(record)
                for column in (
                    "state", "accepted_quantity", "average_fill_price",
                    "close_order_link_id", "close_order_id",
                    "realized_exchange_pnl", "payload", "updated_at",
                ):
                    setattr(existing, column, getattr(row, column))
                candidate = session.get(
                    SignalCandidateRow, str(record.candidate_id)
                )
                if candidate is not None:
                    _set_candidate_demo_state(candidate, record.state)
                session.add(DemoExecutionEventRow(
                    id=str(uuid4()), execution_id=str(record.id),
                    event_key=f"terminal:{record.id}:{record.state.value}",
                    event_type=event_type,
                    payload=record.model_dump(mode="json"),
                    occurred_at=record.updated_at,
                ))
                session.flush()
            return "APPLIED"
        except IntegrityError:
            return "ALREADY_TERMINAL"
        except (SQLAlchemyError, ValueError) as exc:
            self._failed(exc)
            return "FAILED"

    def repair_demo_execution(
        self,
        record: DemoExecutionRecord,
        *,
        event_types: list[str],
        repair_payload: dict[str, Any],
    ) -> bool:
        """Atomically finalize one flat, read-only-verified Demo execution."""
        if not self.available or not event_types:
            return False
        try:
            with Session(self.engine) as session, session.begin():
                existing = session.get(DemoExecutionRow, str(record.id))
                if existing is None:
                    return False
                existing_was_terminal = DemoExecutionState(existing.state) in {
                    DemoExecutionState.DEMO_CLOSED,
                    DemoExecutionState.DEMO_CLOSED_AFTER_FAILURE,
                    DemoExecutionState.DEMO_CLOSED_EXTERNALLY,
                }
                row = _demo_execution_row(record)
                for column in (
                    "state", "accepted_quantity", "average_fill_price",
                    "close_order_id", "realized_exchange_pnl", "payload",
                    "updated_at",
                ):
                    setattr(existing, column, getattr(row, column))
                candidate = session.get(
                    SignalCandidateRow, str(record.candidate_id)
                )
                if candidate is not None:
                    _set_candidate_demo_state(candidate, record.state)
                if "PNL_ACCOUNTING_RECONCILED" in event_types:
                    portfolio_state = session.get(
                        V2PortfolioStateRow, 1, with_for_update=True
                    )
                    if portfolio_state is None:
                        raise ValueError(
                            "V2 portfolio state is missing for accounting repair"
                        )
                    portfolio_state.payload = (
                        _reconcile_v2_portfolio_accounting_payload(
                            dict(portfolio_state.payload),
                            record,
                            allow_missing=not existing_was_terminal,
                        )
                    )
                    portfolio_state.updated_at = record.updated_at
                    reservation = session.scalar(
                        select(V2PortfolioReservationRow)
                        .where(
                            V2PortfolioReservationRow.execution_id
                            == str(record.id)
                        )
                        .with_for_update()
                    )
                    if reservation is not None:
                        reservation_payload = dict(reservation.payload)
                        reservation_payload["state"] = (
                            ReservationState.RELEASED.value
                        )
                        reservation_payload["released_at"] = (
                            record.closed_at or record.updated_at
                        ).isoformat()
                        reservation.state = ReservationState.RELEASED.value
                        reservation.active_symbol = None
                        reservation.released_at = (
                            record.closed_at or record.updated_at
                        )
                        reservation.payload = reservation_payload
                base_time = record.updated_at
                for index, event_type in enumerate(event_types):
                    occurred_at = base_time + timedelta(microseconds=index)
                    event_payload = record.model_dump(mode="json")
                    event_payload["repair"] = dict(repair_payload)
                    session.add(DemoExecutionEventRow(
                        id=str(uuid4()), execution_id=str(record.id),
                        event_key=(
                            f"repair:{record.id}:{event_type}:"
                            f"{base_time.isoformat()}"
                        ),
                        event_type=event_type, payload=event_payload,
                        occurred_at=occurred_at,
                    ))
                session.flush()
            return True
        except SQLAlchemyError as exc:
            self._failed(exc)
            return False

    def get_demo_execution(self, candidate_id: str) -> DemoExecutionRecord | None:
        if not self.available:
            return None
        try:
            with Session(self.engine) as session:
                row = session.scalar(select(DemoExecutionRow).where(
                    DemoExecutionRow.candidate_id == candidate_id
                ))
                return DemoExecutionRecord.model_validate(row.payload) if row else None
        except SQLAlchemyError as exc:
            self._failed(exc)
            return None

    def find_demo_execution(
        self, order_link_id: str, order_id: str
    ) -> DemoExecutionRecord | None:
        if not self.available:
            return None
        try:
            with Session(self.engine) as session:
                predicates = []
                if order_link_id:
                    predicates.append(DemoExecutionRow.order_link_id == order_link_id)
                    predicates.append(DemoExecutionRow.close_order_link_id == order_link_id)
                if order_id:
                    predicates.append(DemoExecutionRow.order_id == order_id)
                    predicates.append(DemoExecutionRow.close_order_id == order_id)
                for predicate in predicates:
                    row = session.scalar(select(DemoExecutionRow).where(predicate))
                    if row is not None:
                        return DemoExecutionRecord.model_validate(row.payload)
            return None
        except SQLAlchemyError as exc:
            self._failed(exc)
            return None

    def load_demo_executions(self) -> list[DemoExecutionRecord]:
        if not self.available:
            return []
        try:
            with Session(self.engine) as session:
                return [
                    DemoExecutionRecord.model_validate(row.payload)
                    for row in session.scalars(select(DemoExecutionRow)).all()
                ]
        except SQLAlchemyError as exc:
            self._failed(exc)
            return []

    def load_demo_execution_events(self, execution_id: str) -> list[dict[str, Any]]:
        """Return the durable audit trail for one execution in timestamp order."""
        if not self.available:
            return []
        try:
            with Session(self.engine) as session:
                rows = session.scalars(
                    select(DemoExecutionEventRow)
                    .where(DemoExecutionEventRow.execution_id == execution_id)
                    .order_by(DemoExecutionEventRow.occurred_at)
                ).all()
                return [
                    {
                        "event_type": row.event_type,
                        "occurred_at": _utc_aware(row.occurred_at).isoformat(),
                        "state": (row.payload or {}).get("state"),
                        "run_id": (row.payload or {}).get("run_id"),
                        "order_link_id": (row.payload or {}).get("order_link_id"),
                        "order_id": (row.payload or {}).get("order_id"),
                        "close_order_link_id": (row.payload or {}).get(
                            "close_order_link_id"
                        ),
                        "close_order_id": (row.payload or {}).get("close_order_id"),
                        "failure_reason": (row.payload or {}).get("failure_reason"),
                        "cleanup_result": (row.payload or {}).get("cleanup_result"),
                        "last_error": (row.payload or {}).get("last_error"),
                        "close_reason": (row.payload or {}).get("close_reason"),
                        "protection_confirmed": (row.payload or {}).get(
                            "protection_confirmed"
                        ),
                        "accepted_quantity": (row.payload or {}).get(
                            "accepted_quantity"
                        ),
                    }
                    for row in rows
                ]
        except SQLAlchemyError as exc:
            self._failed(exc)
            return []

    def update_demo_exit_attribution(
        self,
        execution_id: str,
        *,
        attribution: str,
        evidence: dict[str, Any],
        attribution_failure_reason: str | None = None,
    ) -> bool:
        """Append-only audited analytics repair; exchange economics stay immutable."""
        if not self.available:
            return False
        try:
            with Session(self.engine) as session, session.begin():
                row = session.get(DemoExecutionRow, execution_id)
                if row is None:
                    return False
                record = DemoExecutionRecord.model_validate(row.payload)
                if record.state not in {
                    DemoExecutionState.DEMO_CLOSED,
                    DemoExecutionState.DEMO_CLOSED_AFTER_FAILURE,
                    DemoExecutionState.DEMO_CLOSED_AFTER_INTERRUPTION,
                    DemoExecutionState.DEMO_CLOSED_EXTERNALLY,
                    DemoExecutionState.DEMO_FAILED_FLAT_VERIFIED,
                }:
                    return False
                record.exit_attribution = attribution
                record.close_reason = attribution
                record.exit_attribution_evidence = dict(evidence)
                record.attribution_failure_reason = attribution_failure_reason
                record.updated_at = datetime.now(timezone.utc)
                row.payload = record.model_dump(mode="json")
                row.updated_at = record.updated_at
                session.add(DemoExecutionEventRow(
                    id=str(uuid4()), execution_id=execution_id,
                    event_key=f"analytics-exit-attribution:{execution_id}:{attribution}",
                    event_type="EXIT_ATTRIBUTION_REPAIRED",
                    payload={
                        "execution_id": execution_id,
                        "exit_attribution": attribution,
                        "evidence": dict(evidence),
                        "repaired_at": record.updated_at.isoformat(),
                    },
                    occurred_at=record.updated_at,
                ))
                session.flush()
            return True
        except IntegrityError:
            # Exact attribution repairs are idempotent by event_key.
            return True
        except SQLAlchemyError as exc:
            self._failed(exc)
            return False

    def begin_demo_soak_run(
        self, run_id: str, started_at: datetime
    ) -> dict[str, Any] | None:
        """Persist an idempotent reporting boundary before pipeline processing."""
        if not self.available:
            return None
        try:
            with Session(self.engine) as session, session.begin():
                row = session.get(DemoSoakRunRow, run_id)
                if row is None:
                    row = DemoSoakRunRow(
                        run_id=run_id,
                        started_at=started_at,
                        status="RUNNING",
                        opening_snapshot=self._demo_cumulative_snapshot(session),
                    )
                    session.add(row)
                    session.flush()
                return {
                    "run_id": row.run_id,
                    "started_at": _utc_aware(row.started_at),
                    "status": row.status,
                    "opening_snapshot": dict(row.opening_snapshot),
                }
        except SQLAlchemyError as exc:
            self._failed(exc)
            return None

    def demo_soak_report(
        self, run_id: str, *, finish: bool = False
    ) -> dict[str, Any] | None:
        """Build run and cumulative metrics exclusively from durable records."""
        if not self.available:
            return None
        try:
            with Session(self.engine) as session, session.begin():
                run = session.get(DemoSoakRunRow, run_id)
                if run is None:
                    return None
                candidates = session.scalars(
                    select(SignalCandidateRow).where(
                        SignalCandidateRow.execution_environment
                        == ExecutionEnvironment.BYBIT_DEMO.value,
                        SignalCandidateRow.run_id == run_id,
                        SignalCandidateRow.created_at >= run.started_at,
                    ).order_by(SignalCandidateRow.created_at)
                ).all()
                executions = session.scalars(
                    select(DemoExecutionRow).where(
                        DemoExecutionRow.execution_environment
                        == ExecutionEnvironment.BYBIT_DEMO.value,
                        DemoExecutionRow.run_id == run_id,
                        DemoExecutionRow.created_at >= run.started_at,
                    )
                ).all()
                news = session.scalars(
                    select(NewsItemRow).where(NewsItemRow.received_at >= run.started_at)
                ).all()
                classifications = session.scalars(
                    select(NewsClassificationRow).where(
                        NewsClassificationRow.classified_at >= run.started_at
                    )
                ).all()
                news_by_id = {
                    row.id: row
                    for row in session.scalars(select(NewsItemRow)).all()
                }
                execution_by_candidate = {row.candidate_id: row for row in executions}
                candidate_ids = {row.id for row in candidates}
                if any(row.candidate_id not in candidate_ids for row in executions):
                    raise ValueError(
                        "Demo order exists without a current-run durable candidate"
                    )
                state_counts: dict[str, int] = {}
                details: list[dict[str, Any]] = []
                for candidate in candidates:
                    if candidate.state.startswith("PAPER_") or candidate.state == "EXECUTING_PAPER":
                        raise ValueError("Demo run contains PAPER candidate state")
                    state_counts[candidate.state] = state_counts.get(candidate.state, 0) + 1
                    payload = dict(candidate.payload or {})
                    preview = dict(candidate.risk_preview or {})
                    execution = execution_by_candidate.get(candidate.id)
                    item = news_by_id.get(candidate.news_id)
                    execution_payload = dict(execution.payload or {}) if execution else {}
                    candidate_reasons = list(payload.get("reasons", []))
                    details.append({
                        "candidate_id": candidate.id,
                        "created_at": candidate.created_at.isoformat(),
                        "news_title": item.title if item else None,
                        "symbol": candidate.symbol,
                        "final_state": candidate.state,
                        "final_action": payload.get("final_action"),
                        "expected_edge_bps": payload.get("expected_edge_bps"),
                        "risk_result": {
                            "preview_performed": preview.get("preview_performed"),
                            "approved": preview.get("approved"),
                            "reasons": preview.get("rejection_reasons", []),
                        },
                        "execution_result": execution.state if execution else "NOT_SUBMITTED",
                        "block_or_expiry_reason": (
                            "; ".join(candidate_reasons)
                            or execution_payload.get("last_error")
                            or None
                        ),
                    })
                if len(candidates) != sum(state_counts.values()):
                    raise ValueError("current-run candidate state totals are inconsistent")
                accepted_news_ids = {row.news_id for row in classifications}
                fees = sum(
                    Decimal(str((row.payload or {}).get("exchange_fees", "0")))
                    for row in executions
                )
                realized = sum(
                    Decimal(str(row.realized_exchange_pnl or 0)) for row in executions
                )
                final_snapshot = self._demo_cumulative_snapshot(session)
                if finish:
                    run.finished_at = datetime.now(timezone.utc)
                    run.status = "COMPLETED"
                    run.final_snapshot = final_snapshot
                activity = {
                    "news_seen_this_run": len(news),
                    "news_accepted_this_run": len(accepted_news_ids),
                    "classifications_this_run": len(classifications),
                    "trade_eligible_classifications_this_run": sum(
                        bool((row.payload or {}).get("trade_eligible"))
                        for row in classifications
                    ),
                    "candidates_created_this_run": len(candidates),
                    "candidate_final_states_this_run": state_counts,
                    "orders_submitted_this_run": len(executions),
                    "orders_accepted_this_run": sum(bool(row.order_id) for row in executions),
                    "orders_rejected_this_run": sum(
                        row.state == DemoExecutionState.DEMO_FAILED.value
                        for row in executions
                    ),
                    "fills_this_run": sum(
                        len((row.payload or {}).get("fills", []))
                        + len((row.payload or {}).get("close_fills", []))
                        for row in executions
                    ),
                    "positions_opened_this_run": sum(
                        bool((row.payload or {}).get("protection_confirmed"))
                        for row in executions
                    ),
                    "positions_closed_this_run": sum(
                        row.state == DemoExecutionState.DEMO_CLOSED.value
                        for row in executions
                    ),
                    "exchange_fees_this_run": str(fees),
                    "realized_pnl_this_run": str(realized),
                }
                return {
                    "run_id": run.run_id,
                    "started_at": _utc_aware(run.started_at).isoformat(),
                    "finished_at": _utc_aware(run.finished_at).isoformat() if run.finished_at else None,
                    "status": run.status,
                    "opening_state": dict(run.opening_snapshot),
                    "activity_this_run": activity,
                    "current_run_candidates": details,
                    "final_cumulative_state": final_snapshot,
                }
        except SQLAlchemyError as exc:
            self._failed(exc)
            return None

    @staticmethod
    def _demo_cumulative_snapshot(session: Session) -> dict[str, int]:
        candidate_count = int(
            session.scalar(select(func.count()).select_from(SignalCandidateRow)) or 0
        )
        return {
            "preexisting_candidates": candidate_count,
            "cumulative_candidates": candidate_count,
            "cumulative_demo_executions": int(
                session.scalar(select(func.count()).select_from(DemoExecutionRow)) or 0
            ),
            "cumulative_news": int(
                session.scalar(select(func.count()).select_from(NewsItemRow)) or 0
            ),
            "cumulative_classifications": int(
                session.scalar(select(func.count()).select_from(NewsClassificationRow)) or 0
            ),
        }

    def record_demo_event(
        self, event_key: str, event_type: str, payload: dict[str, Any]
    ) -> bool:
        """Return False for an already processed private-stream event."""
        if not self.available:
            return False
        try:
            with Session(self.engine) as session:
                session.add(DemoExecutionEventRow(
                    id=str(uuid4()), execution_id=None, event_key=event_key,
                    event_type=event_type, payload=payload,
                    occurred_at=datetime.now(timezone.utc),
                ))
                session.commit()
            return True
        except IntegrityError:
            return False
        except SQLAlchemyError as exc:
            self._failed(exc)
            return False

    def reserve_demo_canary_job(
        self, job_id: str, run_id: str, symbol: str, request_payload: dict[str, Any]
    ) -> dict[str, Any] | None:
        if not self.available:
            return None
        now = datetime.now(timezone.utc)
        try:
            with Session(self.engine) as session:
                existing = session.scalar(
                    select(DemoCanaryJobRow).where(DemoCanaryJobRow.run_id == run_id)
                )
                if existing is None:
                    session.add(DemoCanaryJobRow(
                        id=job_id, run_id=run_id, symbol=symbol, status="PENDING",
                        execution_id=None, request_payload=request_payload,
                        result_payload=None, error_code=None,
                        created_at=now, updated_at=now,
                    ))
                    session.commit()
            return self.get_demo_canary_job(job_id=job_id, run_id=run_id)
        except SQLAlchemyError as exc:
            self._failed(exc)
            return None

    def update_demo_canary_job(
        self, job_id: str, *, status: str,
        execution_id: str | None = None,
        result_payload: dict[str, Any] | None = None,
        error_code: str | None = None,
    ) -> bool:
        if not self.available:
            return False
        try:
            with Session(self.engine) as session:
                row = session.get(DemoCanaryJobRow, job_id)
                if row is None:
                    return False
                row.status = status
                if execution_id is not None:
                    row.execution_id = execution_id
                if result_payload is not None:
                    row.result_payload = result_payload
                row.error_code = error_code
                row.updated_at = datetime.now(timezone.utc)
                session.commit()
            return True
        except SQLAlchemyError as exc:
            self._failed(exc)
            return False

    def get_demo_canary_job(
        self, *, job_id: str | None = None, run_id: str | None = None
    ) -> dict[str, Any] | None:
        if not self.available or (job_id is None and run_id is None):
            return None
        try:
            with Session(self.engine) as session:
                row = (
                    session.get(DemoCanaryJobRow, job_id)
                    if job_id is not None
                    else session.scalar(select(DemoCanaryJobRow).where(
                        DemoCanaryJobRow.run_id == run_id
                    ))
                )
                if row is None:
                    return None
                return {
                    "job_id": row.id, "run_id": row.run_id,
                    "symbol": row.symbol, "status": row.status,
                    "execution_id": row.execution_id,
                    "request": dict(row.request_payload),
                    "result": dict(row.result_payload) if row.result_payload else None,
                    "error_code": row.error_code,
                    "created_at": row.created_at.isoformat(),
                    "updated_at": row.updated_at.isoformat(),
                }
        except SQLAlchemyError as exc:
            self._failed(exc)
            return None

    def recoverable_demo_canary_jobs(self) -> list[dict[str, Any]]:
        if not self.available:
            return []
        try:
            with Session(self.engine) as session:
                rows = session.scalars(select(DemoCanaryJobRow).where(
                    DemoCanaryJobRow.status.in_(["PENDING", "RUNNING"])
                )).all()
                return [self.get_demo_canary_job(job_id=row.id) for row in rows]
        except SQLAlchemyError as exc:
            self._failed(exc)
            return []

    def load_demo_kill_switch(self) -> dict[str, Any] | None:
        if not self.available:
            return None
        try:
            with Session(self.engine) as session:
                row = session.get(DemoKillSwitchRow, 1)
                if row is None:
                    return None
                events = session.scalars(
                    select(DemoKillSwitchEventRow).order_by(
                        DemoKillSwitchEventRow.created_at
                    )
                ).all()
                activation_events = [
                    event for event in events
                    if event.event_type in {
                        "KILL_SWITCH_ACTIVATED", "LEGACY_ACTIVATION"
                    }
                ]
                return {
                    "active": row.active,
                    "reasons": list(row.reasons),
                    "updated_at": row.updated_at,
                    "activated_at": (
                        activation_events[-1].created_at
                        if activation_events else row.updated_at
                    ),
                    "activation_count": len(activation_events) or int(row.active),
                    "events": [
                        {
                            "id": event.id,
                            "event_type": event.event_type,
                            "active": event.active,
                            "reasons": list(event.reasons),
                            "execution_id": event.execution_id,
                            "payload": dict(event.payload or {}),
                            "created_at": event.created_at,
                        }
                        for event in events
                    ],
                }
        except SQLAlchemyError as exc:
            self._failed(exc)
            return None

    def save_demo_kill_switch(self, active: bool, reasons: list[str]) -> bool:
        if not self.available:
            return False
        try:
            with Session(self.engine) as session, session.begin():
                now = datetime.now(timezone.utc)
                existing = session.get(DemoKillSwitchRow, 1)
                old_active = bool(existing.active) if existing else False
                old_reasons = list(existing.reasons) if existing else []
                session.merge(DemoKillSwitchRow(
                    id=1, active=active, reasons=list(reasons), updated_at=now,
                ))
                event_type = None
                if active and not old_active:
                    event_type = "KILL_SWITCH_ACTIVATED"
                elif active and list(reasons) != old_reasons:
                    event_type = "KILL_SWITCH_REASON_ADDED"
                elif not active and old_active:
                    event_type = "KILL_SWITCH_CLEARED"
                if event_type:
                    session.add(DemoKillSwitchEventRow(
                        id=str(uuid4()), event_type=event_type, active=active,
                        reasons=list(reasons), execution_id=None,
                        payload={}, created_at=now,
                    ))
            return True
        except SQLAlchemyError as exc:
            self._failed(exc)
            return False

    def reset_demo_kill_switch(
        self, execution_id: str, *, reason: str
    ) -> bool:
        """Clear only the Demo latch and preserve an immutable audit event."""
        if not self.available:
            return False
        try:
            with Session(self.engine) as session, session.begin():
                row = session.get(DemoKillSwitchRow, 1)
                execution = session.get(DemoExecutionRow, execution_id)
                if row is None or not row.active or execution is None:
                    return False
                now = datetime.now(timezone.utc)
                preserved_reasons = list(row.reasons)
                row.active = False
                row.updated_at = now
                session.add(DemoKillSwitchEventRow(
                    id=str(uuid4()), event_type="KILL_SWITCH_RESET",
                    active=False, reasons=preserved_reasons,
                    execution_id=execution_id,
                    payload={"reset_reason": reason[:250]}, created_at=now,
                ))
                session.flush()
            return True
        except SQLAlchemyError as exc:
            self._failed(exc)
            return False

    def link_demo_kill_switch_execution(
        self, execution_id: str, *, reason: str
    ) -> bool:
        """Append, never rewrite, guarded historical incident linkage."""
        if not self.available:
            return False
        try:
            with Session(self.engine) as session, session.begin():
                row = session.get(DemoKillSwitchRow, 1)
                execution = session.get(DemoExecutionRow, execution_id)
                if row is None or not row.active or execution is None:
                    return False
                existing = session.scalar(select(DemoKillSwitchEventRow).where(
                    DemoKillSwitchEventRow.event_type
                    == "KILL_SWITCH_EXECUTION_LINK_REPAIRED",
                    DemoKillSwitchEventRow.execution_id == execution_id,
                ))
                if existing is not None:
                    return True
                session.add(DemoKillSwitchEventRow(
                    id=str(uuid4()),
                    event_type="KILL_SWITCH_EXECUTION_LINK_REPAIRED",
                    active=True,
                    reasons=list(row.reasons),
                    execution_id=execution_id,
                    payload={"link_reason": reason[:250]},
                    created_at=datetime.now(timezone.utc),
                ))
                session.flush()
            return True
        except SQLAlchemyError as exc:
            self._failed(exc)
            return False

    def repair_demo_kill_switch_activation_link(
        self,
        *,
        activation_id: str,
        execution_id: str,
        run_id: str,
        evidence: dict[str, Any],
    ) -> bool:
        """Append an explicit activation link after exact durable verification."""
        if not self.available:
            return False
        try:
            with Session(self.engine) as session, session.begin():
                activation = session.get(DemoKillSwitchEventRow, activation_id)
                execution = session.get(DemoExecutionRow, execution_id)
                if activation is None or execution is None:
                    return False
                if activation.event_type != "KILL_SWITCH_ACTIVATED":
                    return False
                if activation.execution_id is not None:
                    return activation.execution_id == execution_id
                if execution.run_id != run_id or execution.symbol != "BTCUSDT":
                    return False
                if not (
                    execution.created_at <= activation.created_at <= execution.updated_at
                ):
                    return False
                if not any(
                    "unattributed active Demo order for BTCUSDT" in str(reason)
                    for reason in activation.reasons
                ):
                    return False
                existing = session.scalar(select(DemoKillSwitchEventRow).where(
                    DemoKillSwitchEventRow.event_type
                    == "KILL_SWITCH_EXECUTION_LINK_REPAIRED",
                    DemoKillSwitchEventRow.execution_id == execution_id,
                ))
                if existing is not None:
                    return True
                now = datetime.now(timezone.utc)
                session.add(DemoKillSwitchEventRow(
                    id=str(uuid4()),
                    event_type="KILL_SWITCH_EXECUTION_LINK_REPAIRED",
                    active=True,
                    reasons=list(activation.reasons),
                    execution_id=execution_id,
                    payload={
                        "activation_id": activation_id,
                        "execution_id": execution_id,
                        "run_id": run_id,
                        "repair_timestamp": now.isoformat(),
                        "evidence": evidence,
                    },
                    created_at=now,
                ))
                session.flush()
            return True
        except SQLAlchemyError as exc:
            self._failed(exc)
            return False

    def save_v2_universe_status(self, status: UniverseStatus) -> bool:
        if not self.available:
            return False
        try:
            with Session(self.engine) as session, session.begin():
                row = session.get(V2UniverseStateRow, status.symbol.value)
                values = {
                    "state": status.state.value, "accepted": status.accepted,
                    "rejection_reasons": list(status.reasons),
                    "payload": status.model_dump(mode="json"),
                    "checked_at": status.checked_at,
                }
                if row is None:
                    session.add(V2UniverseStateRow(symbol=status.symbol.value, **values))
                else:
                    for key, value in values.items():
                        setattr(row, key, value)
            return True
        except SQLAlchemyError as exc:
            self._failed(exc)
            return False

    def load_v2_universe(self) -> list[UniverseStatus]:
        if not self.available:
            return []
        try:
            with Session(self.engine) as session:
                return [UniverseStatus.model_validate(row.payload) for row in session.scalars(select(V2UniverseStateRow)).all()]
        except SQLAlchemyError as exc:
            self._failed(exc)
            return []

    def save_v2_market_feature(self, feature: Any) -> bool:
        if not self.available:
            return False
        try:
            with Session(self.engine) as session, session.begin():
                session.add(V2MarketFeatureRow(
                    symbol=feature.symbol.value, captured_at=feature.timestamp,
                    fresh=feature.fresh, payload=feature.model_dump(mode="json"),
                ))
            return True
        except IntegrityError:
            return True
        except SQLAlchemyError as exc:
            self._failed(exc)
            return False

    def save_v4_opportunity(self, opportunity: V4Opportunity) -> bool:
        """Persist one additive shadow/research tape row idempotently."""
        if not self.available:
            return False
        try:
            with Session(self.engine) as session, session.begin():
                key = str(opportunity.opportunity_id)
                if session.get(V4OpportunityRow, key) is None:
                    session.add(V4OpportunityRow(
                        opportunity_id=key,
                        cycle_id=opportunity.cycle_id,
                        run_id=opportunity.run_id,
                        symbol=opportunity.symbol,
                        side=opportunity.side,
                        source=opportunity.source,
                        candidate_type=opportunity.candidate_type,
                        decision=opportunity.decision.value,
                        rejected=bool(opportunity.rejection_reasons),
                        feature_snapshot_at=opportunity.feature_snapshot_time,
                        decision_at=opportunity.decision_time,
                        payload=opportunity.model_dump(mode="json"),
                        created_at=datetime.now(timezone.utc),
                    ))
            return True
        except IntegrityError:
            return True
        except SQLAlchemyError as exc:
            self._failed(exc)
            return False

    def load_v4_opportunities(self, run_id: str | None = None) -> list[V4Opportunity]:
        if not self.available:
            return []
        try:
            with Session(self.engine) as session:
                statement = select(V4OpportunityRow).order_by(V4OpportunityRow.decision_at)
                if run_id:
                    statement = statement.where(V4OpportunityRow.run_id == run_id)
                return [
                    V4Opportunity.model_validate(row.payload)
                    for row in session.scalars(statement).all()
                ]
        except SQLAlchemyError as exc:
            self._failed(exc)
            return []

    def save_v4_forward_label(self, label: V4ForwardLabel) -> bool:
        """Upsert a progressively maturing market-path label."""
        if not self.available:
            return False
        try:
            with Session(self.engine) as session, session.begin():
                key = str(label.opportunity_id)
                row = session.get(V4ForwardLabelRow, key)
                values = {
                    "symbol": label.symbol,
                    "decision_at": label.decision_time,
                    "maximum_horizon_seconds": label.maximum_horizon_seconds,
                    "complete": label.complete,
                    "payload": label.model_dump(mode="json"),
                    "updated_at": datetime.now(timezone.utc),
                }
                if row is None:
                    session.add(V4ForwardLabelRow(opportunity_id=key, **values))
                else:
                    for name, value in values.items():
                        setattr(row, name, value)
            return True
        except SQLAlchemyError as exc:
            self._failed(exc)
            return False

    def save_v2_signal_candidate(self, candidate: V2SignalCandidate) -> bool:
        if not self.available:
            return False
        try:
            with Session(self.engine) as session, session.begin():
                row = session.get(V2SignalCandidateRow, str(candidate.id))
                values = {
                    "run_id": candidate.run_id,
                    "strategy_name": candidate.strategy_name.value,
                    "strategy_version": candidate.strategy_version,
                    "symbol": candidate.symbol.value, "side": candidate.side.value,
                    "state": candidate.state, "admitted": candidate.admitted,
                    "final_score": (
                        candidate.score_components.final_score
                        if candidate.score_components else None
                    ),
                    "rejection_reason": candidate.rejection_reason,
                    "payload": candidate.model_dump(mode="json"),
                    "created_at": candidate.created_at, "expires_at": candidate.expires_at,
                }
                if row is None:
                    session.add(V2SignalCandidateRow(id=str(candidate.id), **values))
                else:
                    for key, value in values.items():
                        setattr(row, key, value)
                if candidate.rejection_reason:
                    rejection_id = str(candidate.id)
                    if session.get(V2RejectionRow, rejection_id) is None:
                        session.add(V2RejectionRow(
                            id=rejection_id, run_id=candidate.run_id,
                            candidate_id=str(candidate.id),
                            strategy_name=candidate.strategy_name.value,
                            symbol=candidate.symbol.value,
                            reason=candidate.rejection_reason,
                            payload=candidate.model_dump(mode="json"),
                            created_at=candidate.created_at,
                        ))
            return True
        except IntegrityError:
            return False
        except SQLAlchemyError as exc:
            self._failed(exc)
            return False

    def load_v2_signal_candidates(self, run_id: str | None = None) -> list[V2SignalCandidate]:
        if not self.available:
            return []
        try:
            with Session(self.engine) as session:
                statement = select(V2SignalCandidateRow).order_by(V2SignalCandidateRow.created_at)
                if run_id:
                    statement = statement.where(V2SignalCandidateRow.run_id == run_id)
                return [V2SignalCandidate.model_validate(row.payload) for row in session.scalars(statement).all()]
        except SQLAlchemyError as exc:
            self._failed(exc)
            return []

    def load_v2_calibration_candidates(self) -> list[V2SignalCandidate]:
        """Load only candidates which can contribute a realized observation.

        Loading every historical V2 candidate also transfers each large feature
        snapshot JSON document. Long validation runs can create thousands of
        rejected candidates, so that full-table restore is neither necessary
        nor acceptable on application startup.
        """
        if not self.available:
            return []
        try:
            with Session(self.engine) as session:
                statement = (
                    select(V2SignalCandidateRow)
                    .join(
                        DemoExecutionRow,
                        DemoExecutionRow.candidate_id == V2SignalCandidateRow.id,
                    )
                    .where(
                        DemoExecutionRow.realized_exchange_pnl.is_not(None),
                        DemoExecutionRow.accepted_quantity > 0,
                    )
                    .order_by(V2SignalCandidateRow.created_at)
                )
                return [
                    V2SignalCandidate.model_validate(row.payload)
                    for row in session.scalars(statement).unique().all()
                ]
        except SQLAlchemyError as exc:
            self._failed(exc)
            return []

    def reserve_v2_portfolio(
        self, reservation: PortfolioReservation, settings: Any,
    ) -> PortfolioReservation | None:
        if not self.available:
            return None
        try:
            with Session(self.engine) as session, session.begin():
                existing = session.scalar(select(V2PortfolioReservationRow).where(
                    V2PortfolioReservationRow.candidate_id == str(reservation.candidate_id)
                ).with_for_update())
                if existing:
                    return PortfolioReservation.model_validate(existing.payload)
                active = session.scalars(select(V2PortfolioReservationRow).where(
                    V2PortfolioReservationRow.active_symbol.is_not(None)
                ).with_for_update()).all()
                if len(active) >= settings.max_concurrent_positions:
                    return None
                if any(row.symbol == reservation.symbol.value for row in active):
                    return None
                if sum(row.correlation_group == reservation.correlation_group for row in active) >= settings.max_positions_per_correlation_group:
                    return None
                meme_symbols = {"DOGEUSDT", "PEPEUSDT", "SHIBUSDT", "WIFUSDT", "BONKUSDT", "FLOKIUSDT"}
                if reservation.symbol.value in meme_symbols and sum(row.symbol in meme_symbols for row in active) >= settings.max_meme_positions:
                    return None
                if sum(Decimal(str(row.notional_usdt)) for row in active) + reservation.notional_usdt > settings.max_total_notional_usdt:
                    return None
                if sum(Decimal(str(row.risk_usdt)) for row in active) + reservation.risk_usdt > settings.risk_capital_usdt * settings.max_portfolio_risk_pct / Decimal("100"):
                    return None
                now = datetime.now(timezone.utc)
                recent_count = session.scalar(select(func.count()).select_from(V2PortfolioReservationRow).where(
                    V2PortfolioReservationRow.created_at >= now - timedelta(minutes=5)
                )) or 0
                if recent_count >= settings.max_new_entries_per_5_minutes:
                    return None
                day_count = session.scalar(select(func.count()).select_from(V2PortfolioReservationRow).where(
                    V2PortfolioReservationRow.created_at >= now.replace(hour=0, minute=0, second=0, microsecond=0)
                )) or 0
                if day_count >= settings.max_trades_per_day:
                    return None
                row = V2PortfolioReservationRow(
                    id=str(reservation.id), run_id=reservation.run_id,
                    candidate_id=str(reservation.candidate_id), execution_id=None,
                    symbol=reservation.symbol.value, active_symbol=reservation.symbol.value,
                    correlation_group=reservation.correlation_group,
                    strategy_name=reservation.strategy_name.value,
                    state=reservation.state.value,
                    notional_usdt=reservation.notional_usdt, risk_usdt=reservation.risk_usdt,
                    payload=reservation.model_dump(mode="json"),
                    created_at=reservation.created_at, released_at=None,
                )
                session.add(row)
                session.flush()
            return reservation
        except IntegrityError:
            return None
        except SQLAlchemyError as exc:
            self._failed(exc)
            return None

    def update_v2_portfolio_reservation(self, reservation: PortfolioReservation) -> bool:
        if not self.available:
            return False
        try:
            with Session(self.engine) as session, session.begin():
                row = session.get(V2PortfolioReservationRow, str(reservation.id))
                if row is None:
                    return False
                row.state = reservation.state.value
                row.execution_id = str(reservation.execution_id) if reservation.execution_id else None
                row.active_symbol = (
                    reservation.symbol.value
                    if reservation.state in {ReservationState.RESERVED, ReservationState.EXECUTING, ReservationState.OPEN}
                    else None
                )
                row.released_at = reservation.released_at
                row.payload = reservation.model_dump(mode="json")
            return True
        except SQLAlchemyError as exc:
            self._failed(exc)
            return False

    def save_v2_portfolio_state(self, payload: dict[str, Any]) -> bool:
        if not self.available:
            return False
        now = datetime.now(timezone.utc)
        try:
            with Session(self.engine) as session, session.begin():
                row = session.get(V2PortfolioStateRow, 1)
                if row is None:
                    session.add(V2PortfolioStateRow(id=1, payload=payload, updated_at=now))
                else:
                    row.payload = payload; row.updated_at = now
            return True
        except SQLAlchemyError as exc:
            self._failed(exc)
            return False

    def load_v2_portfolio_state(self) -> dict[str, Any] | None:
        if not self.available:
            return None
        try:
            with Session(self.engine) as session:
                state = session.get(V2PortfolioStateRow, 1)
                reservations = session.scalars(select(V2PortfolioReservationRow)).all()
                payload = dict(state.payload) if state else {}
                payload["reservations"] = [row.payload for row in reservations]
                return payload
        except SQLAlchemyError as exc:
            self._failed(exc)
            return None

    def begin_v2_run(self, run_id: str, started_at: datetime) -> bool:
        if not self.available:
            return False
        try:
            with Session(self.engine) as session, session.begin():
                if session.get(V2RunRow, run_id) is None:
                    session.add(V2RunRow(
                        run_id=run_id, started_at=started_at, finished_at=None,
                        status="RUNNING", payload={"run_id": run_id},
                    ))
            return True
        except SQLAlchemyError as exc:
            self._failed(exc)
            return False

    def finish_v2_run(self, run_id: str, payload: dict[str, Any]) -> bool:
        if not self.available:
            return False
        try:
            with Session(self.engine) as session, session.begin():
                row = session.get(V2RunRow, run_id)
                if row is None:
                    return False
                row.finished_at = datetime.now(timezone.utc)
                row.status = "FINISHED"
                row.payload = payload
            return True
        except SQLAlchemyError as exc:
            self._failed(exc)
            return False

    def update_v2_run_runtime(self, run_id: str, payload: dict[str, Any]) -> bool:
        if not self.available:
            return False
        try:
            with Session(self.engine) as session, session.begin():
                row = session.get(V2RunRow, run_id)
                if row is None:
                    return False
                current = dict(row.payload or {})
                existing_runtime = dict(current.get("runtime") or {})
                existing_finalization = dict(
                    existing_runtime.get("run_finalization") or {}
                )
                incoming = dict(payload)
                incoming_finalization = dict(
                    incoming.get("run_finalization") or {}
                )
                phase_order = {
                    "RUNNING": 0,
                    "DRAINING": 1,
                    "RECONCILING": 2,
                    "FINISHED": 3,
                }
                existing_phase = max(
                    (
                        str(existing_finalization.get("phase") or "RUNNING"),
                        str(row.status or "RUNNING"),
                    ),
                    key=lambda value: phase_order.get(value, -1),
                )
                if not incoming_finalization:
                    incoming_finalization = existing_finalization
                incoming_phase = str(
                    incoming_finalization.get("phase") or existing_phase
                )
                if phase_order.get(incoming_phase, -1) < phase_order.get(
                    existing_phase, -1
                ):
                    incoming_finalization = (
                        existing_finalization or {"phase": existing_phase}
                    )
                    incoming["run_finalization"] = incoming_finalization
                    incoming_phase = existing_phase
                elif incoming_finalization:
                    incoming["run_finalization"] = incoming_finalization
                current["runtime"] = incoming
                if phase_order.get(incoming_phase, -1) >= phase_order.get(
                    str(row.status), -1
                ):
                    row.status = incoming_phase
                if incoming_phase == "FINISHED" and row.finished_at is None:
                    row.finished_at = datetime.now(timezone.utc)
                row.payload = current
            return True
        except SQLAlchemyError as exc:
            self._failed(exc)
            return False

    def load_v2_run_runtime(self, run_id: str) -> dict[str, Any]:
        if not self.available:
            return {}
        try:
            with Session(self.engine) as session:
                row = session.get(V2RunRow, run_id)
                return dict((row.payload or {}).get("runtime") or {}) if row else {}
        except SQLAlchemyError as exc:
            self._failed(exc)
            return {}

    def load_v2_run_state(self, run_id: str) -> dict[str, Any]:
        if not self.available:
            return {}
        try:
            with Session(self.engine) as session:
                row = session.get(V2RunRow, run_id)
                if row is None:
                    return {}
                return {
                    "run_id": row.run_id,
                    "started_at": _utc_aware(row.started_at).isoformat(),
                    "finished_at": (
                        _utc_aware(row.finished_at).isoformat()
                        if row.finished_at else None
                    ),
                    "status": row.status,
                    "runtime": dict(
                        (row.payload or {}).get("runtime") or {}
                    ),
                }
        except SQLAlchemyError as exc:
            self._failed(exc)
            return {}

    def save_v2_incident(self, incident: V2Incident) -> bool:
        if not self.available:
            return False
        try:
            with Session(self.engine) as session, session.begin():
                row = session.get(V2IncidentRow, str(incident.id))
                payload = incident.model_dump(mode="json")
                if row is None:
                    session.add(V2IncidentRow(
                        id=str(incident.id), run_id=incident.run_id,
                        event_type=incident.event_type,
                        symbol=incident.symbol.value if incident.symbol else None,
                        execution_id=str(incident.execution_id) if incident.execution_id else None,
                        candidate_id=str(incident.candidate_id) if incident.candidate_id else None,
                        payload=payload, occurred_at=incident.occurred_at,
                    ))
                else:
                    row.payload = payload
            return True
        except IntegrityError:
            return True
        except SQLAlchemyError as exc:
            self._failed(exc)
            return False

    def v2_report_rows(self, run_id: str) -> dict[str, list[dict[str, Any]]]:
        if not self.available:
            return {
                "signals": [], "rejections": [], "incidents": [],
                "executions": [], "runtime": {},
            }
        try:
            with Session(self.engine) as session:
                signals = session.scalars(select(V2SignalCandidateRow).where(V2SignalCandidateRow.run_id == run_id)).all()
                rejections = session.scalars(select(V2RejectionRow).where(V2RejectionRow.run_id == run_id)).all()
                incidents = session.scalars(select(V2IncidentRow).where(V2IncidentRow.run_id == run_id)).all()
                executions = session.scalars(select(DemoExecutionRow).where(DemoExecutionRow.run_id == run_id)).all()
                run = session.get(V2RunRow, run_id)
                return {
                    "signals": [row.payload for row in signals],
                    "rejections": [row.payload for row in rejections],
                    "incidents": [row.payload for row in incidents],
                    "executions": [row.payload for row in executions],
                    "runtime": dict((run.payload or {}).get("runtime") or {}) if run else {},
                }
        except SQLAlchemyError as exc:
            self._failed(exc)
            return {
                "signals": [], "rejections": [], "incidents": [],
                "executions": [], "runtime": {},
            }

    def _failed(self, exc: SQLAlchemyError) -> None:
        self.available = False
        self.last_error = type(exc).__name__
        self.last_error_code = _database_error_code(exc)
        self.last_sqlstate = _sqlstate(exc)
        LOGGER.error(
            "database operation failed: type=%s message=%s",
            type(exc).__name__,
            _sanitize_database_error(str(exc)),
        )


def _preserve_demo_execution_exchange_identity(
    record: DemoExecutionRecord, existing: DemoExecutionRow,
) -> None:
    """Never let sparse/stale lifecycle payloads erase exact exchange IDs."""
    payload = existing.payload or {}
    if not record.order_id:
        record.order_id = existing.order_id or payload.get("order_id")
    if not record.order_link_id:
        record.order_link_id = existing.order_link_id or str(
            payload.get("order_link_id") or ""
        )
    if not record.close_order_id:
        record.close_order_id = existing.close_order_id or payload.get(
            "close_order_id"
        )
    if not record.close_order_link_id:
        record.close_order_link_id = (
            existing.close_order_link_id or payload.get("close_order_link_id")
        )


def _preserve_demo_execution_monotonic_state(
    record: DemoExecutionRecord, existing: DemoExecutionRow,
) -> None:
    """Merge late lifecycle evidence without regressing durable exposure state."""
    try:
        current = DemoExecutionRecord.model_validate(existing.payload)
    except (TypeError, ValueError):
        return
    rank = {
        DemoExecutionState.DEMO_SUBMITTING: 0,
        DemoExecutionState.DEMO_ORDER_ACKNOWLEDGED: 1,
        DemoExecutionState.DEMO_ACCEPTED: 1,
        DemoExecutionState.DEMO_PARTIALLY_FILLED: 2,
        DemoExecutionState.DEMO_FILLED: 3,
        DemoExecutionState.DEMO_FULLY_FILLED: 3,
        DemoExecutionState.DEMO_PROTECTION_PENDING: 4,
        DemoExecutionState.DEMO_POSITION_OPEN: 5,
        DemoExecutionState.DEMO_CLOSING: 6,
        DemoExecutionState.DEMO_RECONCILIATION_REQUIRED: 6,
    }
    if rank.get(record.state, 99) >= rank.get(current.state, -1):
        return

    record.state = current.state
    for name in (
        "protection_confirmed", "take_profit", "stop_loss", "tp_identifier",
        "sl_identifier", "tp_order_id", "sl_order_id",
        "protection_position_idx", "protection_orders_verified_at",
        "position_confirmed_at", "protection_confirmed_at",
        "close_order_id", "close_order_link_id", "average_close_price",
        "gross_realized_pnl", "realized_exchange_pnl", "paper_shadow_pnl",
        "entry_fees", "close_fees", "funding_pnl",
        "funding_transaction_ids", "accounting_components",
        "authoritative_closed_pnl", "accounting_status",
        "accounting_finalized_at",
        "close_reason", "exit_attribution", "exit_attribution_evidence",
        "closed_at", "cleanup_result",
    ):
        current_value = getattr(current, name)
        if current_value not in (None, "", [], {}):
            setattr(record, name, current_value)
    record.fills = _merge_demo_fills(current.fills, record.fills)
    record.close_fills = _merge_demo_fills(
        current.close_fills, record.close_fills
    )


def _merge_demo_fills(existing: list[Any], incoming: list[Any]) -> list[Any]:
    merged = {item.execution_id: item for item in existing}
    merged.update({item.execution_id: item for item in incoming})
    return sorted(merged.values(), key=lambda item: item.executed_at)


def _demo_execution_row(record: DemoExecutionRecord) -> DemoExecutionRow:
    return DemoExecutionRow(
        id=str(record.id),
        candidate_id=str(record.candidate_id),
        execution_environment=record.execution_environment.value,
        risk_decision_id=record.risk_decision_id,
        run_id=record.run_id,
        order_link_id=record.order_link_id,
        order_id=record.order_id,
        symbol=record.symbol.value,
        side=record.side.value,
        state=record.state.value,
        requested_quantity=record.requested_quantity,
        accepted_quantity=record.accepted_quantity,
        average_fill_price=record.average_fill_price,
        close_order_link_id=record.close_order_link_id,
        close_order_id=record.close_order_id,
        realized_exchange_pnl=record.realized_exchange_pnl,
        payload=record.model_dump(mode="json"),
        created_at=record.created_at,
        updated_at=record.updated_at,
    )


def _reconcile_v2_portfolio_accounting_payload(
    payload: dict[str, Any],
    record: DemoExecutionRecord,
    *,
    allow_missing: bool = False,
) -> dict[str, Any]:
    """Create or replace one exact ledger value without applying it twice."""

    if record.realized_exchange_pnl is None or record.closed_at is None:
        raise ValueError("final accounting requires realized PnL and close time")
    events = {
        str(key): dict(value)
        for key, value in dict(payload.get("realized_events") or {}).items()
    }
    execution_id = str(record.id)
    if execution_id not in events and not allow_missing:
        raise ValueError(
            "accounting reconciliation cannot create a missing ledger event"
        )
    events[execution_id] = {
        **events.get(execution_id, {}),
        "pnl": str(record.realized_exchange_pnl),
        "closed_at": record.closed_at.isoformat(),
        "fees": str(record.exchange_fees),
        "funding_pnl": str(record.funding_pnl),
        "accounting_status": record.accounting_status,
        "symbol": record.symbol.value,
    }
    now = record.updated_at
    day_start = now.replace(hour=0, minute=0, second=0, microsecond=0)
    week_start = day_start - timedelta(days=day_start.weekday())
    parsed: list[tuple[datetime, Decimal]] = []
    for value in events.values():
        try:
            stamp = datetime.fromisoformat(
                str(value["closed_at"]).replace("Z", "+00:00")
            )
            if stamp.tzinfo is None:
                stamp = stamp.replace(tzinfo=timezone.utc)
            parsed.append((stamp.astimezone(timezone.utc), Decimal(value["pnl"])))
        except (KeyError, ValueError):
            continue
    cumulative = sum((value for _, value in parsed), Decimal("0"))
    daily = sum(
        (value for stamp, value in parsed if stamp >= day_start), Decimal("0")
    )
    weekly = sum(
        (value for stamp, value in parsed if stamp >= week_start), Decimal("0")
    )
    capital = Decimal(str(payload.get("risk_capital_usdt") or "0"))
    unrealized = Decimal(str(payload.get("unrealized_pnl") or "0"))
    equity = capital + cumulative + unrealized
    peak = max(Decimal(str(payload.get("peak_equity") or capital)), equity)
    drawdown = (
        (peak - equity) / peak * Decimal("100")
        if peak > 0 else Decimal("0")
    )
    return {
        **payload,
        "realized_events": events,
        "cumulative_realized_pnl": str(cumulative),
        "daily_pnl": str(daily),
        "weekly_pnl": str(weekly),
        "equity": str(equity),
        "peak_equity": str(peak),
        "current_drawdown_pct": str(drawdown),
    }


def _set_candidate_demo_state(
    candidate: SignalCandidateRow, state: DemoExecutionState
) -> None:
    candidate.execution_environment = ExecutionEnvironment.BYBIT_DEMO.value
    candidate.state = state.value
    payload = dict(candidate.payload)
    payload["execution_environment"] = ExecutionEnvironment.BYBIT_DEMO.value
    payload["state"] = state.value
    candidate.payload = payload


def normalize_url(url: str | None) -> str | None:
    return url.strip().lower().rstrip("/") if url else None


def news_content_hash(item: NewsItem) -> str:
    normalized = f"{item.title.strip().lower()}\n{item.summary.strip().lower()}"
    return sha256(normalized.encode("utf-8")).hexdigest()


def classifier_cache_key(item: NewsItem) -> str:
    return news_content_hash(item)


def _utc_aware(value: datetime) -> datetime:
    return value if value.tzinfo is not None else value.replace(tzinfo=timezone.utc)


def _paper_execution_payload(position: PaperPosition) -> dict[str, Any]:
    return {
        **position.model_dump(mode="json"),
        "quantity": float(position.size),
        "notional": float(position.position_notional),
        "entry_fee": float(position.estimated_entry_fee),
        "exit_fee": float(position.estimated_exit_fee),
        "entry_slippage": float(position.estimated_entry_slippage),
        "exit_slippage": float(position.estimated_exit_slippage),
        "close_reason": position.close_reason,
    }


def _paper_account_payload(account: PaperAccountRow) -> dict[str, float]:
    return {
        "starting_equity": float(account.starting_equity),
        "realized_pnl": float(account.realized_pnl),
        "fees_paid": float(account.fees_paid),
        "equity": float(account.equity),
    }


def _paper_risk_state_payload(row: PaperRiskStateRow) -> dict[str, Any]:
    return {
        "kill_switch_active": bool(row.kill_switch_active),
        "kill_switch_reasons": list(row.kill_switch_reasons),
        "peak_equity": float(row.peak_equity),
        "daily_pnl": float(row.daily_pnl),
        "weekly_pnl": float(row.weekly_pnl),
        "current_drawdown_pct": float(row.current_drawdown_pct),
        "last_entry_at": row.last_entry_at,
        "symbol_cooldowns": dict(row.symbol_cooldowns),
        "updated_at": row.updated_at,
    }


def _database_error_code(exc: object) -> str:
    return f"DB_{type(exc).__name__.upper()}"


def _sanitize_database_error(message: str) -> str:
    sanitized = re.sub(
        r"postgresql(?:\+psycopg)?://[^\s@]+@",
        "postgresql+psycopg://***@",
        message,
        flags=re.IGNORECASE,
    )
    sanitized = re.sub(r"(?i)(password\s*[=:]\s*)[^\s,;]+", r"\1***", sanitized)
    return sanitized[:1000]


def _sqlstate(exc: BaseException) -> str | None:
    current: BaseException | None = exc
    while current is not None:
        value = getattr(current, "sqlstate", None) or getattr(
            current, "pgcode", None
        )
        if value:
            return str(value)[:10]
        current = current.__cause__ or current.__context__
    return None
