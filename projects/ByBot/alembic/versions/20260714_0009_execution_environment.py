"""Separate PAPER and BYBIT_DEMO durable records."""

from alembic import op
import sqlalchemy as sa


revision = "20260714_0009"
down_revision = "20260714_0008"
branch_labels = None
depends_on = None


def _add_environment_column(
    table_name: str, default: str, inspector: object
) -> None:
    columns = {column["name"] for column in inspector.get_columns(table_name)}
    if "execution_environment" not in columns:
        op.add_column(
            table_name,
            sa.Column(
                "execution_environment",
                sa.String(20),
                nullable=False,
                server_default=default,
            ),
        )


def upgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    tables = set(inspector.get_table_names())

    if "signal_candidates" in tables:
        _add_environment_column("signal_candidates", "PAPER", inspector)
    inspector = sa.inspect(bind)
    if "paper_executions" in tables:
        _add_environment_column("paper_executions", "PAPER", inspector)
    inspector = sa.inspect(bind)
    if "demo_executions" in tables:
        _add_environment_column("demo_executions", "BYBIT_DEMO", inspector)

    # Existing Demo executions are authoritative evidence that their candidate
    # belongs to the Demo environment. Unexecuted historical candidates remain
    # PAPER, which is the safe backward-compatible classification.
    tables = set(sa.inspect(bind).get_table_names())
    if {"signal_candidates", "demo_executions"}.issubset(tables):
        op.execute(sa.text(
            "UPDATE signal_candidates SET execution_environment = 'BYBIT_DEMO' "
            "WHERE id IN (SELECT candidate_id FROM demo_executions)"
        ))


def downgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    tables = set(inspector.get_table_names())
    for table_name in ("demo_executions", "paper_executions", "signal_candidates"):
        if table_name not in tables:
            continue
        columns = {
            column["name"]
            for column in sa.inspect(bind).get_columns(table_name)
        }
        if "execution_environment" in columns:
            op.drop_column(table_name, "execution_environment")
