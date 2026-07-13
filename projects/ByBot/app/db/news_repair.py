from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
import logging
from typing import Any

from pydantic import ValidationError
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import Asset, NewsItem


LOGGER = logging.getLogger(__name__)


@dataclass
class NewsRepairReport:
    total_rows_scanned: int = 0
    valid_rows: int = 0
    repairable_rows: int = 0
    quarantined_rows: int = 0
    affected_row_ids: list[str] = field(default_factory=list)
    repaired_row_ids: list[str] = field(default_factory=list)
    quarantined_row_ids: list[str] = field(default_factory=list)

    def as_dict(self) -> dict[str, object]:
        return {
            "total_rows_scanned": self.total_rows_scanned,
            "valid_rows": self.valid_rows,
            "repairable_rows": self.repairable_rows,
            "quarantined_rows": self.quarantined_rows,
            "affected_row_ids": list(self.affected_row_ids),
            "repaired_row_ids": list(self.repaired_row_ids),
            "quarantined_row_ids": list(self.quarantined_row_ids),
        }


def sanitized_validation_error(exc: Exception) -> str:
    if isinstance(exc, ValidationError):
        parts = []
        for error in exc.errors(include_url=False, include_context=False, include_input=False):
            location = ".".join(str(item) for item in error.get("loc", ())) or "payload"
            parts.append(f"{location}: {error.get('type', 'validation_error')}")
        return "; ".join(parts)[:1000] or "NewsItem validation failed"
    return type(exc).__name__[:1000]


def reconstruct_news_item(row: Any) -> NewsItem | None:
    required = (row.title, row.summary, row.source, row.published_at)
    if any(value in (None, "") for value in required):
        return None
    try:
        return NewsItem(
            id=row.id,
            title=row.title,
            summary=row.summary,
            source=row.source,
            url=row.normalized_url,
            published_at=_aware(row.published_at),
            received_at=_aware(row.received_at),
            asset_hint=Asset(row.asset_hint or Asset.OTHER.value),
            raw_category=row.raw_category,
            importance=float(row.importance or 0),
        )
    except (ValidationError, ValueError):
        return None


def audit_news_row(
    session: Session,
    row: Any,
    *,
    validation_error: str,
    repair_status: str,
    now: datetime,
) -> None:
    audit_persistence_payload(
        session,
        original_table="news_items",
        original_row_id=str(row.id),
        original_payload=_safe_payload(row.payload),
        validation_error=validation_error,
        repair_status=repair_status,
        now=now,
    )


def audit_persistence_payload(
    session: Session,
    *,
    original_table: str,
    original_row_id: str,
    original_payload: dict[str, Any] | None,
    validation_error: str,
    repair_status: str,
    now: datetime,
) -> None:
    # Imported lazily to avoid a persistence <-> repair import cycle.
    from app.db.persistence import PersistenceQuarantineRow

    audit = session.scalar(select(PersistenceQuarantineRow).where(
        PersistenceQuarantineRow.original_table == original_table,
        PersistenceQuarantineRow.original_row_id == original_row_id,
    ))
    if audit is None:
        audit = PersistenceQuarantineRow(
            original_table=original_table,
            original_row_id=original_row_id,
            original_payload=original_payload,
            validation_error=validation_error,
            repair_status=repair_status,
            quarantined_at=now,
            updated_at=now,
        )
        session.add(audit)
    else:
        if (
            audit.validation_error != validation_error
            or audit.repair_status != repair_status
        ):
            audit.validation_error = validation_error
            audit.repair_status = repair_status
            audit.updated_at = now


def inspect_or_repair_news_rows(session: Session, *, apply: bool) -> NewsRepairReport:
    from app.db.persistence import NewsItemRow

    report = NewsRepairReport()
    rows = session.scalars(select(NewsItemRow).order_by(NewsItemRow.received_at, NewsItemRow.id)).all()
    report.total_rows_scanned = len(rows)
    now = datetime.now(timezone.utc)
    for row in rows:
        try:
            item = NewsItem.model_validate(row.payload)
        except (ValidationError, ValueError, TypeError) as exc:
            error = sanitized_validation_error(exc)
            repaired = reconstruct_news_item(row)
            report.affected_row_ids.append(str(row.id))
            if repaired is not None:
                report.repairable_rows += 1
                report.repaired_row_ids.append(str(row.id))
                if apply:
                    _apply_complete_item(row, repaired)
                    audit_news_row(
                        session, row, validation_error=error,
                        repair_status="REPAIRED", now=now,
                    )
            else:
                report.quarantined_rows += 1
                report.quarantined_row_ids.append(str(row.id))
                if apply:
                    audit_news_row(
                        session, row, validation_error=error,
                        repair_status="QUARANTINED", now=now,
                    )
                    row.is_quarantined = True
            continue
        report.valid_rows += 1
        if apply:
            _apply_complete_item(row, item)
    return report


def _apply_complete_item(row: Any, item: NewsItem) -> None:
    row.payload = item.model_dump(mode="json")
    row.title = item.title
    row.summary = item.summary
    row.source = item.source
    row.published_at = item.published_at
    row.asset_hint = item.asset_hint.value
    row.raw_category = item.raw_category
    row.importance = item.importance
    row.is_quarantined = False


def _safe_payload(payload: object) -> dict[str, Any] | None:
    # The audit table is restricted to normalized news records. Never stringify
    # arbitrary objects or emit the payload to logs.
    if not isinstance(payload, dict):
        return None
    return dict(payload)


def _aware(value: datetime) -> datetime:
    return value if value.tzinfo else value.replace(tzinfo=timezone.utc)
