from __future__ import annotations

from pathlib import Path

from sqlalchemy import engine_from_config

from app.config import Settings
from app.db.url import normalize_database_url


def test_legacy_postgresql_url_is_normalized_to_psycopg_v3() -> None:
    legacy = "postgresql://user:password@localhost:5432/bybot"

    assert normalize_database_url(legacy) == (
        "postgresql+psycopg://user:password@localhost:5432/bybot"
    )
    assert Settings(_env_file=None, database_url=legacy).database_url.startswith(
        "postgresql+psycopg://"
    )


def test_alembic_engine_from_config_uses_psycopg_v3() -> None:
    # Alembic env.py explicitly applies the same normalizer before calling
    # engine_from_config. Engine construction imports psycopg but does not connect.
    configuration = {
        "sqlalchemy.url": normalize_database_url(
            "postgresql://user:password@localhost:5432/bybot"
        )
    }

    engine = engine_from_config(configuration, prefix="sqlalchemy.")

    assert engine.dialect.name == "postgresql"
    assert engine.dialect.driver == "psycopg"


def test_project_never_declares_psycopg2_dependency() -> None:
    requirements = Path("requirements.txt").read_text(encoding="utf-8").lower()

    assert "psycopg[binary]" in requirements
    assert "psycopg2" not in requirements


def test_runtime_postgresql_urls_explicitly_select_psycopg_v3() -> None:
    files = (".env.example", "alembic.ini", "docker-compose.yml", "README.md")

    for filename in files:
        content = Path(filename).read_text(encoding="utf-8")
        assert "postgresql+psycopg://" in content
