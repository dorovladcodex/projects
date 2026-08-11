"""Add the V4 shadow opportunity tape and forward labels."""

from alembic import op
import sqlalchemy as sa


revision = "20260811_0015"
down_revision = "20260715_0014"
branch_labels = None
depends_on = None


def upgrade() -> None:
    existing = set(sa.inspect(op.get_bind()).get_table_names())
    if "v4_opportunities" not in existing:
        op.create_table(
            "v4_opportunities",
            sa.Column("opportunity_id", sa.String(36), primary_key=True),
            sa.Column("cycle_id", sa.String(160), nullable=False),
            sa.Column("run_id", sa.String(64), nullable=False),
            sa.Column("symbol", sa.String(20), nullable=False),
            sa.Column("side", sa.String(8), nullable=False),
            sa.Column("source", sa.String(80), nullable=False),
            sa.Column("candidate_type", sa.String(80), nullable=False),
            sa.Column("decision", sa.String(30), nullable=False),
            sa.Column("rejected", sa.Boolean(), nullable=False),
            sa.Column("feature_snapshot_at", sa.DateTime(timezone=True), nullable=False),
            sa.Column("decision_at", sa.DateTime(timezone=True), nullable=False),
            sa.Column("payload", sa.JSON(), nullable=False),
            sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
            sa.UniqueConstraint(
                "run_id", "cycle_id", "symbol", "candidate_type",
                name="uq_v4_opportunity_cycle_symbol_type",
            ),
        )
        op.create_index(
            "ix_v4_opportunity_run_decision", "v4_opportunities",
            ["run_id", "decision_at"],
        )
    if "v4_forward_labels" not in existing:
        op.create_table(
            "v4_forward_labels",
            sa.Column(
                "opportunity_id", sa.String(36),
                sa.ForeignKey("v4_opportunities.opportunity_id"), primary_key=True,
            ),
            sa.Column("symbol", sa.String(20), nullable=False),
            sa.Column("decision_at", sa.DateTime(timezone=True), nullable=False),
            sa.Column("maximum_horizon_seconds", sa.Integer(), nullable=False),
            sa.Column("complete", sa.Boolean(), nullable=False),
            sa.Column("payload", sa.JSON(), nullable=False),
            sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        )
        op.create_index("ix_v4_label_decision", "v4_forward_labels", ["decision_at"])


def downgrade() -> None:
    op.drop_table("v4_forward_labels")
    op.drop_table("v4_opportunities")
