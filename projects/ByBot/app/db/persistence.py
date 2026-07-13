from __future__ import annotations

from datetime import datetime, timezone
from hashlib import sha256
from typing import Any

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
    SignalDryRunResult,
)
from app.db.url import normalize_database_url


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
    payload: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False)
    decided_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


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


class PersistenceRepository:
    """Small synchronous repository. Failures degrade persistence, never trading safety."""

    def __init__(self, database_url: str, *, create_schema: bool = True) -> None:
        self.database_url = database_url
        sqlalchemy_url = normalize_database_url(database_url)
        connect_args = {"connect_timeout": 2} if sqlalchemy_url.startswith("postgresql") else {}
        self.available = False
        self.last_error: str | None = None
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
        except IntegrityError:
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

    def save_risk_decision(self, candidate_id: str, decision: RiskDecision) -> None:
        if not self.available:
            return
        try:
            with Session(self.engine) as session:
                session.add(RiskDecisionRow(
                    candidate_id=candidate_id, approved=decision.approved,
                    payload=decision.model_dump(mode="json"), decided_at=decision.decided_at,
                ))
                session.commit()
        except SQLAlchemyError as exc:
            self._failed(exc)

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


def normalize_url(url: str | None) -> str | None:
    return url.strip().lower().rstrip("/") if url else None


def news_content_hash(item: NewsItem) -> str:
    normalized = f"{item.title.strip().lower()}\n{item.summary.strip().lower()}"
    return sha256(normalized.encode("utf-8")).hexdigest()


def classifier_cache_key(item: NewsItem) -> str:
    return news_content_hash(item)
