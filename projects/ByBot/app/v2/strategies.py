from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from typing import Protocol

from app.config import Settings
from app.models import Symbol
from app.v2.models import (
    MarketFeatureSnapshot,
    StrategyName,
    StrategySide,
    V2SignalCandidate,
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
    available: bool = True
    observed_at: datetime | None = None


class V2Strategy(Protocol):
    name: StrategyName
    version: str
    enabled: bool

    def evaluate(
        self, features: MarketFeatureSnapshot, **context: object
    ) -> V2SignalCandidate: ...


class _BaseStrategy:
    name: StrategyName
    version = "2.1.0"
    ttl_seconds = 180
    max_holding_seconds = 3600
    stop_risk_multiplier = Decimal("1.2")
    reward_multiple = Decimal("1.6")
    trailing_stop_pct: Decimal | None = None

    def __init__(
        self, settings: Settings, enabled: bool, threshold: Decimal | None = None
    ) -> None:
        self.settings = settings
        self.enabled = enabled
        self.threshold = threshold or Decimal(
            str(settings.v2_strategy_default_threshold)
        )

    def applies_to(self, symbol: Symbol) -> bool:
        return self.settings.v2_strategy_applies_to_symbol(
            self.name.value, symbol.value
        )

    def _candidate(
        self,
        features: MarketFeatureSnapshot,
        side: StrategySide,
        score: Decimal,
        confidence: Decimal,
        edge_proxy_bps: Decimal,
        reason: str,
        *,
        setup_rejection_reasons: tuple[str, ...] = (),
        news_ids: tuple[str, ...] = (),
        ttl_seconds: int | None = None,
    ) -> V2SignalCandidate:
        now = datetime.now(timezone.utc)
        atr_pct = max(features.atr_bps / Decimal("100"), Decimal("0.20"))
        stop = min(Decimal("3"), atr_pct * self.stop_risk_multiplier)
        take_profit = stop * self.reward_multiple
        maximum_attainable_edge = take_profit * Decimal("100")
        edge = min(
            max(Decimal("0"), edge_proxy_bps),
            maximum_attainable_edge,
            self.settings.v2_max_empirical_edge_bps,
        )
        target_notional = self.settings.v2_target_notional_for_symbol(
            features.symbol.value
        )
        executable_depth = min(
            features.bid_depth_10bps_usdt or features.bid_depth_usdt,
            features.ask_depth_10bps_usdt or features.ask_depth_usdt,
        )
        impact_bps = (
            min(Decimal("25"), target_notional / executable_depth * Decimal("10000"))
            if executable_depth > 0
            else Decimal("25")
        )
        round_trip_slippage = max(
            self.settings.v2_slippage_bps * Decimal("2"),
            features.spread_bps + impact_bps,
        )
        direction = Decimal("1") if side == StrategySide.LONG else Decimal("-1")
        funding_cost = max(
            Decimal("0"),
            direction * (features.funding_rate or Decimal("0")) * Decimal("10000")
            * Decimal(self.max_holding_seconds) / Decimal("28800"),
        )
        return V2SignalCandidate(
            run_id="unassigned",
            strategy_name=self.name,
            strategy_version=self.version,
            symbol=features.symbol,
            side=side,
            created_at=now,
            expires_at=now + timedelta(seconds=ttl_seconds or self.ttl_seconds),
            market_regime=features.market_regime,
            feature_snapshot=features,
            raw_strategy_score=_clamp(score),
            confidence=_clamp(confidence),
            estimated_edge_bps=edge,
            edge_proxy_bps=edge_proxy_bps,
            edge_calibrated=False,
            expected_fees_bps=self.settings.v2_taker_fee_bps * Decimal("2"),
            expected_slippage_bps=round_trip_slippage,
            expected_funding_bps=funding_cost,
            entry_reason=reason,
            setup_valid=not setup_rejection_reasons,
            setup_rejection_reasons=list(dict.fromkeys(setup_rejection_reasons)),
            threshold=self.threshold,
            distance_to_threshold=_clamp(score) - self.threshold,
            news_ids=list(news_ids),
            stop_loss_pct=stop,
            take_profit_pct=take_profit,
            trailing_stop_pct=self.trailing_stop_pct,
            break_even_at_r=Decimal("1"),
            maximum_holding_seconds=self.max_holding_seconds,
        )

    def _regime_reasons(
        self, features: MarketFeatureSnapshot, side: StrategySide
    ) -> list[str]:
        if not self.settings.v2_regime_routing_enabled:
            return []
        regime = features.market_regime
        if regime == "RANGE":
            return ["momentum strategy is disabled in RANGE regime"]
        if regime == "TRENDING_UP" and side == StrategySide.SHORT:
            return ["market regime conflicts with SHORT direction"]
        if regime == "TRENDING_DOWN" and side == StrategySide.LONG:
            return ["market regime conflicts with LONG direction"]
        if regime == "UNKNOWN":
            return ["market regime is unavailable"]
        return []

    def _history_reasons(
        self, features: MarketFeatureSnapshot, windows: tuple[str, ...]
    ) -> list[str]:
        if not features.observation_count:
            return []  # Compatibility for deterministic legacy fixtures.
        reasons: list[str] = []
        required_coverage = self.settings.v2_min_feature_coverage_pct / Decimal("100")
        seconds_by_window = {"30s": 30, "1m": 60, "3m": 180, "5m": 300}
        for window in windows:
            if features.observation_count.get(window, 0) < self.settings.v2_min_feature_observations:
                reasons.append(f"insufficient observations for {window}")
            required = Decimal(seconds_by_window[window]) * required_coverage
            if features.window_coverage_seconds.get(window, Decimal("0")) < required:
                reasons.append(f"insufficient time coverage for {window}")
        return reasons


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
            return self._candidate(
                features,
                StrategySide.LONG,
                Decimal("0"),
                Decimal("0"),
                Decimal("0"),
                "news context missing",
                setup_rejection_reasons=("news context missing",),
            )
        sentiment = news.sentiment.upper()
        if sentiment not in {"BULLISH", "BEARISH"}:
            return self._candidate(
                features,
                StrategySide.LONG,
                Decimal("0"),
                news.confidence,
                Decimal("0"),
                "neutral news does not define a trade direction",
                setup_rejection_reasons=("neutral news classification",),
                news_ids=news.news_ids,
            )
        side = StrategySide.LONG if sentiment == "BULLISH" else StrategySide.SHORT
        direction = Decimal("1") if side == StrategySide.LONG else Decimal("-1")
        confirmation = direction * features.price_momentum.get("1m", Decimal("0"))
        reasons = self._history_reasons(features, ("1m",))
        if confirmation <= 0:
            reasons.append("market direction does not confirm classified news")
        reasons.extend(self._regime_reasons(features, side))
        score = (
            news.confidence * Decimal("0.45")
            + news.importance * Decimal("0.35")
            + _signed_norm(confirmation, Decimal("40")) * Decimal("0.20")
        )
        edge_proxy = max(Decimal("0"), confirmation) + news.importance * Decimal("30")
        return self._candidate(
            features,
            side,
            score,
            news.confidence,
            edge_proxy,
            "important classified news with directional market confirmation",
            setup_rejection_reasons=tuple(reasons),
            news_ids=news.news_ids,
        )


class VolumeBreakoutStrategy(_BaseStrategy):
    name = StrategyName.VOLUME_BREAKOUT
    ttl_seconds = 90
    max_holding_seconds = 3600
    reward_multiple = Decimal("1.5")
    trailing_stop_pct = Decimal("0.35")

    def evaluate(
        self, features: MarketFeatureSnapshot, **context: object
    ) -> V2SignalCandidate:
        del context
        windows = ("30s", "1m", "3m", "5m")
        values = {key: features.breakout_distance_bps.get(key, Decimal("0")) for key in windows}
        positive = [value for value in values.values() if value > 0]
        negative = [value for value in values.values() if value < 0]
        side = StrategySide.LONG if sum(positive) >= abs(sum(negative)) else StrategySide.SHORT
        direction = Decimal("1") if side == StrategySide.LONG else Decimal("-1")
        aligned = [direction * value for value in values.values() if direction * value > 0]
        breakout = max(aligned, default=Decimal("0"))
        imbalance = direction * features.trade_imbalance.get("1m", Decimal("0"))
        volume = min(
            features.volume_acceleration.get("1m", Decimal("0")),
            self.settings.v2_max_volume_acceleration,
        )
        reasons = self._history_reasons(features, windows)
        if len(aligned) < 2:
            reasons.append("breakout lacks multi-window confirmation")
        if volume <= Decimal("1.2"):
            reasons.append("volume acceleration is insufficient")
        if imbalance <= 0:
            reasons.append("trade imbalance conflicts with breakout")
        reasons.extend(self._regime_reasons(features, side))
        overextended = abs(
            features.distance_from_low_bps
            if side == StrategySide.LONG
            else features.distance_from_high_bps
        ) > Decimal("350")
        if overextended:
            reasons.append("breakout is already overextended")
        score = (
            _magnitude_norm(breakout, Decimal("35")) * Decimal("0.40")
            + _magnitude_norm(volume - Decimal("1"), Decimal("2")) * Decimal("0.30")
            + _signed_norm(imbalance, Decimal("0.5")) * Decimal("0.30")
        )
        edge_proxy = breakout + _magnitude_norm(
            volume - Decimal("1"), Decimal("2")
        ) * Decimal("24")
        return self._candidate(
            features,
            side,
            score,
            _clamp(score),
            edge_proxy,
            "multi-window volume breakout",
            setup_rejection_reasons=tuple(reasons),
        )


class OIFundingSqueezeStrategy(_BaseStrategy):
    name = StrategyName.OI_FUNDING_SQUEEZE
    max_holding_seconds = 7200
    reward_multiple = Decimal("2")
    trailing_stop_pct = Decimal("0.5")

    def evaluate(
        self, features: MarketFeatureSnapshot, **context: object
    ) -> V2SignalCandidate:
        del context
        price = features.price_momentum.get("5m", Decimal("0"))
        side = StrategySide.LONG if price >= 0 else StrategySide.SHORT
        direction = Decimal("1") if side == StrategySide.LONG else Decimal("-1")
        reasons = self._history_reasons(features, ("1m", "5m"))
        if features.open_interest_change_pct is None:
            reasons.append("open interest history is unavailable")
        if features.funding_deviation_bps is None:
            reasons.append("funding data is unavailable")
        # Production snapshots always expose source timestamps. Legacy typed
        # fixtures without source-age metadata remain deterministic; the value
        # presence checks above still apply.
        if features.source_timestamps or features.source_age_seconds:
            for source in ("funding", "open_interest"):
                age = features.source_age_seconds.get(source)
                if age is None or age > self.settings.v2_rest_data_stale_seconds:
                    reasons.append(f"{source} data is stale or unavailable")
        oi = features.open_interest_change_pct or Decimal("0")
        funding = features.funding_deviation_bps or Decimal("0")
        if price == 0 or oi == 0:
            reasons.append("price/open-interest impulse is absent")
        reasons.extend(self._regime_reasons(features, side))
        crowding = max(Decimal("0"), -direction * funding)
        directional_imbalance = direction * features.trade_imbalance.get("1m", Decimal("0"))
        score = (
            _magnitude_norm(abs(price), Decimal("50")) * Decimal("0.35")
            + _magnitude_norm(abs(oi), Decimal("3")) * Decimal("0.30")
            + _magnitude_norm(crowding, Decimal("5")) * Decimal("0.20")
            + _signed_norm(directional_imbalance, Decimal("0.5")) * Decimal("0.15")
        )
        if price > 0 and oi > 0:
            label = "new long participation"
        elif price > 0 and oi < 0:
            label = "short covering"
        elif price < 0 and oi < 0:
            label = "long unwinding"
        else:
            label = "new short participation"
        edge_proxy = abs(price) + min(abs(oi), Decimal("10")) * Decimal("5")
        return self._candidate(
            features,
            side,
            score,
            _clamp(score),
            edge_proxy,
            label,
            setup_rejection_reasons=tuple(reasons),
        )


class LiquidationMomentumStrategy(_BaseStrategy):
    name = StrategyName.LIQUIDATION_MOMENTUM
    ttl_seconds = 60
    max_holding_seconds = 2400
    reward_multiple = Decimal("1.5")
    trailing_stop_pct = Decimal("0.35")

    def evaluate(
        self, features: MarketFeatureSnapshot, **context: object
    ) -> V2SignalCandidate:
        del context
        imbalance = features.liquidation_imbalance
        side = StrategySide.LONG if imbalance > 0 else StrategySide.SHORT
        direction = Decimal("1") if side == StrategySide.LONG else Decimal("-1")
        signed_momentum = direction * features.price_momentum.get("30s", Decimal("0"))
        directional_book = direction * features.orderbook_imbalance
        reasons = self._history_reasons(features, ("30s", "1m"))
        if features.liquidation_event_count_5m < self.settings.v2_min_liquidation_events:
            reasons.append("no recent symbol-specific liquidation event")
        if features.liquidation_notional_5m < self.settings.v2_min_liquidation_notional_usdt:
            reasons.append("liquidation notional is below minimum intensity")
        if (
            features.liquidation_data_age_seconds is None
            or features.liquidation_data_age_seconds > self.settings.v2_liquidation_stale_seconds
        ):
            reasons.append("liquidation event is stale or unavailable")
        if imbalance == 0:
            reasons.append("liquidation imbalance is absent")
        if signed_momentum <= 0:
            reasons.append("price does not confirm liquidation continuation")
        reasons.extend(self._regime_reasons(features, side))
        intensity = min(
            Decimal("1"),
            features.liquidation_notional_5m
            / max(features.volume_24h / Decimal("288"), Decimal("1")),
        )
        score = (
            _magnitude_norm(abs(imbalance), Decimal("0.8")) * Decimal("0.40")
            + _magnitude_norm(intensity, Decimal("0.05")) * Decimal("0.20")
            + _signed_norm(signed_momentum, Decimal("30")) * Decimal("0.25")
            + _signed_norm(directional_book, Decimal("0.5")) * Decimal("0.15")
        )
        edge_proxy = min(abs(imbalance), Decimal("1")) * Decimal("35") + max(
            signed_momentum, Decimal("0")
        )
        return self._candidate(
            features,
            side,
            score,
            _clamp(score),
            edge_proxy,
            "liquidation continuation with symbol-specific intensity",
            setup_rejection_reasons=tuple(reasons),
        )


MEME_SYMBOLS = {
    Symbol.DOGEUSDT,
    Symbol.PEPEUSDT,
    Symbol.SHIBUSDT,
    Symbol.WIFUSDT,
    Symbol.BONKUSDT,
    Symbol.FLOKIUSDT,
}


class MemeTrendStrategy(_BaseStrategy):
    name = StrategyName.MEME_TREND
    ttl_seconds = 45
    max_holding_seconds = 1800
    reward_multiple = Decimal("1.3")
    trailing_stop_pct = Decimal("0.25")

    def __init__(
        self, settings: Settings, enabled: bool, threshold: Decimal | None = None
    ) -> None:
        super().__init__(
            settings,
            enabled,
            threshold or Decimal(str(settings.v2_meme_strategy_threshold)),
        )

    def evaluate(
        self, features: MarketFeatureSnapshot, **context: object
    ) -> V2SignalCandidate:
        trend = context.get("meme")
        trend_score = (
            trend.external_trend_score
            if isinstance(trend, MemeTrendContext)
            else Decimal("0")
        )
        momentum = features.price_momentum.get("1m", Decimal("0"))
        side = (
            StrategySide.LONG
            if momentum + trend_score * Decimal("20") >= 0
            else StrategySide.SHORT
        )
        direction = Decimal("1") if side == StrategySide.LONG else Decimal("-1")
        parabolic = abs(features.price_momentum.get("5m", Decimal("0"))) > Decimal("300")
        reasons = self._history_reasons(features, ("1m", "5m"))
        if not self.applies_to(features.symbol) or features.symbol not in MEME_SYMBOLS:
            reasons.append("symbol is outside meme strategy universe")
        if not isinstance(trend, MemeTrendContext) or not trend.available:
            reasons.append("external meme trend data is unavailable")
        if parabolic:
            reasons.append("meme move is already parabolic")
        if direction * momentum <= 0:
            reasons.append("Bybit momentum does not confirm external trend")
        reasons.extend(self._regime_reasons(features, side))
        volume = min(
            features.volume_acceleration.get("1m", Decimal("0")),
            self.settings.v2_max_volume_acceleration,
        )
        score = (
            _signed_norm(direction * momentum, Decimal("50")) * Decimal("0.30")
            + _magnitude_norm(volume - Decimal("1"), Decimal("2")) * Decimal("0.25")
            + _signed_norm(
                direction * features.relative_strength_vs_btc_bps, Decimal("60")
            ) * Decimal("0.20")
            + _signed_norm(
                direction * features.orderbook_imbalance, Decimal("0.5")
            ) * Decimal("0.15")
            + _clamp(direction * trend_score) * Decimal("0.10")
        )
        return self._candidate(
            features,
            side,
            score,
            _clamp(score),
            max(Decimal("0"), direction * momentum),
            "meme trend, relative strength and liquidity",
            setup_rejection_reasons=tuple(reasons),
            ttl_seconds=self.ttl_seconds,
        )


class RangeMeanReversionStrategy(_BaseStrategy):
    """Isolated RANGE-only challenger; disabled by default."""

    name = StrategyName.RANGE_MEAN_REVERSION
    ttl_seconds = 60
    max_holding_seconds = 1800
    reward_multiple = Decimal("1.25")
    stop_risk_multiplier = Decimal("1")

    def evaluate(
        self, features: MarketFeatureSnapshot, **context: object
    ) -> V2SignalCandidate:
        del context
        from_high = features.distance_from_high_bps
        from_low = features.distance_from_low_bps
        side = (
            StrategySide.LONG
            if features.market_regime != "RANGE"
            else StrategySide.LONG
            if abs(from_low) < abs(from_high)
            else StrategySide.SHORT
        )
        direction = Decimal("1") if side == StrategySide.LONG else Decimal("-1")
        displacement = abs(from_high if side == StrategySide.SHORT else from_low)
        reversal_flow = direction * (
            features.trade_imbalance.get("30s", Decimal("0"))
            + features.orderbook_imbalance
        ) / Decimal("2")
        reasons = self._history_reasons(features, ("30s", "1m"))
        if features.market_regime != "RANGE":
            reasons.append("mean reversion requires RANGE regime")
        if displacement < Decimal("20"):
            reasons.append("price is not displaced from the local range")
        if reversal_flow <= 0:
            reasons.append("order flow does not confirm mean reversion")
        score = (
            _magnitude_norm(displacement, Decimal("80")) * Decimal("0.55")
            + _signed_norm(reversal_flow, Decimal("0.5")) * Decimal("0.45")
        )
        return self._candidate(
            features,
            side,
            score,
            _clamp(score),
            min(displacement, Decimal("80")),
            "range displacement with order-flow reversal",
            setup_rejection_reasons=tuple(reasons),
        )


def build_v2_strategies(settings: Settings) -> tuple[_BaseStrategy, ...]:
    return (
        NewsMomentumStrategyV2(settings, settings.v2_news_momentum_enabled),
        VolumeBreakoutStrategy(settings, settings.v2_volume_breakout_enabled),
        OIFundingSqueezeStrategy(settings, settings.v2_oi_funding_squeeze_enabled),
        LiquidationMomentumStrategy(settings, settings.v2_liquidation_momentum_enabled),
        MemeTrendStrategy(settings, settings.v2_meme_trend_enabled),
        RangeMeanReversionStrategy(settings, settings.v2_range_mean_reversion_enabled),
    )


def _signed_norm(value: Decimal, scale: Decimal) -> Decimal:
    return _clamp(Decimal("0.5") + value / (max(scale, Decimal("0.000001")) * 2))


def _magnitude_norm(value: Decimal, scale: Decimal) -> Decimal:
    """Magnitude normalizer: zero evidence contributes exactly zero."""
    return _clamp(max(Decimal("0"), value) / max(scale, Decimal("0.000001")))


def _clamp(value: Decimal) -> Decimal:
    return max(Decimal("0"), min(Decimal("1"), value))
