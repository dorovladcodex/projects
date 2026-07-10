from __future__ import annotations

from datetime import datetime, timezone

from fastapi import FastAPI

from app.config import get_settings
from app.runtime import build_status

settings = get_settings()
app = FastAPI(title="ByBot", version="0.1.0")


@app.get("/health")
def health() -> dict[str, str]:
    return {"status": "ok", "timestamp": datetime.now(timezone.utc).isoformat()}


@app.get("/status")
def status() -> dict[str, object]:
    return build_status(settings)
