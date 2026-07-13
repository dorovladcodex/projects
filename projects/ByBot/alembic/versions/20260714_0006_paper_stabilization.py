"""Persist paper stabilization risk state and open-position slots."""

import json

from alembic import op
import sqlalchemy as sa


revision = "20260714_0006"
down_revision = "20260714_0005"
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    columns = {
        column["name"] for column in inspector.get_columns("paper_positions")
    }
    if "symbol" not in columns:
        op.add_column("paper_positions", sa.Column("symbol", sa.String(20)))
    if "candidate_id" not in columns:
        op.add_column("paper_positions", sa.Column("candidate_id", sa.String(36)))
    if "open_slot" not in columns:
        op.add_column("paper_positions", sa.Column("open_slot", sa.String(20)))

    rows = bind.execute(sa.text(
        "SELECT id, status, payload FROM paper_positions"
    )).mappings().all()
    for row in rows:
        payload = row["payload"]
        if isinstance(payload, str):
            payload = json.loads(payload)
        symbol = payload.get("symbol") if isinstance(payload, dict) else None
        candidate_id = payload.get("candidate_id") if isinstance(payload, dict) else None
        open_slot = symbol if row["status"] == "OPEN" else None
        bind.execute(
            sa.text(
                "UPDATE paper_positions SET symbol=:symbol, candidate_id=:candidate_id, "
                "open_slot=:open_slot WHERE id=:id"
            ),
            {
                "id": row["id"],
                "symbol": symbol,
                "candidate_id": candidate_id,
                "open_slot": open_slot,
            },
        )

    inspector = sa.inspect(bind)
    indexes = {item["name"] for item in inspector.get_indexes("paper_positions")}
    if "uq_paper_positions_open_slot" not in indexes:
        op.create_index(
            "uq_paper_positions_open_slot",
            "paper_positions",
            ["open_slot"],
            unique=True,
        )

    if "paper_risk_state" not in inspector.get_table_names():
        op.create_table(
            "paper_risk_state",
            sa.Column("id", sa.Integer(), primary_key=True),
            sa.Column("kill_switch_active", sa.Boolean(), nullable=False),
            sa.Column("kill_switch_reasons", sa.JSON(), nullable=False),
            sa.Column("peak_equity", sa.Float(), nullable=False),
            sa.Column("daily_pnl", sa.Float(), nullable=False, server_default="0"),
            sa.Column("weekly_pnl", sa.Float(), nullable=False, server_default="0"),
            sa.Column(
                "current_drawdown_pct", sa.Float(), nullable=False, server_default="0"
            ),
            sa.Column("last_entry_at", sa.DateTime(timezone=True), nullable=True),
            sa.Column("symbol_cooldowns", sa.JSON(), nullable=False),
            sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        )


def downgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    if "paper_risk_state" in inspector.get_table_names():
        op.drop_table("paper_risk_state")
    indexes = {item["name"] for item in inspector.get_indexes("paper_positions")}
    if "uq_paper_positions_open_slot" in indexes:
        op.drop_index("uq_paper_positions_open_slot", table_name="paper_positions")
    columns = {
        column["name"] for column in sa.inspect(bind).get_columns("paper_positions")
    }
    for name in ("open_slot", "candidate_id", "symbol"):
        if name in columns:
            op.drop_column("paper_positions", name)
