from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone

from fastapi import FastAPI, HTTPException

from app.bybit.market_data import build_market_data_service, snapshot_to_payload
from app.bybit.private import build_account_service
from app.config import get_settings
from app.models import (
    PaperTestSignalRequest,
    RiskContext,
    SignalAction,
    Symbol,
    TradeSignal,
)
from app.portfolio.paper_trading import PaperTradingService
from app.risk import RiskManager, RiskRules
from app.runtime import build_status

settings = get_settings()
market_data_service = build_market_data_service(settings)
account_service = build_account_service(settings)
paper_trading_service = PaperTradingService(
    timeout=timedelta(minutes=settings.paper_position_timeout_minutes)
)


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    account_service.refresh_if_stale(force=True)
    yield


app = FastAPI(title="ByBot", version="0.1.0", lifespan=lifespan)


@app.get("/health")
def health() -> dict[str, str]:
    return {"status": "ok", "timestamp": datetime.now(timezone.utc).isoformat()}


@app.get("/status")
def status() -> dict[str, object]:
    refresh_runtime_state(refresh_account=True)
    return build_status(settings, market_data_service, account_service, paper_trading_service)


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


@app.get("/account")
def account() -> dict[str, object]:
    account_service.refresh_if_stale(force=True)
    return account_service.as_payload()


@app.post("/paper/test-signal")
def paper_test_signal(request: PaperTestSignalRequest) -> dict[str, object]:
    refresh_runtime_state(refresh_account=True)
    if market_data_service.status != "OK":
        return {
            "accepted": False,
            "reason": "market data unavailable",
            "market_status": market_data_service.status,
            "last_error": market_data_service.last_error,
        }

    market_snapshot = market_data_service.latest_snapshot(request.symbol)
    if market_snapshot is None:
        return {
            "accepted": False,
            "reason": "market snapshot unavailable",
            "market_status": market_data_service.status,
        }

    signal = TradeSignal(
        action=SignalAction.TRADE,
        symbol=request.symbol,
        side=request.side,
        confidence=request.confidence,
        expected_edge_bps=request.expected_edge_bps,
        stop_loss_pct=request.stop_loss_pct,
        reasons=["manual paper test signal"],
    )
    equity = account_service.status.equity or settings.paper_starting_equity
    risk_context = RiskContext(
        equity=equity,
        requested_risk_pct=request.requested_risk_pct or settings.max_risk_per_trade_pct,
        leverage=request.leverage or settings.max_leverage,
        open_positions=1 if paper_trading_service.open_position else 0,
        daily_pnl_pct=settings.paper_daily_pnl_pct,
        weekly_pnl_pct=settings.paper_weekly_pnl_pct,
        consecutive_losses=settings.paper_consecutive_losses,
        api_stable=market_data_service.status == "OK",
    )
    risk_rules = RiskRules(
        max_risk_per_trade_pct=settings.max_risk_per_trade_pct,
        max_daily_loss_pct=settings.max_daily_loss_pct,
        max_weekly_loss_pct=settings.max_weekly_loss_pct,
        max_leverage=settings.max_leverage,
        max_spread_bps=settings.max_spread_bps,
        min_confidence=settings.min_llm_confidence,
        min_expected_edge_bps=settings.min_expected_edge_bps,
    )
    risk_decision = RiskManager(risk_rules).assess(signal, market_snapshot, risk_context)
    if not risk_decision.approved:
        paper_trading_service.last_risk_decision = risk_decision
        return {
            "accepted": False,
            "reason": "risk manager rejected signal",
            "risk_decision": risk_decision.model_dump(mode="json"),
        }

    position = paper_trading_service.open_from_signal(
        signal,
        risk_decision,
        market_snapshot,
        take_profit_pct=request.take_profit_pct or settings.paper_take_profit_pct,
    )
    return {
        "accepted": True,
        "position": position.model_dump(mode="json"),
        "risk_decision": risk_decision.model_dump(mode="json"),
        "real_exchange_execution": "blocked",
    }


@app.get("/paper/positions")
def paper_positions() -> dict[str, object]:
    refresh_runtime_state(refresh_account=False)
    return {"positions": paper_trading_service.positions_payload()}


@app.post("/paper/close-position")
def paper_close_position() -> dict[str, object]:
    refresh_runtime_state(refresh_account=False)
    open_position = paper_trading_service.open_position
    if open_position is None:
        return {
            "closed": False,
            "reason": "no open paper position",
            "last_trade": (
                paper_trading_service.closed_trades[-1].model_dump(mode="json")
                if paper_trading_service.closed_trades
                else None
            ),
        }

    market_snapshot = market_data_service.latest_snapshot(open_position.symbol)
    if market_data_service.status != "OK" or market_snapshot is None:
        return {
            "closed": False,
            "reason": "market data unavailable",
            "market_status": market_data_service.status,
            "last_error": market_data_service.last_error,
        }

    closed = paper_trading_service.close_position(
        market_snapshot.last_price,
        reason="manual_close",
    )
    return {
        "closed": True,
        "position": closed.model_dump(mode="json"),
        "pnl": paper_trading_service.pnl().model_dump(mode="json"),
        "real_exchange_execution": "blocked",
    }


@app.get("/paper/trades")
def paper_trades() -> dict[str, object]:
    refresh_runtime_state(refresh_account=False)
    return {"trades": paper_trading_service.trades_payload()}


@app.get("/paper/pnl")
def paper_pnl() -> dict[str, object]:
    refresh_runtime_state(refresh_account=False)
    return paper_trading_service.pnl().model_dump(mode="json")


def refresh_runtime_state(*, refresh_account: bool) -> None:
    market_data_service.refresh_all()
    if refresh_account:
        account_service.refresh_if_stale()
    for snapshot in market_data_service.latest_snapshots():
        paper_trading_service.update_from_market(snapshot)
