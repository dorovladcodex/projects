"""Add durable paper execution idempotency."""
from alembic import op
import sqlalchemy as sa

revision = "20260714_0002"
down_revision = "20260713_0001"
branch_labels = None
depends_on = None


def upgrade() -> None:
    if "paper_executions" in sa.inspect(op.get_bind()).get_table_names():
        return
    op.create_table(
        "paper_executions",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("execution_key", sa.String(100), nullable=False, unique=True),
        sa.Column("candidate_id", sa.String(36), nullable=False, unique=True),
        sa.Column("risk_decision_id", sa.Integer(), nullable=True),
        sa.Column("state", sa.String(30), nullable=False),
        sa.Column("position_id", sa.String(36), nullable=True),
        sa.Column("payload", sa.JSON(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["candidate_id"], ["signal_candidates.id"]),
        sa.ForeignKeyConstraint(["risk_decision_id"], ["risk_decisions.id"]),
    )


def downgrade() -> None:
    if "paper_executions" in sa.inspect(op.get_bind()).get_table_names():
        op.drop_table("paper_executions")
