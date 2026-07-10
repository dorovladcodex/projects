from __future__ import annotations

from datetime import datetime, timezone

from fastapi import FastAPI

from app.bybit.market_data import build_market_data_service
from app.config import get_settings
from app.runtime import build_status

settings = get_settings()
app = FastAPI(title="ByBot", version="0.1.0")
market_data_service = build_market_data_service(settings)


@app.get("/health")
def health() -> dict[str, str]:
    return {"status": "ok", "timestamp": datetime.now(timezone.utc).isoformat()}


@app.get("/status")
def status() -> dict[str, object]:
    market_data_service.refresh_all()
    return build_status(settings, market_data_service)


@app.get("/market")
def market() -> dict[str, object]:
    market_data_service.refresh_all()
    return market_data_service.as_payload()
