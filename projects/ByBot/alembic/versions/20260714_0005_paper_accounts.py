"""Add durable singleton paper account totals."""

from alembic import op
import sqlalchemy as sa


revision = "20260714_0005"
down_revision = "20260714_0004"
branch_labels = None
depends_on = None


def upgrade() -> None:
    if "paper_accounts" in sa.inspect(op.get_bind()).get_table_names():
        return
    op.create_table(
        "paper_accounts",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("starting_equity", sa.Float(), nullable=False),
        sa.Column("realized_pnl", sa.Float(), nullable=False, server_default="0"),
        sa.Column("fees_paid", sa.Float(), nullable=False, server_default="0"),
        sa.Column("equity", sa.Float(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
    )


def downgrade() -> None:
    if "paper_accounts" in sa.inspect(op.get_bind()).get_table_names():
        op.drop_table("paper_accounts")
