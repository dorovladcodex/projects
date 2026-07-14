"""Add durable Bybit Demo kill-switch activation/reset audit."""

from alembic import op
import sqlalchemy as sa


revision = "20260714_0011"
down_revision = "20260714_0010"
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    tables = set(inspector.get_table_names())
    if "demo_kill_switch_events" not in tables:
        op.create_table(
            "demo_kill_switch_events",
            sa.Column("id", sa.String(36), primary_key=True),
            sa.Column("event_type", sa.String(40), nullable=False),
            sa.Column("active", sa.Boolean(), nullable=False),
            sa.Column("reasons", sa.JSON(), nullable=False),
            sa.Column(
                "execution_id", sa.String(36),
                sa.ForeignKey("demo_executions.id"), nullable=True,
            ),
            sa.Column("payload", sa.JSON(), nullable=False),
            sa.Column(
                "created_at", sa.DateTime(timezone=True), nullable=False,
                server_default=sa.func.now(),
            ),
        )
        op.create_index(
            "ix_demo_kill_switch_events_created_at",
            "demo_kill_switch_events", ["created_at"], unique=False,
        )
        row = bind.execute(
            sa.text(
                "SELECT active, reasons, updated_at FROM demo_kill_switch "
                "WHERE id = 1"
            )
        ).mappings().first()
        if row and row["active"]:
            import json
            import uuid

            reasons = row["reasons"]
            if not isinstance(reasons, str):
                reasons = json.dumps(reasons)
            bind.execute(
                sa.text(
                    "INSERT INTO demo_kill_switch_events "
                    "(id,event_type,active,reasons,execution_id,payload,created_at) "
                    "VALUES (:id,'LEGACY_ACTIVATION',true,CAST(:reasons AS JSON),"
                    "NULL,CAST(:payload AS JSON),:created_at)"
                ),
                {
                    "id": str(uuid.uuid4()), "reasons": reasons,
                    "payload": "{}", "created_at": row["updated_at"],
                },
            )


def downgrade() -> None:
    op.drop_index(
        "ix_demo_kill_switch_events_created_at",
        table_name="demo_kill_switch_events",
        if_exists=True,
    )
    op.drop_table("demo_kill_switch_events", if_exists=True)
