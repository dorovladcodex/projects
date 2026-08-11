from __future__ import annotations

from collections import Counter, defaultdict
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from threading import RLock
from typing import Any

from app.v2.models import MarketFeatureSnapshot
from app.v4.models import V4Opportunity
from app.v4.research import (
    HORIZONS_SECONDS,
    build_forward_label,
    build_opportunity,
    cost_components,
)


class V4ShadowCollector:
    """Non-executing V4 opportunity/label collector.

    The class intentionally has no portfolio, admission, execution, exchange or
    risk-service dependency.  Its only side effect is additive research
    persistence through ``save_v4_*`` repository methods.
    """

    def __init__(
        self, settings: Any, repository: Any, *, run_id: str,
        cadence_seconds: int | None = None,
    ) -> None:
        if not bool(settings.v4_alpha_shadow_only):
            raise ValueError("V4 collector requires shadow-only mode")
        self.settings = settings
        self.repository = repository
        self.run_id = run_id
        self.cadence_seconds = cadence_seconds or settings.v4_opportunity_cadence_seconds
        self._last_opportunity_at: dict[str, datetime] = {}
        self._started_at = datetime.now(timezone.utc)
        self._pending: dict[str, V4Opportunity] = {}
        self._paths: dict[str, list[tuple[datetime, Decimal]]] = defaultdict(list)
        self._last_partial_horizon: dict[str, int] = {}
        self._lock = RLock()
        self._metrics: Counter[str] = Counter()
        self._symbols: Counter[str] = Counter()
        self._regimes: Counter[str] = Counter()

    def observe(
        self, feature: MarketFeatureSnapshot, *, cycle_id: str,
    ) -> V4Opportunity | None:
        """Capture one scheduled tape row and advance pending market labels."""
        with self._lock:
            self._advance_paths(feature)
            last = self._last_opportunity_at.get(feature.symbol.value)
            if last is not None and (
                feature.timestamp - last
            ).total_seconds() < self.cadence_seconds:
                return None
            opportunity = build_opportunity(
                feature, run_id=self.run_id, cycle_id=cycle_id,
                source="V4_SHADOW_RUNTIME",
            )
            self._last_opportunity_at[feature.symbol.value] = feature.timestamp
            self._metrics["opportunities"] += 1
            self._metrics[
                "shadow_selected" if opportunity.decision.value == "SHADOW_TRADE"
                else "rejected"
            ] += 1
            self._symbols[opportunity.symbol] += 1
            self._regimes[str(opportunity.features.get("market_regime") or "UNKNOWN")] += 1
            saver = getattr(self.repository, "save_v4_opportunity", None)
            if callable(saver) and saver(opportunity) is not False:
                self._metrics["opportunities_persisted"] += 1
            else:
                self._metrics["persistence_warnings"] += 1
            key = str(opportunity.opportunity_id)
            self._pending[key] = opportunity
            self._paths[key] = []
            return opportunity

    def _advance_paths(self, feature: MarketFeatureSnapshot) -> None:
        completed: list[str] = []
        for key, opportunity in tuple(self._pending.items()):
            if opportunity.symbol != feature.symbol.value:
                continue
            if feature.timestamp <= opportunity.decision_time:
                continue
            if feature.timestamp <= opportunity.decision_time + timedelta(seconds=900):
                self._paths[key].append((feature.timestamp, feature.last_price))
            age = int((feature.timestamp - opportunity.decision_time).total_seconds())
            matured = max((value for value in HORIZONS_SECONDS if age >= value), default=0)
            if matured <= self._last_partial_horizon.get(key, 0):
                continue
            label = build_forward_label(
                opportunity,
                self._paths[key],
                generated_at=feature.timestamp,
                components=cost_components(
                    maker_taker_fees_bps=(
                        self.settings.v2_taker_fee_bps * Decimal("2")
                    ),
                    spread_bps=Decimal(str(
                        opportunity.features.get("spread_bps") or 0
                    )),
                    estimated_slippage_bps=Decimal(str(
                        opportunity.features.get("estimated_market_slippage_bps") or 0
                    )),
                ),
            )
            saver = getattr(self.repository, "save_v4_forward_label", None)
            if callable(saver) and saver(label) is not False:
                self._metrics["label_updates_persisted"] += 1
            else:
                self._metrics["persistence_warnings"] += 1
            self._last_partial_horizon[key] = matured
            if age >= 900:
                completed.append(key)
                self._metrics["labeled_opportunities"] += 1
        for key in completed:
            self._pending.pop(key, None)
            self._paths.pop(key, None)
            self._last_partial_horizon.pop(key, None)

    def status(self) -> dict[str, Any]:
        with self._lock:
            opportunities = self._metrics["opportunities"]
            elapsed_hours = max(
                Decimal("0.000001"),
                Decimal(str((
                    datetime.now(timezone.utc) - self._started_at
                ).total_seconds())) / Decimal("3600"),
            )
            labeled = self._metrics["labeled_opportunities"]
            return {
                "enabled": True,
                "shadow_only": True,
                "opportunities": opportunities,
                "rejected": self._metrics["rejected"],
                "shadow_selected": self._metrics["shadow_selected"],
                "labeled_opportunities": labeled,
                "pending_labels": len(self._pending),
                "opportunities_per_hour": str(Decimal(opportunities) / elapsed_hours),
                "label_coverage_pct": str(
                    Decimal(labeled) / Decimal(opportunities) * Decimal("100")
                    if opportunities else ZERO
                ),
                "persistence_warnings": self._metrics["persistence_warnings"],
                "symbol_distribution": dict(self._symbols),
                "regime_distribution": dict(self._regimes),
                "exchange_mutations": 0,
                "capacity_mutations": 0,
                "production_risk_mutations": 0,
            }


ZERO = Decimal("0")
