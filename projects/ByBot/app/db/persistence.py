from __future__ import annotations

from datetime import datetime, timedelta, timezone
from hashlib import sha256
import logging
import re
from typing import Any
from uuid import uuid4

from sqlalchemy import JSON, Boolean, DateTime, Float, ForeignKey, String, UniqueConstraint, create_engine, select
from sqlalchemy.exc import IntegrityError, SQLAlchemyError
from sqlalchemy.orm import DeclarativeBase, Mapped, Session, mapped_column

from app.models import (
    CandidateLifecycleState,
    ClassificationStatus,
    NewsClassification,
    NewsItem,
    PaperPosition,
    RiskDecision,
    SignalRiskPreview,
    SignalDryRunResult,
)
from app.db.url import normalize_database_url

LOGGER = logging.getLogger(__name__)


class Base(DeclarativeBase):
    pass


class NewsItemRow(Base):
    __tablename__ = "news_items"
    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    normalized_url: Mapped[str | None] = mapped_column(String(1000), unique=True, nullable=True)
    content_hash: Mapped[str] = mapped_column(String(64), unique=True, nullable=False)
    payload: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False)
    received_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


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
    symbol: Mapped[str] = mapped_column(String(20), nullable=False)
    state: Mapped[str] = mapped_column(String(30), nullable=False)
    active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    payload: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False)
    risk_preview: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False)
    risk_decision_id: Mapped[int | None] = mapped_column(nullable=True)
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
    payload: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False)


class PaperTradeRow(Base):
    __tablename__ = "paper_trades"
    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    realized_pnl: Mapped[float] = mapped_column(Float, nullable=False, default=0)
    payload: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False)


class PaperExecutionRow(Base):
    __tablename__ = "paper_executions"
    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    execution_key: Mapped[str] = mapped_column(String(100), unique=True, nullable=False)
    candidate_id: Mapped[str] = mapped_column(
        ForeignKey("signal_candidates.id"), unique=True, nullable=False
    )
    risk_decision_id: Mapped[int | None] = mapped_column(
        ForeignKey("risk_decisions.id"), nullable=True
    )
    state: Mapped[str] = mapped_column(String(30), nullable=False)
    position_id: Mapped[str | None] = mapped_column(String(36), nullable=True)
    payload: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class PersistenceRepository:
    """Small synchronous repository. Failures degrade persistence, never trading safety."""

    def __init__(self, database_url: str, *, create_schema: bool = True) -> None:
        self.database_url = database_url
        sqlalchemy_url = normalize_database_url(database_url)
        connect_args = {"connect_timeout": 2} if sqlalchemy_url.startswith("postgresql") else {}
        self.available = False
        self.last_error: str | None = None
        self.last_error_code: str | None = None
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

    def save_news(self, item: NewsItem) -> bool:
        if not self.available:
            return False
        row = NewsItemRow(
            id=str(item.id), normalized_url=normalize_url(item.url),
            content_hash=news_content_hash(item), payload=item.model_dump(mode="json"),
            received_at=item.received_at,
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
                    news_id=str(candidate.news_id), symbol=candidate.symbol.value if candidate.symbol else "NONE",
                    state=candidate.state.value, active=active, expires_at=candidate.expires_at,
                    payload=candidate.model_dump(mode="json"), risk_preview=result.risk_preview.model_dump(mode="json"),
                    risk_decision_id=result.risk_preview.risk_decision_id,
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
        except IntegrityError:
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
            with Session(self.engine) as session:
                news = [NewsItem.model_validate(row.payload) for row in session.scalars(select(NewsItemRow)).all()]
                classifications = [NewsClassification.model_validate(row.payload) for row in session.scalars(select(NewsClassificationRow)).all()]
            return news, classifications
        except SQLAlchemyError as exc:
            self._failed(exc)
            return [], []

    def load_signal_results(self) -> list[SignalDryRunResult]:
        if not self.available:
            return []
        try:
            with Session(self.engine) as session:
                return [SignalDryRunResult.model_validate({"candidate": row.payload, "risk_preview": row.risk_preview}) for row in session.scalars(select(SignalCandidateRow)).all()]
        except SQLAlchemyError as exc:
            self._failed(exc)
            return []

    def save_paper_position(self, position: PaperPosition) -> None:
        if not self.available:
            return
        try:
            with Session(self.engine) as session:
                payload = position.model_dump(mode="json")
                session.merge(PaperPositionRow(id=str(position.id), status=position.status.value, payload=payload))
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

    def _failed(self, exc: SQLAlchemyError) -> None:
        self.available = False
        self.last_error = type(exc).__name__
        self.last_error_code = _database_error_code(exc)
        LOGGER.error(
            "database operation failed: type=%s message=%s",
            type(exc).__name__,
            _sanitize_database_error(str(exc)),
        )


def normalize_url(url: str | None) -> str | None:
    return url.strip().lower().rstrip("/") if url else None


def news_content_hash(item: NewsItem) -> str:
    normalized = f"{item.title.strip().lower()}\n{item.summary.strip().lower()}"
    return sha256(normalized.encode("utf-8")).hexdigest()


def classifier_cache_key(item: NewsItem) -> str:
    return news_content_hash(item)


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
