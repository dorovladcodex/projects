from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from typing import Protocol

from app.config import Settings
from app.models import Symbol
from app.v2.models import (
    MarketFeatureSnapshot, StrategyName, StrategySide, V2SignalCandidate,
)


@dataclass(frozen=True)
class NewsStrategyContext:
    sentiment: str
    confidence: Decimal
    importance: Decimal
    news_ids: tuple[str, ...] = ()
    market_wide: bool = False


@dataclass(frozen=True)
class MemeTrendContext:
    external_trend_score: Decimal = Decimal("0")


class V2Strategy(Protocol):
    name: StrategyName
    version: str
    enabled: bool
    def evaluate(self, features: MarketFeatureSnapshot, **context: object) -> V2SignalCandidate: ...


class _BaseStrategy:
    name: StrategyName
    version = "2.0.0"
    ttl_seconds = 180
    max_holding_seconds = 3600
    stop_risk_multiplier = Decimal("1.2")
    reward_multiple = Decimal("1.6")
    trailing_stop_pct: Decimal | None = None

    def __init__(self, settings: Settings, enabled: bool, threshold: Decimal | None = None) -> None:
        self.settings = settings
        self.enabled = enabled
        self.threshold = threshold or Decimal(str(settings.v2_strategy_default_threshold))

    def _candidate(
        self, features: MarketFeatureSnapshot, side: StrategySide,
        score: Decimal, confidence: Decimal, edge_bps: Decimal, reason: str,
        *, news_ids: tuple[str, ...] = (), ttl_seconds: int | None = None,
    ) -> V2SignalCandidate:
        now = datetime.now(timezone.utc)
        atr_pct = max(features.atr_bps / Decimal("100"), Decimal("0.20"))
        stop = min(Decimal("3"), atr_pct * self.stop_risk_multiplier)
        return V2SignalCandidate(
            run_id="unassigned", strategy_name=self.name, strategy_version=self.version,
            symbol=features.symbol, side=side, created_at=now,
            expires_at=now + timedelta(seconds=ttl_seconds or self.ttl_seconds),
            market_regime=features.market_regime, feature_snapshot=features,
            raw_strategy_score=_clamp(score), confidence=_clamp(confidence),
            estimated_edge_bps=edge_bps,
            expected_fees_bps=self.settings.v2_taker_fee_bps * Decimal("2"),
            expected_slippage_bps=self.settings.v2_slippage_bps * Decimal("2"),
            entry_reason=reason, threshold=self.threshold,
            distance_to_threshold=_clamp(score) - self.threshold, news_ids=list(news_ids),
            stop_loss_pct=stop, take_profit_pct=stop * self.reward_multiple,
            trailing_stop_pct=self.trailing_stop_pct, break_even_at_r=Decimal("1"),
            maximum_holding_seconds=self.max_holding_seconds,
        )


class NewsMomentumStrategyV2(_BaseStrategy):
    name = StrategyName.NEWS_MOMENTUM_V2
    ttl_seconds = 300
    max_holding_seconds = 10_800
    reward_multiple = Decimal("1.8")

    def evaluate(
        self, features: MarketFeatureSnapshot, **context: object
    ) -> V2SignalCandidate:
        news = context.get("news")
        if not isinstance(news, NewsStrategyContext):
            return self._candidate(features, StrategySide.LONG, Decimal("0"), Decimal("0"), Decimal("0"), "news context missing")
        side = StrategySide.LONG if news.sentiment.upper() == "BULLISH" else StrategySide.SHORT
        direction = Decimal("1") if side == StrategySide.LONG else Decimal("-1")
        confirmation = direction * features.price_momentum.get("1m", Decimal("0"))
        score = news.confidence * Decimal("0.45") + news.importance * Decimal("0.35") + _norm(confirmation, Decimal("40")) * Decimal("0.20")
        edge = max(Decimal("0"), confirmation) + news.importance * Decimal("30")
        return self._candidate(features, side, score, news.confidence, edge, "important classified news with market confirmation", news_ids=news.news_ids)


class VolumeBreakoutStrategy(_BaseStrategy):
    name = StrategyName.VOLUME_BREAKOUT
    ttl_seconds = 90
    max_holding_seconds = 3600
    reward_multiple = Decimal("1.5")
    trailing_stop_pct = Decimal("0.35")

    def evaluate(self, features: MarketFeatureSnapshot, **context: object) -> V2SignalCandidate:
        del context
        breakout = max(features.breakout_distance_bps.get(key, Decimal("0")) for key in ("30s", "1m", "3m", "5m"))
        downside = min(features.breakout_distance_bps.get(key, Decimal("0")) for key in ("30s", "1m", "3m", "5m"))
        side = (
            StrategySide.LONG
            if abs(breakout) > abs(downside)
            else StrategySide.SHORT
            if abs(downside) > abs(breakout)
            else StrategySide.LONG
            if features.price_momentum.get("1m", Decimal("0")) >= 0
            else StrategySide.SHORT
        )
        signed = breakout if side == StrategySide.LONG else -downside
        imbalance = features.trade_imbalance.get("1m", Decimal("0")) * (Decimal("1") if side == StrategySide.LONG else Decimal("-1"))
        volume = features.volume_acceleration.get("1m", Decimal("0"))
        overextended = abs(features.distance_from_low_bps if side == StrategySide.LONG else features.distance_from_high_bps) > Decimal("350")
        score = _norm(signed, Decimal("35")) * Decimal("0.4") + _norm(volume - 1, Decimal("2")) * Decimal("0.3") + _norm(imbalance, Decimal("0.5")) * Decimal("0.3")
        if overextended:
            score -= Decimal("0.35")
        return self._candidate(features, side, score, _clamp(score), signed + volume * Decimal("12"), "multi-window volume breakout")


class OIFundingSqueezeStrategy(_BaseStrategy):
    name = StrategyName.OI_FUNDING_SQUEEZE
    max_holding_seconds = 7200
    reward_multiple = Decimal("2")
    trailing_stop_pct = Decimal("0.5")

    def evaluate(self, features: MarketFeatureSnapshot, **context: object) -> V2SignalCandidate:
        del context
        price = features.price_momentum.get("5m", Decimal("0"))
        oi = features.open_interest_change_pct or Decimal("0")
        funding = features.funding_deviation_bps or Decimal("0")
        side = StrategySide.LONG if price >= 0 else StrategySide.SHORT
        signed = Decimal("1") if side == StrategySide.LONG else Decimal("-1")
        crowding = max(Decimal("0"), -signed * funding)
        score = _norm(abs(price), Decimal("50")) * Decimal("0.35") + _norm(abs(oi), Decimal("3")) * Decimal("0.30") + _norm(crowding, Decimal("5")) * Decimal("0.20") + _norm(signed * features.trade_imbalance.get("1m", Decimal("0")), Decimal("0.5")) * Decimal("0.15")
        label = "short covering / healthy momentum" if side == StrategySide.LONG else "long unwinding / healthy downside momentum"
        return self._candidate(features, side, score, _clamp(score), abs(price) + abs(oi) * Decimal("5"), label)


class LiquidationMomentumStrategy(_BaseStrategy):
    name = StrategyName.LIQUIDATION_MOMENTUM
    ttl_seconds = 60
    max_holding_seconds = 2400
    reward_multiple = Decimal("1.5")
    trailing_stop_pct = Decimal("0.35")

    def evaluate(self, features: MarketFeatureSnapshot, **context: object) -> V2SignalCandidate:
        del context
        imbalance = features.liquidation_imbalance
        side = StrategySide.LONG if imbalance >= 0 else StrategySide.SHORT
        signed_momentum = features.price_momentum.get("30s", Decimal("0")) * (Decimal("1") if side == StrategySide.LONG else Decimal("-1"))
        score = _norm(abs(imbalance), Decimal("0.8")) * Decimal("0.55") + _norm(signed_momentum, Decimal("30")) * Decimal("0.30") + _norm(abs(features.orderbook_imbalance), Decimal("0.5")) * Decimal("0.15")
        return self._candidate(features, side, score, _clamp(score), abs(imbalance) * Decimal("35") + max(signed_momentum, Decimal("0")), "liquidation continuation with liquidity confirmation")


MEME_SYMBOLS = {Symbol.PEPEUSDT, Symbol.SHIBUSDT, Symbol.WIFUSDT, Symbol.BONKUSDT, Symbol.FLOKIUSDT}


class MemeTrendStrategy(_BaseStrategy):
    name = StrategyName.MEME_TREND
    ttl_seconds = 45
    max_holding_seconds = 1800
    reward_multiple = Decimal("1.3")
    trailing_stop_pct = Decimal("0.25")

    def __init__(self, settings: Settings, enabled: bool, threshold: Decimal | None = None) -> None:
        super().__init__(settings, enabled, threshold or Decimal(str(settings.v2_meme_strategy_threshold)))

    def evaluate(self, features: MarketFeatureSnapshot, **context: object) -> V2SignalCandidate:
        trend = context.get("meme")
        trend_score = trend.external_trend_score if isinstance(trend, MemeTrendContext) else Decimal("0")
        momentum = features.price_momentum.get("1m", Decimal("0"))
        side = StrategySide.LONG if momentum + trend_score * Decimal("20") >= 0 else StrategySide.SHORT
        signed = Decimal("1") if side == StrategySide.LONG else Decimal("-1")
        parabolic = abs(features.price_momentum.get("5m", Decimal("0"))) > Decimal("300")
        score = _norm(signed * momentum, Decimal("50")) * Decimal("0.30") + _norm(features.volume_acceleration.get("1m", Decimal("0")) - 1, Decimal("2")) * Decimal("0.25") + _norm(signed * features.relative_strength_vs_btc_bps, Decimal("60")) * Decimal("0.20") + _norm(signed * features.orderbook_imbalance, Decimal("0.5")) * Decimal("0.15") + _clamp(trend_score) * Decimal("0.10")
        if features.symbol not in MEME_SYMBOLS or parabolic:
            score = Decimal("0")
        return self._candidate(features, side, score, _clamp(score), max(Decimal("0"), signed * momentum), "meme trend, relative strength and liquidity", ttl_seconds=self.ttl_seconds)


def build_v2_strategies(settings: Settings) -> tuple[_BaseStrategy, ...]:
    return (
        NewsMomentumStrategyV2(settings, settings.v2_news_momentum_enabled),
        VolumeBreakoutStrategy(settings, settings.v2_volume_breakout_enabled),
        OIFundingSqueezeStrategy(settings, settings.v2_oi_funding_squeeze_enabled),
        LiquidationMomentumStrategy(settings, settings.v2_liquidation_momentum_enabled),
        MemeTrendStrategy(settings, settings.v2_meme_trend_enabled),
    )


def _norm(value: Decimal, scale: Decimal) -> Decimal:
    return _clamp(Decimal("0.5") + value / (scale * Decimal("2")))


def _clamp(value: Decimal) -> Decimal:
    return max(Decimal("0"), min(Decimal("1"), value))
