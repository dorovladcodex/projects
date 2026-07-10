from __future__ import annotations

from datetime import datetime, timezone

from fastapi import FastAPI, HTTPException

from app.bybit.market_data import build_market_data_service, snapshot_to_payload
from app.config import get_settings
from app.models import Symbol
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


@app.get("/market/{symbol}")
def market_symbol(symbol: str) -> dict[str, object]:
    try:
        parsed_symbol = Symbol(symbol.upper())
    except ValueError as exc:
        raise HTTPException(status_code=404, detail="Unsupported symbol") from exc

    market_data_service.refresh_all()
    snapshot = market_data_service.latest_snapshot(parsed_symbol)
    if snapshot is None:
        return {
            "status": market_data_service.status,
            "last_error": market_data_service.last_error,
            "snapshot": None,
        }

    return {
        "status": market_data_service.status,
        "last_error": market_data_service.last_error,
        "snapshot": snapshot_to_payload(snapshot),
    }
