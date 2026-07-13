"""Align paper risk decision persistence fields."""
from alembic import op
import sqlalchemy as sa

revision = "20260714_0004"
down_revision = "20260714_0003"
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    risk_columns = {column["name"] for column in inspector.get_columns("risk_decisions")}
    if "created_at" not in risk_columns and "decided_at" in risk_columns:
        op.alter_column("risk_decisions", "decided_at", new_column_name="created_at")
    additions = {
        "capped_size": sa.Column("capped_size", sa.Float(), nullable=False, server_default="0"),
        "position_notional": sa.Column("position_notional", sa.Float(), nullable=False, server_default="0"),
        "max_allowed_notional": sa.Column("max_allowed_notional", sa.Float(), nullable=False, server_default="0"),
        "estimated_fees": sa.Column("estimated_fees", sa.Float(), nullable=False, server_default="0"),
        "estimated_slippage": sa.Column("estimated_slippage", sa.Float(), nullable=False, server_default="0"),
        "rejection_reasons": sa.Column("rejection_reasons", sa.JSON(), nullable=False, server_default="[]"),
    }
    for name, column in additions.items():
        if name not in risk_columns:
            op.add_column("risk_decisions", column)

def downgrade() -> None:
    # Existing audit fields are intentionally retained.
    pass
