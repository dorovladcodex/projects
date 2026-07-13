from __future__ import annotations

from datetime import datetime, timezone
import os
from pathlib import Path
import subprocess
import sys
import sqlite3
import json
from uuid import uuid4

import pytest
from sqlalchemy import insert, select
from sqlalchemy.orm import Session

from app.db.persistence import (
    NewsItemRow,
    PersistenceQuarantineRow,
    PersistenceRepository,
    SignalCandidateRow,
)
from app.models import Asset, NewsItem


def news_item(title: str = "Valid Bitcoin ETF news") -> NewsItem:
    return NewsItem(
        title=title,
        summary="SEC ETF approval supports institutional Bitcoin adoption.",
        source="restore-test",
        url=f"https://example.invalid/{uuid4()}",
        published_at=datetime.now(timezone.utc),
        asset_hint=Asset.BTC,
        importance=0.9,
    )


def insert_historical_row(
    repository: PersistenceRepository,
    *,
    repairable: bool,
) -> str:
    row_id = str(uuid4())
    now = datetime.now(timezone.utc)
    values = {
        "id": row_id,
        "normalized_url": f"https://example.invalid/historical/{row_id}",
        "content_hash": uuid4().hex + uuid4().hex,
        "title": "Repairable Bitcoin news" if repairable else None,
        "summary": "SEC approval is material for BTC." if repairable else None,
        "source": "historical-source" if repairable else None,
        "published_at": now if repairable else None,
        "asset_hint": "BTC" if repairable else None,
        "raw_category": "etf" if repairable else None,
        "importance": 0.9 if repairable else None,
        "is_quarantined": False,
        "payload": {"id": row_id},
        "received_at": now,
    }
    # Core insert intentionally simulates pre-validation historical data.
    with repository.engine.begin() as connection:
        connection.execute(insert(NewsItemRow.__table__).values(**values))
    return row_id


def test_valid_news_restores_normally(tmp_path: Path) -> None:
    repository = PersistenceRepository(f"sqlite:///{tmp_path / 'valid.db'}")
    item = news_item()
    assert repository.save_news(item)
    items, _ = repository.load_news()
    assert [entry.id for entry in items] == [item.id]
    assert repository.news_restore_valid_count == 1
    assert repository.news_restore_quarantined_count == 0


def test_malformed_row_is_quarantined_without_hiding_valid_neighbors(tmp_path: Path) -> None:
    repository = PersistenceRepository(f"sqlite:///{tmp_path / 'mixed.db'}")
    before, after = news_item("Valid before"), news_item("Valid after")
    repository.save_news(before)
    malformed_id = insert_historical_row(repository, repairable=False)
    repository.save_news(after)

    items, _ = repository.load_news()
    assert {item.id for item in items} == {before.id, after.id}
    assert repository.news_restore_valid_count == 2
    assert repository.news_restore_quarantined_count == 1
    assert repository.news_restore_last_error
    with Session(repository.engine) as session:
        row = session.get(NewsItemRow, malformed_id)
        audit = session.scalar(select(PersistenceQuarantineRow).where(
            PersistenceQuarantineRow.original_row_id == malformed_id
        ))
        assert row.is_quarantined is True
        assert audit is not None
        assert audit.original_table == "news_items"
        assert audit.original_payload == {"id": malformed_id}
        assert audit.repair_status == "QUARANTINED"
        assert "title" in audit.validation_error


def test_repairable_row_is_reconstructed_from_dedicated_columns(tmp_path: Path) -> None:
    repository = PersistenceRepository(f"sqlite:///{tmp_path / 'repair.db'}")
    repaired_id = insert_historical_row(repository, repairable=True)
    items, _ = repository.load_news()
    repaired = next(item for item in items if str(item.id) == repaired_id)
    assert repaired.title == "Repairable Bitcoin news"
    assert repaired.asset_hint == Asset.BTC
    assert repository.news_restore_repaired_count == 1
    with Session(repository.engine) as session:
        row = session.get(NewsItemRow, repaired_id)
        audit = session.scalar(select(PersistenceQuarantineRow).where(
            PersistenceQuarantineRow.original_row_id == repaired_id
        ))
        assert row.is_quarantined is False
        assert NewsItem.model_validate(row.payload).title == repaired.title
        assert audit.repair_status == "REPAIRED"


def test_repeated_startup_does_not_duplicate_quarantine(tmp_path: Path) -> None:
    repository = PersistenceRepository(f"sqlite:///{tmp_path / 'repeat.db'}")
    malformed_id = insert_historical_row(repository, repairable=False)
    repository.load_news()
    restarted = PersistenceRepository(repository.database_url)
    restarted.load_news()
    with Session(restarted.engine) as session:
        audits = session.scalars(select(PersistenceQuarantineRow).where(
            PersistenceQuarantineRow.original_row_id == malformed_id
        )).all()
        assert len(audits) == 1
    assert restarted.news_restore_quarantined_count == 1


def test_repair_script_apply_is_idempotent(tmp_path: Path) -> None:
    database = tmp_path / "script.db"
    repository = PersistenceRepository(f"sqlite:///{database}")
    malformed_id = insert_historical_row(repository, repairable=False)
    env = {**os.environ, "DATABASE_URL": f"sqlite:///{database}"}
    script = Path(__file__).parents[1] / "scripts" / "repair_news_payloads.py"
    dry = subprocess.run(
        [sys.executable, str(script), "--dry-run"], env=env,
        text=True, capture_output=True, timeout=30,
    )
    assert dry.returncode == 0, dry.stderr
    assert "quarantined rows: 1" in dry.stdout
    for _ in range(2):
        applied = subprocess.run(
            [sys.executable, str(script), "--apply"], env=env,
            text=True, capture_output=True, timeout=30,
        )
        assert applied.returncode == 0, applied.stderr
    with Session(repository.engine) as session:
        audits = session.scalars(select(PersistenceQuarantineRow).where(
            PersistenceQuarantineRow.original_row_id == malformed_id
        )).all()
        assert len(audits) == 1
        assert session.get(NewsItemRow, malformed_id).is_quarantined is True


def test_complete_news_persistence_is_enforced(tmp_path: Path) -> None:
    repository = PersistenceRepository(f"sqlite:///{tmp_path / 'enforce.db'}")
    item = news_item()
    assert repository.save_news(item)
    with Session(repository.engine) as session:
        payload = session.get(NewsItemRow, str(item.id)).payload
        assert {"id", "title", "summary", "source", "published_at"} <= payload.keys()

    incomplete = NewsItem.model_construct(id=uuid4())
    with pytest.raises(Exception):
        repository.save_news(incomplete)


def test_migration_quarantines_preexisting_id_only_payload(tmp_path: Path) -> None:
    database = tmp_path / "migration.db"
    env = {**os.environ, "DATABASE_URL": f"sqlite:///{database}"}
    root = Path(__file__).parents[1]
    first = subprocess.run(
        [sys.executable, "-m", "alembic", "upgrade", "20260714_0007"],
        cwd=root, env=env, text=True, capture_output=True, timeout=60,
    )
    assert first.returncode == 0, first.stderr
    row_id = str(uuid4())
    now = datetime.now(timezone.utc).isoformat()
    with sqlite3.connect(database) as connection:
        connection.execute(
            "INSERT INTO news_items "
            "(id, normalized_url, content_hash, payload, received_at, is_quarantined) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (row_id, None, uuid4().hex + uuid4().hex, json.dumps({"id": row_id}), now, 0),
        )
        connection.commit()
    head = subprocess.run(
        [sys.executable, "-m", "alembic", "upgrade", "head"],
        cwd=root, env=env, text=True, capture_output=True, timeout=60,
    )
    assert head.returncode == 0, head.stderr
    repository = PersistenceRepository(f"sqlite:///{database}")
    items, _ = repository.load_news()
    assert items == []
    assert repository.news_restore_quarantined_count == 1


def test_malformed_candidate_does_not_break_startup_restore(tmp_path: Path) -> None:
    repository = PersistenceRepository(f"sqlite:///{tmp_path / 'candidate.db'}")
    item = news_item()
    repository.save_news(item)
    candidate_id = str(uuid4())
    now = datetime.now(timezone.utc)
    with repository.engine.begin() as connection:
        connection.execute(insert(SignalCandidateRow.__table__).values(
            id=candidate_id, news_id=str(item.id), symbol="BTCUSDT",
            state="PAPER_CLOSED", active=False, expires_at=now,
            payload={"state": "PAPER_CLOSED"}, risk_preview={},
            risk_decision_id=None,
        ))
    assert repository.load_signal_results() == []
    with Session(repository.engine) as session:
        audit = session.scalar(select(PersistenceQuarantineRow).where(
            PersistenceQuarantineRow.original_table == "signal_candidates",
            PersistenceQuarantineRow.original_row_id == candidate_id,
        ))
        assert audit is not None
        assert audit.repair_status == "QUARANTINED"
