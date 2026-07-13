"""Link signal candidates to durable risk decisions."""
from alembic import op
import sqlalchemy as sa

revision = "20260714_0003"
down_revision = "20260714_0002"
branch_labels = None
depends_on = None


def upgrade() -> None:
    columns = {column["name"] for column in sa.inspect(op.get_bind()).get_columns("signal_candidates")}
    if "risk_decision_id" not in columns:
        op.add_column(
            "signal_candidates",
            sa.Column("risk_decision_id", sa.Integer(), nullable=True),
        )


def downgrade() -> None:
    columns = {column["name"] for column in sa.inspect(op.get_bind()).get_columns("signal_candidates")}
    if "risk_decision_id" in columns:
        op.drop_column("signal_candidates", "risk_decision_id")
