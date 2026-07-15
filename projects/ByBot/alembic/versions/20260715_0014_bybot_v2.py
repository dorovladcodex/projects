"""ByBot V2 universe, strategies, portfolio reservations and run analytics."""

from alembic import op
import sqlalchemy as sa


revision = "20260715_0014"
down_revision = "20260714_0013"
branch_labels = None
depends_on = None


def upgrade() -> None:
    existing = set(sa.inspect(op.get_bind()).get_table_names())
    created: set[str] = set()

    def create_table(name: str, *columns: sa.Column, **kwargs: object) -> None:
        # Migration 0001 historically calls current Base.metadata.create_all(),
        # so a fresh test database can already contain future tables.
        if name in existing:
            return
        op.create_table(name, *columns, **kwargs)
        existing.add(name)
        created.add(name)

    create_table(
        "v2_symbol_universe",
        sa.Column("symbol", sa.String(20), primary_key=True),
        sa.Column("state", sa.String(30), nullable=False),
        sa.Column("accepted", sa.Boolean(), nullable=False),
        sa.Column("rejection_reasons", sa.JSON(), nullable=False),
        sa.Column("payload", sa.JSON(), nullable=False),
        sa.Column("checked_at", sa.DateTime(timezone=True), nullable=False),
    )
    create_table(
        "v2_market_feature_snapshots",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("symbol", sa.String(20), nullable=False),
        sa.Column("captured_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("fresh", sa.Boolean(), nullable=False),
        sa.Column("payload", sa.JSON(), nullable=False),
        sa.UniqueConstraint("symbol", "captured_at", name="uq_v2_feature_symbol_time"),
    )
    create_table(
        "v2_signal_candidates",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("run_id", sa.String(64), nullable=False),
        sa.Column("strategy_name", sa.String(80), nullable=False),
        sa.Column("strategy_version", sa.String(30), nullable=False),
        sa.Column("symbol", sa.String(20), nullable=False),
        sa.Column("side", sa.String(8), nullable=False),
        sa.Column("state", sa.String(40), nullable=False),
        sa.Column("admitted", sa.Boolean(), nullable=False),
        sa.Column("final_score", sa.Numeric(18, 8), nullable=True),
        sa.Column("rejection_reason", sa.String(1000), nullable=True),
        sa.Column("payload", sa.JSON(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint(
            "run_id", "strategy_name", "symbol", "created_at",
            name="uq_v2_candidate_generation",
        ),
    )
    if "v2_signal_candidates" in created:
        op.create_index(
            "ix_v2_candidate_run_created", "v2_signal_candidates", ["run_id", "created_at"]
        )
    create_table(
        "v2_portfolio_reservations",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("run_id", sa.String(64), nullable=False),
        sa.Column("candidate_id", sa.String(36), sa.ForeignKey("v2_signal_candidates.id"), nullable=False, unique=True),
        sa.Column("execution_id", sa.String(36), nullable=True),
        sa.Column("symbol", sa.String(20), nullable=False),
        sa.Column("active_symbol", sa.String(20), nullable=True, unique=True),
        sa.Column("correlation_group", sa.String(40), nullable=False),
        sa.Column("strategy_name", sa.String(80), nullable=False),
        sa.Column("state", sa.String(30), nullable=False),
        sa.Column("notional_usdt", sa.Numeric(36, 18), nullable=False),
        sa.Column("risk_usdt", sa.Numeric(36, 18), nullable=False),
        sa.Column("payload", sa.JSON(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("released_at", sa.DateTime(timezone=True), nullable=True),
    )
    create_table(
        "v2_portfolio_state",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("payload", sa.JSON(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
    )
    create_table(
        "v2_signal_rejections",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("run_id", sa.String(64), nullable=False),
        sa.Column("candidate_id", sa.String(36), nullable=True),
        sa.Column("strategy_name", sa.String(80), nullable=False),
        sa.Column("symbol", sa.String(20), nullable=False),
        sa.Column("reason", sa.String(1000), nullable=False),
        sa.Column("payload", sa.JSON(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    )
    create_table(
        "v2_incidents",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("run_id", sa.String(64), nullable=False),
        sa.Column("event_type", sa.String(80), nullable=False),
        sa.Column("symbol", sa.String(20), nullable=True),
        sa.Column("execution_id", sa.String(36), nullable=True),
        sa.Column("candidate_id", sa.String(36), nullable=True),
        sa.Column("payload", sa.JSON(), nullable=False),
        sa.Column("occurred_at", sa.DateTime(timezone=True), nullable=False),
    )
    create_table(
        "v2_runs",
        sa.Column("run_id", sa.String(64), primary_key=True),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("status", sa.String(30), nullable=False),
        sa.Column("payload", sa.JSON(), nullable=False),
    )


def downgrade() -> None:
    op.drop_table("v2_runs")
    op.drop_table("v2_incidents")
    op.drop_table("v2_signal_rejections")
    op.drop_table("v2_portfolio_state")
    op.drop_table("v2_portfolio_reservations")
    op.drop_index("ix_v2_candidate_run_created", table_name="v2_signal_candidates")
    op.drop_table("v2_signal_candidates")
    op.drop_table("v2_market_feature_snapshots")
    op.drop_table("v2_symbol_universe")
