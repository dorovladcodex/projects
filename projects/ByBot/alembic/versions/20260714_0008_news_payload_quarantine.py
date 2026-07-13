"""Add durable NewsItem repair columns and quarantine audit."""

from datetime import datetime, timezone

from alembic import op
import sqlalchemy as sa


revision = "20260714_0008"
down_revision = "20260714_0007"
branch_labels = None
depends_on = None


DEDICATED_COLUMNS = (
    sa.Column("title", sa.String(300), nullable=True),
    sa.Column("summary", sa.String(1000), nullable=True),
    sa.Column("source", sa.String(100), nullable=True),
    sa.Column("published_at", sa.DateTime(timezone=True), nullable=True),
    sa.Column("asset_hint", sa.String(20), nullable=True),
    sa.Column("raw_category", sa.String(100), nullable=True),
    sa.Column("importance", sa.Float(), nullable=True),
    sa.Column("is_quarantined", sa.Boolean(), nullable=False, server_default=sa.false()),
)


def upgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    existing = {column["name"] for column in inspector.get_columns("news_items")}
    for column in DEDICATED_COLUMNS:
        if column.name not in existing:
            op.add_column("news_items", column)

    inspector = sa.inspect(bind)
    if "persistence_quarantine" not in inspector.get_table_names():
        op.create_table(
            "persistence_quarantine",
            sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
            sa.Column("original_table", sa.String(100), nullable=False),
            sa.Column("original_row_id", sa.String(100), nullable=False),
            sa.Column("original_payload", sa.JSON(), nullable=True),
            sa.Column("validation_error", sa.String(1000), nullable=False),
            sa.Column("repair_status", sa.String(30), nullable=False),
            sa.Column("quarantined_at", sa.DateTime(timezone=True), nullable=False),
            sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
            sa.UniqueConstraint(
                "original_table", "original_row_id",
                name="uq_persistence_quarantine_origin",
            ),
        )

    indexes = {item["name"] for item in sa.inspect(bind).get_indexes("news_items")}
    if "ix_news_items_quarantined" not in indexes:
        op.create_index(
            "ix_news_items_quarantined", "news_items", ["is_quarantined"]
        )

    _backfill_and_quarantine(bind)


def _backfill_and_quarantine(bind) -> None:
    quarantine_table = sa.Table(
        "persistence_quarantine", sa.MetaData(), autoload_with=bind
    )
    rows = bind.execute(sa.text(
        "SELECT id, payload FROM news_items"
    )).mappings().all()
    now = datetime.now(timezone.utc)
    for row in rows:
        payload = row["payload"] if isinstance(row["payload"], dict) else {}
        required = ("title", "summary", "source", "published_at")
        complete = all(payload.get(name) not in (None, "") for name in required)
        if complete:
            published_at = payload.get("published_at")
            if isinstance(published_at, str):
                try:
                    published_at = datetime.fromisoformat(
                        published_at.replace("Z", "+00:00")
                    )
                except ValueError:
                    published_at = None
            bind.execute(sa.text(
                "UPDATE news_items SET title=:title, summary=:summary, source=:source, "
                "published_at=:published_at, asset_hint=:asset_hint, "
                "raw_category=:raw_category, importance=:importance, "
                "is_quarantined=false WHERE id=:id"
            ), {
                "id": row["id"], "title": payload.get("title"),
                "summary": payload.get("summary"), "source": payload.get("source"),
                "published_at": published_at, "asset_hint": payload.get("asset_hint"),
                "raw_category": payload.get("raw_category"),
                "importance": payload.get("importance", 0),
            })
            continue

        bind.execute(sa.text(
            "UPDATE news_items SET is_quarantined=true WHERE id=:id"
        ), {"id": row["id"]})
        exists = bind.execute(sa.text(
            "SELECT 1 FROM persistence_quarantine "
            "WHERE original_table='news_items' AND original_row_id=:id"
        ), {"id": str(row["id"])}).first()
        if not exists:
            bind.execute(quarantine_table.insert().values(
                original_table="news_items",
                original_row_id=str(row["id"]),
                original_payload=row["payload"] if isinstance(row["payload"], dict) else None,
                validation_error="missing required NewsItem payload fields",
                repair_status="QUARANTINED",
                quarantined_at=now,
                updated_at=now,
            ))


def downgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    indexes = {item["name"] for item in inspector.get_indexes("news_items")}
    if "ix_news_items_quarantined" in indexes:
        op.drop_index("ix_news_items_quarantined", table_name="news_items")
    if "persistence_quarantine" in inspector.get_table_names():
        op.drop_table("persistence_quarantine")
    existing = {column["name"] for column in sa.inspect(bind).get_columns("news_items")}
    for name in reversed([column.name for column in DEDICATED_COLUMNS]):
        if name in existing:
            op.drop_column("news_items", name)
