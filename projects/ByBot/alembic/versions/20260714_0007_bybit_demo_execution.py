"""Persist fail-closed Bybit Demo execution lifecycle."""

from alembic import op
import sqlalchemy as sa


revision = "20260714_0007"
down_revision = "20260714_0006"
branch_labels = None
depends_on = None


def upgrade() -> None:
    inspector = sa.inspect(op.get_bind())
    tables = set(inspector.get_table_names())
    if "demo_executions" not in tables:
        op.create_table(
            "demo_executions",
            sa.Column("id", sa.String(36), primary_key=True),
            sa.Column("candidate_id", sa.String(36), nullable=False),
            sa.Column("risk_decision_id", sa.Integer(), nullable=True),
            sa.Column("run_id", sa.String(64), nullable=False),
            sa.Column("order_link_id", sa.String(36), nullable=False),
            sa.Column("order_id", sa.String(64), nullable=True),
            sa.Column("symbol", sa.String(20), nullable=False),
            sa.Column("side", sa.String(8), nullable=False),
            sa.Column("state", sa.String(40), nullable=False),
            sa.Column("requested_quantity", sa.Numeric(36, 18), nullable=False),
            sa.Column("accepted_quantity", sa.Numeric(36, 18), nullable=False),
            sa.Column("average_fill_price", sa.Numeric(36, 18), nullable=True),
            sa.Column("close_order_link_id", sa.String(36), nullable=True),
            sa.Column("close_order_id", sa.String(64), nullable=True),
            sa.Column("realized_exchange_pnl", sa.Numeric(36, 18), nullable=False),
            sa.Column("payload", sa.JSON(), nullable=False),
            sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
            sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
            sa.ForeignKeyConstraint(["candidate_id"], ["signal_candidates.id"]),
            sa.ForeignKeyConstraint(["risk_decision_id"], ["risk_decisions.id"]),
            sa.UniqueConstraint("candidate_id", name="uq_demo_execution_candidate"),
            sa.UniqueConstraint("order_link_id", name="uq_demo_execution_order_link"),
            sa.UniqueConstraint("close_order_link_id", name="uq_demo_execution_close_order_link"),
        )
    inspector = sa.inspect(op.get_bind())
    tables = set(inspector.get_table_names())
    if "demo_execution_events" not in tables:
        op.create_table(
            "demo_execution_events",
            sa.Column("id", sa.String(36), primary_key=True),
            sa.Column("execution_id", sa.String(36), nullable=True),
            sa.Column("event_key", sa.String(200), nullable=False),
            sa.Column("event_type", sa.String(80), nullable=False),
            sa.Column("payload", sa.JSON(), nullable=False),
            sa.Column("occurred_at", sa.DateTime(timezone=True), nullable=False),
            sa.ForeignKeyConstraint(["execution_id"], ["demo_executions.id"]),
            sa.UniqueConstraint("event_key", name="uq_demo_execution_event_key"),
        )
    if "demo_kill_switch" not in tables:
        op.create_table(
            "demo_kill_switch",
            sa.Column("id", sa.Integer(), primary_key=True),
            sa.Column("active", sa.Boolean(), nullable=False),
            sa.Column("reasons", sa.JSON(), nullable=False),
            sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        )


def downgrade() -> None:
    inspector = sa.inspect(op.get_bind())
    tables = set(inspector.get_table_names())
    for table in ("demo_execution_events", "demo_kill_switch", "demo_executions"):
        if table in tables:
            op.drop_table(table)
