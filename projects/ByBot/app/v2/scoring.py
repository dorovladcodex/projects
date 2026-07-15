from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal

from app.config import Settings
from app.v2.models import MarketFeatureSnapshot, ScoreComponents, V2SignalCandidate


@dataclass(frozen=True)
class AdmissionContext:
    correlation_penalty: Decimal = Decimal("0")
    portfolio_exposure_penalty: Decimal = Decimal("0")


class CommonScoringPipeline:
    def __init__(self, settings: Settings) -> None:
        self.settings = settings

    def score(
        self,
        strategy_score: Decimal,
        features: MarketFeatureSnapshot,
        context: AdmissionContext = AdmissionContext(),
    ) -> ScoreComponents:
        liquidity = self._liquidity_score(features)
        confirmation = self._confirmation_score(features)
        relative = _clamp01(
            Decimal("0.5") + features.relative_strength_vs_btc_bps / Decimal("200")
        )
        fee_penalty = self.settings.v2_taker_fee_bps * Decimal("2") / Decimal("100")
        slippage_penalty = self.settings.v2_slippage_bps * Decimal("2") / Decimal("100")
        final = (
            strategy_score * Decimal("0.45")
            + liquidity * Decimal("0.20")
            + confirmation * Decimal("0.20")
            + relative * Decimal("0.15")
            - fee_penalty
            - slippage_penalty
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
            final_score=_clamp01(final),
        )

    def admit(
        self, candidate: V2SignalCandidate, *, symbol_valid: bool,
        portfolio_reasons: list[str], context: AdmissionContext = AdmissionContext(),
    ) -> V2SignalCandidate:
        components = self.score(candidate.raw_strategy_score, candidate.feature_snapshot, context)
        candidate.score_components = components
        candidate.distance_to_threshold = components.final_score - candidate.threshold
        reasons = list(portfolio_reasons)
        if not symbol_valid:
            reasons.append("symbol is outside the validated universe")
        if not candidate.feature_snapshot.fresh:
            reasons.extend(candidate.feature_snapshot.stale_reasons or ["mandatory market data is stale"])
        if components.final_score < candidate.threshold:
            reasons.append("final score below strategy threshold")
        costs = candidate.expected_fees_bps + candidate.expected_slippage_bps
        if candidate.estimated_edge_bps <= costs + self.settings.v2_min_expected_edge_bps:
            reasons.append("expected edge does not exceed costs and safety margin")
        candidate.admitted = not reasons
        candidate.state = "READY" if candidate.admitted else "REJECTED"
        candidate.rejection_reason = "; ".join(dict.fromkeys(reasons)) or None
        return candidate

    def _liquidity_score(self, features: MarketFeatureSnapshot) -> Decimal:
        spread = _clamp01(Decimal("1") - features.spread_bps / self.settings.v2_max_spread_bps)
        depth = min(features.bid_depth_usdt, features.ask_depth_usdt)
        depth_score = _clamp01(
            depth / max(self.settings.v2_min_orderbook_depth_usdt, Decimal("1"))
        )
        return (spread + depth_score) / Decimal("2")

    @staticmethod
    def _confirmation_score(features: MarketFeatureSnapshot) -> Decimal:
        momentum = abs(features.price_momentum.get("1m", Decimal("0")))
        imbalance = abs(features.trade_imbalance.get("1m", Decimal("0")))
        return _clamp01(momentum / Decimal("50") + imbalance / Decimal("2"))


def _clamp01(value: Decimal) -> Decimal:
    return min(Decimal("1"), max(Decimal("0"), value))
