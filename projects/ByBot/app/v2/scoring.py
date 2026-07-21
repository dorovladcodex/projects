from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal

from app.config import Settings
from app.v2.models import (
    MarketFeatureSnapshot,
    ScoreComponents,
    StrategyName,
    StrategySide,
    V2SignalCandidate,
)


@dataclass(frozen=True)
class AdmissionContext:
    correlation_penalty: Decimal = Decimal("0")
    portfolio_exposure_penalty: Decimal = Decimal("0")


class CommonScoringPipeline:
    """Side-aware, cost-aware scoring with explicit abstention conditions."""

    def __init__(self, settings: Settings) -> None:
        self.settings = settings

    def score(
        self,
        strategy_score: Decimal,
        features: MarketFeatureSnapshot,
        context: AdmissionContext = AdmissionContext(),
        *,
        side: StrategySide = StrategySide.LONG,
        strategy_name: StrategyName | None = None,
    ) -> ScoreComponents:
        liquidity = self._liquidity_score(features)
        confirmation = self._confirmation_score(features, side)
        direction = Decimal("1") if side == StrategySide.LONG else Decimal("-1")
        relative = _signed_score(
            direction * features.relative_strength_vs_btc_bps, Decimal("100")
        )
        regime = self._regime_score(features.market_regime, side, strategy_name)
        uncertainty = self._uncertainty_penalty(features)
        # Costs are also a hard admission gate. Their score contribution uses a
        # bounded economic scale instead of treating 18 bps as an arbitrary 0.18.
        scale = max(self.settings.v2_max_empirical_edge_bps, Decimal("1"))
        fee_penalty = self.settings.v2_taker_fee_bps * Decimal("2") / scale
        slippage_penalty = self.settings.v2_slippage_bps * Decimal("2") / scale
        final = (
            strategy_score * Decimal("0.40")
            + liquidity * Decimal("0.18")
            + confirmation * Decimal("0.20")
            + relative * Decimal("0.10")
            + regime * Decimal("0.12")
            - fee_penalty
            - slippage_penalty
            - uncertainty
            - context.correlation_penalty
            - context.portfolio_exposure_penalty
        )
        return ScoreComponents(
            strategy_score=strategy_score,
            liquidity_score=liquidity,
            market_confirmation_score=confirmation,
            relative_strength_score=relative,
            estimated_fee_penalty=fee_penalty,
            estimated_slippage_penalty=slippage_penalty,
            correlation_penalty=context.correlation_penalty,
            portfolio_exposure_penalty=context.portfolio_exposure_penalty,
            regime_score=regime,
            uncertainty_penalty=uncertainty,
            final_score=_clamp01(final),
        )

    def admit(
        self, candidate: V2SignalCandidate, *, symbol_valid: bool,
        portfolio_reasons: list[str], context: AdmissionContext = AdmissionContext(),
    ) -> V2SignalCandidate:
        candidate.threshold = self._adaptive_threshold(candidate)
        components = self.score(
            candidate.raw_strategy_score,
            candidate.feature_snapshot,
            context,
            side=candidate.side,
            strategy_name=candidate.strategy_name,
        )
        candidate.score_components = components
        candidate.distance_to_threshold = components.final_score - candidate.threshold
        reasons = list(candidate.setup_rejection_reasons) + list(portfolio_reasons)
        if not candidate.setup_valid and not candidate.setup_rejection_reasons:
            reasons.append("strategy setup is not valid")
        if not symbol_valid:
            reasons.append("symbol is outside the validated universe")
        if not candidate.feature_snapshot.fresh:
            reasons.extend(
                candidate.feature_snapshot.stale_reasons
                or ["mandatory market data is stale"]
            )
        if components.final_score < candidate.threshold:
            reasons.append("final score below strategy threshold")
        costs = (
            candidate.expected_fees_bps
            + candidate.expected_slippage_bps
            + candidate.expected_funding_bps
        )
        if candidate.estimated_edge_bps <= costs + self.settings.v2_min_expected_edge_bps:
            reasons.append("expected edge does not exceed costs and safety margin")
        if candidate.meta_label_status == "READY" and (
            candidate.meta_label_probability is None
            or candidate.meta_label_probability < self.settings.v2_meta_label_min_probability
        ):
            reasons.append("meta-label probability is below threshold")
        candidate.admitted = not reasons
        candidate.state = "READY" if candidate.admitted else "REJECTED"
        candidate.rejection_reason = "; ".join(dict.fromkeys(reasons)) or None
        return candidate

    def _adaptive_threshold(self, candidate: V2SignalCandidate) -> Decimal:
        if candidate.strategy_name == StrategyName.RANGE_MEAN_REVERSION:
            base = Decimal(str(self.settings.v2_range_strategy_threshold))
        elif candidate.strategy_name == StrategyName.MEME_TREND:
            base = Decimal(str(self.settings.v2_meme_strategy_threshold))
        else:
            base = Decimal(str(self.settings.v2_strategy_default_threshold))
        if candidate.market_regime == "HIGH_VOLATILITY":
            base += self.settings.v2_high_volatility_threshold_addition
        return min(Decimal("1"), base)

    def rank(self, candidates: list[V2SignalCandidate]) -> list[V2SignalCandidate]:
        """Rank admitted opportunities by uncertainty-adjusted net edge."""
        def key(item: V2SignalCandidate) -> tuple[Decimal, Decimal, Decimal]:
            costs = (
                item.expected_fees_bps
                + item.expected_slippage_bps
                + item.expected_funding_bps
            )
            probability = item.meta_label_probability or item.confidence
            return (
                (item.estimated_edge_bps - costs) * probability,
                item.score_components.final_score if item.score_components else Decimal("0"),
                -item.feature_snapshot.spread_bps,
            )

        ranked = sorted((row for row in candidates if row.admitted), key=key, reverse=True)
        for index, candidate in enumerate(ranked, start=1):
            candidate.rank_in_cycle = index
        return ranked

    def _liquidity_score(self, features: MarketFeatureSnapshot) -> Decimal:
        spread = _clamp01(
            Decimal("1") - features.spread_bps / self.settings.v2_max_spread_bps
        )
        depth = min(
            features.bid_depth_10bps_usdt or features.bid_depth_usdt,
            features.ask_depth_10bps_usdt or features.ask_depth_usdt,
        )
        depth_score = _clamp01(
            depth / max(self.settings.v2_min_orderbook_depth_usdt, Decimal("1"))
        )
        return (spread + depth_score) / Decimal("2")

    @staticmethod
    def _confirmation_score(
        features: MarketFeatureSnapshot, side: StrategySide
    ) -> Decimal:
        direction = Decimal("1") if side == StrategySide.LONG else Decimal("-1")
        momentum = direction * features.price_momentum.get("1m", Decimal("0"))
        trade = direction * features.trade_imbalance.get("1m", Decimal("0"))
        book = direction * features.orderbook_imbalance
        ofi = direction * features.order_flow_imbalance.get("30s", Decimal("0"))
        return (
            _signed_score(momentum, Decimal("50")) * Decimal("0.45")
            + _signed_score(trade, Decimal("0.5")) * Decimal("0.25")
            + _signed_score(book, Decimal("0.5")) * Decimal("0.15")
            + _signed_score(ofi, Decimal("1")) * Decimal("0.15")
        )

    @staticmethod
    def _regime_score(
        regime: str, side: StrategySide, strategy_name: StrategyName | None
    ) -> Decimal:
        if strategy_name == StrategyName.RANGE_MEAN_REVERSION:
            return Decimal("1") if regime == "RANGE" else Decimal("0")
        if regime == "HIGH_VOLATILITY":
            return Decimal("0.65")
        if regime == "TRENDING_UP":
            return Decimal("1") if side == StrategySide.LONG else Decimal("0")
        if regime == "TRENDING_DOWN":
            return Decimal("1") if side == StrategySide.SHORT else Decimal("0")
        if regime == "RANGE":
            return Decimal("0.25")
        return Decimal("0")

    def _uncertainty_penalty(self, features: MarketFeatureSnapshot) -> Decimal:
        if not features.observation_count:
            return Decimal("0")
        count = features.observation_count.get("1m", 0)
        coverage = features.window_coverage_seconds.get("1m", Decimal("0"))
        count_ratio = min(
            Decimal("1"),
            Decimal(count) / Decimal(self.settings.v2_min_feature_observations),
        )
        coverage_ratio = min(
            Decimal("1"),
            coverage / Decimal("60")
            / (self.settings.v2_min_feature_coverage_pct / Decimal("100")),
        )
        quality = min(count_ratio, coverage_ratio)
        return (Decimal("1") - quality) * Decimal("0.20")


def _signed_score(value: Decimal, scale: Decimal) -> Decimal:
    return _clamp01(Decimal("0.5") + value / (max(scale, Decimal("0.000001")) * 2))


def _clamp01(value: Decimal) -> Decimal:
    return min(Decimal("1"), max(Decimal("0"), value))
