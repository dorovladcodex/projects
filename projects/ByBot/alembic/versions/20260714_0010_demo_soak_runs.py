"""Add durable, run-scoped Bybit Demo soak reporting."""

from alembic import op
import sqlalchemy as sa


revision = "20260714_0010"
down_revision = "20260714_0009"
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    tables = set(inspector.get_table_names())
    if "signal_candidates" in tables:
        columns = {column["name"] for column in inspector.get_columns("signal_candidates")}
        if "run_id" not in columns:
            op.add_column("signal_candidates", sa.Column("run_id", sa.String(64), nullable=True))
        if "created_at" not in columns:
            op.add_column(
                "signal_candidates",
                sa.Column(
                    "created_at", sa.DateTime(timezone=True), nullable=False,
                    server_default=sa.func.now(),
                ),
            )
        op.create_index(
            "ix_signal_candidates_demo_run",
            "signal_candidates",
            ["execution_environment", "run_id", "created_at"],
            unique=False,
            if_not_exists=True,
        )
    inspector = sa.inspect(bind)
    if "demo_soak_runs" not in set(inspector.get_table_names()):
        op.create_table(
            "demo_soak_runs",
            sa.Column("run_id", sa.String(64), primary_key=True),
            sa.Column("started_at", sa.DateTime(timezone=True), nullable=False),
            sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True),
            sa.Column("status", sa.String(20), nullable=False),
            sa.Column("opening_snapshot", sa.JSON(), nullable=False),
            sa.Column("final_snapshot", sa.JSON(), nullable=True),
        )


def downgrade() -> None:
    bind = op.get_bind()
    tables = set(sa.inspect(bind).get_table_names())
    if "demo_soak_runs" in tables:
        op.drop_table("demo_soak_runs")
    if "signal_candidates" in tables:
        indexes = {index["name"] for index in sa.inspect(bind).get_indexes("signal_candidates")}
        if "ix_signal_candidates_demo_run" in indexes:
            op.drop_index("ix_signal_candidates_demo_run", table_name="signal_candidates")
        columns = {column["name"] for column in sa.inspect(bind).get_columns("signal_candidates")}
        if "created_at" in columns:
            op.drop_column("signal_candidates", "created_at")
        if "run_id" in columns:
            op.drop_column("signal_candidates", "run_id")
