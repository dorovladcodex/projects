from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone
from decimal import Decimal
import json
from pathlib import Path
from threading import Thread
from uuid import uuid4

import pytest

from app.config import Settings
from app.db.persistence import PersistenceRepository
from app.models import ExecutionEnvironment, Symbol
from app.v2.analytics import V2ReportGenerator
from app.v2.execution import V2ExecutionCoordinator
from app.v2.market import BybitRestMetricsPoller, RollingFeatureEngine
from app.v2.models import (
    MarketFeatureSnapshot, SourceHealth, StrategyName, StrategySide,
    UniverseInstrument, UniverseStatus, UniverseState,
)
from app.v2.news import EntityMapper, V2NewsAggregator, semantic_fingerprint
from app.v2.portfolio import PortfolioRiskService, normalize_leverage, normalize_order_quantity
from app.v2.scoring import CommonScoringPipeline
from app.v2.strategies import (
    MemeTrendContext, NewsStrategyContext, build_v2_strategies,
)
from app.v2.universe import SymbolUniverseService


def settings(**overrides: object) -> Settings:
    values = {
        "v2_enabled": True, "allowed_symbols": tuple(symbol.value for symbol in Symbol),
        "v2_min_turnover_24h_usdt": Decimal("100"),
        "v2_min_orderbook_depth_usdt": Decimal("100"),
        "v2_max_spread_bps": Decimal("20"),
        "v2_global_entry_cooldown_seconds": 0,
    }
    values.update(overrides)
    return Settings(**values)


def instrument(symbol: Symbol, **updates: object) -> UniverseInstrument:
    values = {
        "symbol": symbol, "status": "Trading", "category": "LinearPerpetual",
        "settle_coin": "USDT", "min_order_qty": Decimal("0.001"),
        "qty_step": Decimal("0.001"), "min_notional_value": Decimal("5"),
        "min_leverage": Decimal("1"), "max_leverage": Decimal("100"),
        "leverage_step": Decimal("0.01"), "tick_size": Decimal("0.1"),
        "turnover_24h": Decimal("10000000"), "spread_bps": Decimal("2"),
        "bid_depth_usdt": Decimal("50000"), "ask_depth_usdt": Decimal("50000"),
        "market_timestamp": datetime.now(timezone.utc),
    }
    values.update(updates)
    return UniverseInstrument(**values)


def feature(symbol: Symbol = Symbol.BTCUSDT, *, fresh: bool = True) -> MarketFeatureSnapshot:
    now = datetime.now(timezone.utc)
    return MarketFeatureSnapshot(
        symbol=symbol, timestamp=now, fresh=fresh,
        stale_reasons=[] if fresh else ["ticker is stale"],
        last_price=Decimal("100"), bid_price=Decimal("99.99"), ask_price=Decimal("100.01"),
        spread_bps=Decimal("2"), bid_depth_usdt=Decimal("50000"), ask_depth_usdt=Decimal("50000"),
        price_momentum={key: Decimal("45") for key in ("10s","30s","1m","3m","5m","15m","1h")},
        breakout_distance_bps={key: Decimal("30") for key in ("10s","30s","1m","3m","5m","15m","1h")},
        volume_acceleration={key: Decimal("2.5") for key in ("10s","30s","1m","3m","5m","15m","1h")},
        trade_imbalance={key: Decimal("0.65") for key in ("10s","30s","1m","3m","5m","15m","1h")},
        orderbook_imbalance=Decimal("0.55"),
        realized_volatility={key: Decimal("12") for key in ("10s","30s","1m","3m","5m","15m","1h")},
        atr_bps=Decimal("30"), distance_from_high_bps=Decimal("-15"),
        distance_from_low_bps=Decimal("90"), relative_strength_vs_btc_bps=Decimal("30"),
        funding_rate=Decimal("-0.0005"), funding_deviation_bps=Decimal("-5"),
        open_interest=Decimal("1000000"), open_interest_change_pct=Decimal("3"),
        liquidation_short_usdt=Decimal("100000"), liquidation_long_usdt=Decimal("1000"),
        liquidation_imbalance=Decimal("0.98"), volume_24h=Decimal("10000000"),
        market_regime="TRENDING_UP",
        source_health={"ticker": SourceHealth.OK, "trades": SourceHealth.OK, "orderbook": SourceHealth.OK},
    )


class UniverseFake:
    def __init__(self, rejected: Symbol | None = None, failing: Symbol | None = None) -> None:
        self.rejected = rejected; self.failing = failing
    def inspect_symbol(self, symbol: Symbol) -> UniverseInstrument:
        if symbol == self.failing:
            raise TimeoutError
        return instrument(symbol, turnover_24h=Decimal("1") if symbol == self.rejected else Decimal("10000000"))


def test_universe_evaluates_all_17_and_isolates_failures() -> None:
    service = SymbolUniverseService(settings(), UniverseFake(Symbol.PEPEUSDT, Symbol.TONUSDT))
    result = service.refresh()
    assert len(result) == 17
    assert result[Symbol.BTCUSDT].accepted
    assert result[Symbol.PEPEUSDT].state == UniverseState.REJECTED
    assert result[Symbol.TONUSDT].state == UniverseState.DATA_UNAVAILABLE


def test_quantity_and_leverage_are_decimal_normalized() -> None:
    rules = instrument(Symbol.BTCUSDT, min_order_qty=Decimal("0.01"), qty_step=Decimal("0.01"), min_notional_value=Decimal("12"), max_leverage=Decimal("2.5"), leverage_step=Decimal("0.5"))
    assert normalize_order_quantity(Decimal("20"), Decimal("101"), rules, Decimal("25")) == Decimal("0.19")
    assert normalize_leverage(Decimal("3"), rules) == Decimal("2.5")


def test_exchange_minimum_above_cap_is_rejected() -> None:
    with pytest.raises(ValueError, match="minimum quantity"):
        normalize_order_quantity(Decimal("5"), Decimal("100"), instrument(Symbol.BTCUSDT, min_order_qty=Decimal("1")), Decimal("20"))


def test_portfolio_allows_independent_symbols_and_blocks_duplicate() -> None:
    service = PortfolioRiskService(settings(max_concurrent_positions=8))
    first = service.reserve(run_id="r", candidate_id=uuid4(), symbol=Symbol.BTCUSDT, strategy_name=StrategyName.VOLUME_BREAKOUT, notional=Decimal("50"), risk_usdt=Decimal("1"))
    second = service.reserve(run_id="r", candidate_id=uuid4(), symbol=Symbol.ETHUSDT, strategy_name=StrategyName.VOLUME_BREAKOUT, notional=Decimal("50"), risk_usdt=Decimal("1"))
    duplicate = service.reserve(run_id="r", candidate_id=uuid4(), symbol=Symbol.BTCUSDT, strategy_name=StrategyName.VOLUME_BREAKOUT, notional=Decimal("50"), risk_usdt=Decimal("1"))
    assert first and second and duplicate is None


def test_portfolio_limits_meme_and_correlation_groups() -> None:
    service = PortfolioRiskService(settings(max_meme_positions=1, max_positions_per_correlation_group=2))
    assert service.reserve(run_id="r", candidate_id=uuid4(), symbol=Symbol.WIFUSDT, strategy_name=StrategyName.MEME_TREND, notional=Decimal("25"), risk_usdt=Decimal("1"))
    assert "maximum meme positions reached" in service.block_reasons(Symbol.BONKUSDT, Decimal("25"))
    service = PortfolioRiskService(settings(max_positions_per_correlation_group=1))
    assert service.reserve(run_id="r", candidate_id=uuid4(), symbol=Symbol.BTCUSDT, strategy_name=StrategyName.VOLUME_BREAKOUT, notional=Decimal("25"), risk_usdt=Decimal("1"))
    assert "maximum positions for correlation group reached" in service.block_reasons(Symbol.ETHUSDT, Decimal("25"))


def test_portfolio_entry_rate_and_daily_cap() -> None:
    service = PortfolioRiskService(settings(max_new_entries_per_5_minutes=1, max_positions_per_symbol=2))
    service.reserve(run_id="r", candidate_id=uuid4(), symbol=Symbol.BTCUSDT, strategy_name=StrategyName.VOLUME_BREAKOUT, notional=Decimal("25"), risk_usdt=Decimal("1"))
    assert "five-minute entry rate limit reached" in service.block_reasons(Symbol.ETHUSDT, Decimal("25"))


def test_per_symbol_lock_serializes() -> None:
    service = PortfolioRiskService(settings())
    order: list[int] = []
    def worker(index: int) -> None:
        with service.symbol_lock(Symbol.BTCUSDT):
            order.append(index)
    threads = [Thread(target=worker, args=(index,)) for index in range(4)]
    [thread.start() for thread in threads]; [thread.join() for thread in threads]
    assert sorted(order) == [0, 1, 2, 3]


@pytest.mark.parametrize("strategy_name", list(StrategyName))
def test_all_five_strategies_produce_typed_long_candidates(strategy_name: StrategyName) -> None:
    strategy = next(item for item in build_v2_strategies(settings()) if item.name == strategy_name)
    snapshot = feature(Symbol.PEPEUSDT if strategy_name == StrategyName.MEME_TREND else Symbol.BTCUSDT)
    if strategy_name == StrategyName.NEWS_MOMENTUM_V2:
        result = strategy.evaluate(snapshot, news=NewsStrategyContext("BULLISH", Decimal("0.95"), Decimal("0.95"), ("n",)))
    elif strategy_name == StrategyName.MEME_TREND:
        result = strategy.evaluate(snapshot, meme=MemeTrendContext(Decimal("0.9")))
    else:
        result = strategy.evaluate(snapshot)
    assert result.strategy_name == strategy_name
    assert result.side == StrategySide.LONG
    assert result.execution_environment == ExecutionEnvironment.BYBIT_DEMO


def test_short_paths_exist_for_all_market_strategies() -> None:
    snapshot = feature().model_copy(deep=True)
    snapshot.price_momentum = {key: -abs(value) for key, value in snapshot.price_momentum.items()}
    snapshot.breakout_distance_bps = {key: -abs(value) for key, value in snapshot.breakout_distance_bps.items()}
    snapshot.trade_imbalance = {key: -abs(value) for key, value in snapshot.trade_imbalance.items()}
    snapshot.liquidation_imbalance = Decimal("-0.98")
    for strategy in build_v2_strategies(settings())[1:4]:
        assert strategy.evaluate(snapshot).side == StrategySide.SHORT


def test_common_scoring_rejects_stale_data_and_insufficient_edge() -> None:
    strategy = build_v2_strategies(settings())[1]
    candidate = strategy.evaluate(feature(fresh=False))
    candidate.estimated_edge_bps = Decimal("1")
    result = CommonScoringPipeline(settings()).admit(candidate, symbol_valid=True, portfolio_reasons=[])
    assert not result.admitted
    assert "stale" in (result.rejection_reason or "")
    assert "expected edge" in (result.rejection_reason or "")


def test_entity_mapping_all_symbols_and_ecosystem() -> None:
    mapper = EntityMapper()
    assert mapper.symbols_for_text("Solana and BONK adoption") == (Symbol.SOLUSDT, Symbol.BONKUSDT)
    assert mapper.ecosystem_symbols(Symbol.SOLUSDT) == (Symbol.SOLUSDT, Symbol.WIFUSDT, Symbol.BONKUSDT)


class AsyncSource:
    name = "test"; reliability = 1.0
    def __init__(self, items: list[object] | None = None, fail: bool = False) -> None:
        self.items = items or []; self.fail = fail
    async def fetch(self) -> list[object]:
        if self.fail: raise TimeoutError
        return list(self.items)


def _news(title: str, item_id: object | None = None) -> object:
    from app.models import NewsItem
    return NewsItem(id=item_id or uuid4(), title=title, summary="Bitcoin market update", source="x", url="https://x.test/a", published_at=datetime.now(timezone.utc))


def test_multisource_dedup_and_failure_isolation() -> None:
    item1 = _news("Bitcoin ETF approved")
    item2 = _news("Bitcoin ETF approved")
    service = V2NewsAggregator([AsyncSource([item1]), AsyncSource(fail=True)])
    first = asyncio.run(service.poll()); second = asyncio.run(V2NewsAggregator([AsyncSource([item2])]).poll())
    assert len(first) == 1 and len(second) == 1
    service = V2NewsAggregator([AsyncSource([item1]), AsyncSource([item2])])
    assert len(asyncio.run(service.poll())) == 1
    assert service.source_failures == 0


def test_rolling_features_and_stale_detection() -> None:
    engine = RollingFeatureEngine(settings(v2_market_stale_seconds=5))
    now = datetime.now(timezone.utc)
    engine.ingest_ticker(Symbol.BTCUSDT, {"lastPrice": "101", "volume24h": "1000"}, now)
    engine.ingest_orderbook(Symbol.BTCUSDT, [["100.9", "10"]], [["101.1", "10"]], now)
    for index in range(5):
        engine.ingest_trade(Symbol.BTCUSDT, Decimal(str(100 + index / 4)), Decimal("1"), "Buy", now - timedelta(seconds=4-index))
    result = engine.snapshot(Symbol.BTCUSDT, now=now)
    assert result and result.fresh and result.price_momentum["10s"] > 0
    stale = engine.snapshot(Symbol.BTCUSDT, now=now + timedelta(seconds=10))
    assert stale and not stale.fresh


def test_rest_metric_source_failure_is_isolated_per_symbol() -> None:
    engine = RollingFeatureEngine(settings())
    def getter(url: str, params: dict[str, str], timeout: float) -> dict[str, object]:
        del timeout
        if params["symbol"] == "ETHUSDT":
            raise TimeoutError
        if url.endswith("tickers"):
            return {"retCode": 0, "result": {"list": [{"lastPrice": "100", "volume24h": "2000"}]}}
        if url.endswith("funding/history"):
            return {"retCode": 0, "result": {"list": [{"fundingRate": "0.0001"}]}}
        return {"retCode": 0, "result": {"list": [{"openInterest": "5000"}]}}
    poller = BybitRestMetricsPoller(settings(), engine, getter)
    poller.poll((Symbol.BTCUSDT, Symbol.ETHUSDT))
    assert Symbol.BTCUSDT in engine.funding
    assert poller.failures[Symbol.ETHUSDT] == "TimeoutError"


def test_sqlite_persistence_reservation_survives_service_restart() -> None:
    repository = PersistenceRepository("sqlite+pysqlite:///:memory:")
    candidate = build_v2_strategies(settings())[1].evaluate(feature())
    candidate.run_id = "run"; repository.save_v2_signal_candidate(candidate)
    service = PortfolioRiskService(settings(), repository)
    reservation = service.reserve(run_id="run", candidate_id=candidate.id, symbol=candidate.symbol, strategy_name=candidate.strategy_name, notional=Decimal("50"), risk_usdt=Decimal("1"))
    assert reservation
    restarted = PortfolioRiskService(settings(), repository)
    assert len(restarted.reservations) == 1
    assert restarted.reserve(run_id="run", candidate_id=uuid4(), symbol=Symbol.BTCUSDT, strategy_name=StrategyName.VOLUME_BREAKOUT, notional=Decimal("50"), risk_usdt=Decimal("1")) is None


class ReportRepo:
    def v2_report_rows(self, run_id: str) -> dict[str, list[dict[str, object]]]:
        return {"signals": [{"run_id": run_id, "id": "c", "strategy_name": "s", "symbol": "BTCUSDT", "score_components": {}}], "rejections": [], "incidents": [], "executions": []}


def test_reports_are_run_scoped(tmp_path: Path) -> None:
    report = V2ReportGenerator(ReportRepo(), str(tmp_path)).generate("r1")
    assert report["run_id"] == "r1"
    assert (tmp_path / "r1" / "summary.json").exists()
    assert (tmp_path / "r1" / "trades.csv").exists()


def test_soak_runner_is_explicit_demo_only_and_not_self_executing() -> None:
    text = Path("scripts/demo_v2_soak.ps1").read_text(encoding="utf-8")
    assert "AllowDemoOrders" in text
    assert "BYBIT_ENABLE_TRADING' 'false" in text
    assert "BYBIT_LIVE_TRADING_ENABLED' 'false" in text
    assert "V2_AUTO_DEMO_EXECUTION' 'true" in text
    assert "demo_v2_preflight.py" in text


def test_live_mainnet_testnet_are_still_impossible() -> None:
    from pydantic import ValidationError
    with pytest.raises(ValidationError):
        Settings(bybit_live_trading_enabled=True)
    with pytest.raises(ValidationError):
        Settings(bybit_enable_trading=True)
