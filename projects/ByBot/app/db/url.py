from __future__ import annotations


def normalize_database_url(database_url: str) -> str:
    """Select psycopg v3 for legacy PostgreSQL URLs; leave other URLs unchanged."""
    value = database_url.strip()
    if value.startswith("postgresql://"):
        return value.replace("postgresql://", "postgresql+psycopg://", 1)
    return value
