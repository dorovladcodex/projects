from __future__ import annotations

from pathlib import Path

from alembic.config import Config
from alembic.script import ScriptDirectory
from sqlalchemy import Integer, Numeric, String

from app.db.persistence import Base


def test_v2_migration_is_the_single_alembic_head() -> None:
    config = Config("alembic.ini")
    script = ScriptDirectory.from_config(config)
    assert script.get_current_head() == "20260811_0015"


def test_v2_durable_tables_and_uniqueness_exist() -> None:
    expected = {
        "v2_symbol_universe", "v2_market_feature_snapshots",
        "v2_signal_candidates", "v2_portfolio_reservations",
        "v2_portfolio_state", "v2_signal_rejections", "v2_incidents", "v2_runs",
    }
    assert expected <= set(Base.metadata.tables)
    reservations = Base.metadata.tables["v2_portfolio_reservations"]
    assert isinstance(reservations.c.active_symbol.type, String)
    assert isinstance(reservations.c.notional_usdt.type, Numeric)
    assert reservations.c.candidate_id.unique
    assert {"v4_opportunities", "v4_forward_labels"} <= set(Base.metadata.tables)


def test_v4_migration_is_additive_and_extends_v2() -> None:
    text = Path(
        "alembic/versions/20260811_0015_v4_alpha_research.py"
    ).read_text(encoding="utf-8")
    assert 'down_revision = "20260715_0014"' in text
    assert "v4_opportunities" in text
    assert "v4_forward_labels" in text
    assert 'drop_table("v2_' not in text


def test_migration_extends_history_without_rewriting_it() -> None:
    text = Path("alembic/versions/20260715_0014_bybot_v2.py").read_text(encoding="utf-8")
    assert 'down_revision = "20260714_0013"' in text
    assert "op.create_table" in text
    assert "drop_table(\"demo_executions\")" not in text
