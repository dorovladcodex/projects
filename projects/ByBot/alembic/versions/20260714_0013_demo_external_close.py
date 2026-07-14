"""Allow unknown Demo PnL for flat-verified executions."""

from alembic import op
import sqlalchemy as sa


revision = "20260714_0013"
down_revision = "20260714_0012"
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table("demo_executions") as batch:
        batch.alter_column(
            "realized_exchange_pnl",
            existing_type=sa.Numeric(36, 18), nullable=True,
        )


def downgrade() -> None:
    op.execute(
        "UPDATE demo_executions SET realized_exchange_pnl = 0 "
        "WHERE realized_exchange_pnl IS NULL"
    )
    with op.batch_alter_table("demo_executions") as batch:
        batch.alter_column(
            "realized_exchange_pnl",
            existing_type=sa.Numeric(36, 18), nullable=False,
        )
