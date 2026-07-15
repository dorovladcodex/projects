from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal
from uuid import uuid4

import pytest

from app.config import Settings
from app.db.persistence import PersistenceRepository
from app.models import (
    BotEvent, DemoExecutionRecord, DemoExecutionState, Side, Symbol,
)
from app.v2.execution import V2ExecutionCoordinator
from app.v2.models import (
    MarketFeatureSnapshot, SourceHealth, StrategyName, StrategySide,
    UniverseInstrument, UniverseState, UniverseStatus,
)
from app.v2.portfolio import PortfolioRiskService
from app.v2.scoring import CommonScoringPipeline
from app.v2.strategies import NewsStrategyContext, build_v2_strategies


def demo_settings() -> Settings:
    return Settings(
        app_env="demo", test_mode=False, bot_mode="BYBIT_DEMO",
        execution_mode="BYBIT_DEMO", bybit_env="demo",
        bybit_demo_trading_enabled=True, bybit_live_trading_enabled=False,
        demo_order_execution_authorized=True,
        bybit_enable_trading=False, bybit_api_key="fake", bybit_api_secret="fake",
        v2_enabled=True, v2_auto_demo_execution=True,
        allowed_symbols=tuple(symbol.value for symbol in Symbol),
        v2_min_turnover_24h_usdt=Decimal("0"),
        v2_min_orderbook_depth_usdt=Decimal("0"),
        v2_global_entry_cooldown_seconds=0,
    )


def rules(symbol: Symbol) -> UniverseInstrument:
    return UniverseInstrument(
        symbol=symbol, status="Trading", category="LinearPerpetual", settle_coin="USDT",
        min_order_qty=Decimal("0.001"), qty_step=Decimal("0.001"),
        min_notional_value=Decimal("5"), min_leverage=Decimal("1"),
        max_leverage=Decimal("100"), leverage_step=Decimal("0.01"),
        tick_size=Decimal("0.01"), turnover_24h=Decimal("1000000"),
        spread_bps=Decimal("2"), bid_depth_usdt=Decimal("10000"),
        ask_depth_usdt=Decimal("10000"), market_timestamp=datetime.now(timezone.utc),
    )


def features(symbol: Symbol, direction: int = 1) -> MarketFeatureSnapshot:
    sign = Decimal(direction)
    windows = ("10s","30s","1m","3m","5m","15m","1h")
    return MarketFeatureSnapshot(
        symbol=symbol, timestamp=datetime.now(timezone.utc), fresh=True,
        last_price=Decimal("100"), bid_price=Decimal("99.99"), ask_price=Decimal("100.01"),
        spread_bps=Decimal("2"), bid_depth_usdt=Decimal("100000"), ask_depth_usdt=Decimal("100000"),
        price_momentum={key: sign * Decimal("50") for key in windows},
        breakout_distance_bps={key: sign * Decimal("40") for key in windows},
        volume_acceleration={key: Decimal("3") for key in windows},
        trade_imbalance={key: sign * Decimal("0.8") for key in windows},
        orderbook_imbalance=sign * Decimal("0.6"),
        realized_volatility={key: Decimal("10") for key in windows},
        atr_bps=Decimal("25"), relative_strength_vs_btc_bps=sign * Decimal("40"),
        open_interest_change_pct=Decimal("3"), funding_deviation_bps=-sign * Decimal("5"),
        liquidation_imbalance=sign * Decimal("0.9"), volume_24h=Decimal("1000000"),
        market_regime="TRENDING_UP" if direction > 0 else "TRENDING_DOWN",
        source_health={"ticker": SourceHealth.OK, "trades": SourceHealth.OK, "orderbook": SourceHealth.OK},
    )


class UniverseStub:
    def __init__(self) -> None:
        self.statuses = {
            symbol: UniverseStatus(symbol=symbol, state=UniverseState.ACCEPTED, accepted=True, instrument=rules(symbol))
            for symbol in Symbol
        }
    def get(self, symbol: Symbol) -> UniverseStatus:
        return self.statuses[symbol]


class DemoStub:
    def __init__(self) -> None:
        self.kill_switch_active = False; self.kill_switch_reasons = []
        self.account_verified = True; self.last_error = None; self.calls = []
    def submit_candidate(self, candidate, preview, classification, snapshot, **kwargs):
        self.calls.append((candidate, preview, classification, snapshot, kwargs))
        return DemoExecutionRecord(
            candidate_id=candidate.id, risk_decision_id=preview.risk_decision_id,
            run_id="run", order_link_id=f"test-{candidate.id.hex[:20]}",
            state=DemoExecutionState.DEMO_POSITION_OPEN, symbol=candidate.symbol,
            side=Side.BUY if candidate.final_action.value == "BUY" else Side.SELL,
            requested_quantity=Decimal(str(preview.capped_size)),
            accepted_quantity=Decimal(str(preview.capped_size)),
            average_fill_price=Decimal(str(snapshot.last_price)),
            protection_confirmed=True, leverage=kwargs["desired_leverage"],
            strategy_name=kwargs["strategy_name"],
        )


def coordinator() -> tuple[V2ExecutionCoordinator, PersistenceRepository, DemoStub]:
    config = demo_settings()
    repository = PersistenceRepository("sqlite+pysqlite:///:memory:")
    universe = UniverseStub()
    portfolio = PortfolioRiskService(config, repository)
    demo = DemoStub()
    service = V2ExecutionCoordinator(config, repository, universe, portfolio, demo, run_id="run")
    return service, repository, demo


@pytest.mark.parametrize("direction,expected", [(1, "BUY"), (-1, "SELL")])
def test_long_and_short_use_guarded_demo_adapter(direction: int, expected: str) -> None:
    service, repository, demo = coordinator()
    strategy = build_v2_strategies(service.settings)[1]
    candidate = strategy.evaluate(features(Symbol.BTCUSDT, direction))
    candidate.run_id = "run"
    candidate = CommonScoringPipeline(service.settings).admit(candidate, symbol_valid=True, portfolio_reasons=[])
    assert candidate.admitted
    assert repository.save_v2_signal_candidate(candidate)
    result = service.execute(candidate)
    assert result["execution_attempted"]
    assert demo.calls[0][0].final_action.value == expected
    assert demo.calls[0][4]["desired_leverage"] == Decimal("3")


def test_two_symbols_can_execute_independently() -> None:
    service, repository, demo = coordinator()
    strategy = build_v2_strategies(service.settings)[1]
    for symbol in (Symbol.BTCUSDT, Symbol.ETHUSDT):
        candidate = strategy.evaluate(features(symbol)); candidate.run_id = "run"
        candidate = CommonScoringPipeline(service.settings).admit(candidate, symbol_valid=True, portfolio_reasons=[])
        repository.save_v2_signal_candidate(candidate)
        assert service.execute(candidate)["execution_attempted"]
    assert len(demo.calls) == 2
    assert {call[0].symbol for call in demo.calls} == {Symbol.BTCUSDT, Symbol.ETHUSDT}


def test_duplicate_entry_is_blocked_before_second_adapter_call() -> None:
    service, repository, demo = coordinator()
    strategy = build_v2_strategies(service.settings)[1]
    first = strategy.evaluate(features(Symbol.BTCUSDT)); first.run_id = "run"
    first = CommonScoringPipeline(service.settings).admit(first, symbol_valid=True, portfolio_reasons=[])
    repository.save_v2_signal_candidate(first); service.execute(first)
    second = strategy.evaluate(features(Symbol.BTCUSDT)); second.run_id = "run"
    second = CommonScoringPipeline(service.settings).admit(second, symbol_valid=True, portfolio_reasons=[])
    repository.save_v2_signal_candidate(second)
    result = service.execute(second)
    assert not result["execution_attempted"]
    assert len(demo.calls) == 1


def test_kill_switch_blocks_without_exchange_mutation() -> None:
    service, repository, demo = coordinator(); demo.kill_switch_active = True
    strategy = build_v2_strategies(service.settings)[1]
    candidate = strategy.evaluate(features(Symbol.BTCUSDT)); candidate.run_id = "run"
    candidate = CommonScoringPipeline(service.settings).admit(candidate, symbol_valid=True, portfolio_reasons=[])
    repository.save_v2_signal_candidate(candidate)
    result = service.execute(candidate)
    assert not result["execution_attempted"] and not demo.calls


def test_all_strategies_are_wired_to_same_demo_coordinator() -> None:
    for strategy in build_v2_strategies(demo_settings()):
        service, repository, demo = coordinator()
        symbol = Symbol.PEPEUSDT if strategy.name == StrategyName.MEME_TREND else Symbol.BTCUSDT
        snapshot = features(symbol)
        if strategy.name == StrategyName.NEWS_MOMENTUM_V2:
            candidate = strategy.evaluate(snapshot, news=NewsStrategyContext("BULLISH", Decimal("1"), Decimal("1"), ("n",)))
        else:
            candidate = strategy.evaluate(snapshot)
        candidate.raw_strategy_score = Decimal("1"); candidate.confidence = Decimal("1")
        candidate.estimated_edge_bps = Decimal("100"); candidate.run_id = "run"
        candidate = CommonScoringPipeline(service.settings).admit(candidate, symbol_valid=True, portfolio_reasons=[])
        repository.save_v2_signal_candidate(candidate)
        result = service.execute(candidate)
        assert result["execution_attempted"], strategy.name
        assert demo.calls[0][4]["strategy_name"] == strategy.name.value


def test_settings_never_enable_live_or_generic_trading() -> None:
    config = demo_settings()
    assert config.bybit_live_trading_enabled is False
    assert config.bybit_enable_trading is False
