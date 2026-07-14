"""Add durable asynchronous Demo canary jobs."""

from alembic import op
import sqlalchemy as sa


revision = "20260714_0012"
down_revision = "20260714_0011"
branch_labels = None
depends_on = None


def upgrade() -> None:
    if "demo_canary_jobs" in sa.inspect(op.get_bind()).get_table_names():
        return
    op.create_table(
        "demo_canary_jobs",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("run_id", sa.String(64), nullable=False, unique=True),
        sa.Column("symbol", sa.String(20), nullable=False),
        sa.Column("status", sa.String(20), nullable=False),
        sa.Column(
            "execution_id", sa.String(36),
            sa.ForeignKey("demo_executions.id"), nullable=True,
        ),
        sa.Column("request_payload", sa.JSON(), nullable=False),
        sa.Column("result_payload", sa.JSON(), nullable=True),
        sa.Column("error_code", sa.String(80), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
    )


def downgrade() -> None:
    op.drop_table("demo_canary_jobs", if_exists=True)
