from __future__ import annotations

import asyncio
from collections import defaultdict, deque
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path
from threading import Lock
from time import perf_counter
from typing import Any, Callable, Sequence

from app.config import Settings
from app.microstructure.calculations import (
    TOUCH_HORIZONS_SECONDS,
    build_carry_candidate,
    evaluate_hypothetical_touch,
    hypothetical_quotes,
    stable_id,
    synchronize_snapshot,
)
from app.microstructure.models import CoverageState, FundingEventRecord
from app.microstructure.public import (
    BybitPublicReadOnlyClient,
    MicrostructureMarketState,
    PublicWebSocketPump,
    bootstrap_market_state,
    refresh_market_state,
    select_liquid_spot_perp_universe,
)
from app.microstructure.storage import MicrostructureStorage, write_json_atomic
from app.v5.models import CarryLabel, CarryOpportunity, DataAvailability, FundingPayment
from app.v5.research import CarryPathPoint, build_carry_label


FUTURE_LABEL_HORIZONS: tuple[tuple[str, timedelta | int], ...] = (
    ("12h", timedelta(hours=12)),
    ("24h", timedelta(hours=24)),
    ("48h", timedelta(hours=48)),
    ("72h", timedelta(hours=72)),
    ("1_funding_interval", 1),
    ("2_funding_intervals", 2),
    ("3_funding_intervals", 3),
    ("6_funding_intervals", 6),
)
MAKER_BATCH_SIZE = 1_000
LABEL_BATCH_SIZE = 256


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _decimal(value: object) -> Decimal | None:
    if value in (None, ""):
        return None
    try:
        result = Decimal(str(value))
        return result if result.is_finite() else None
    except (ArithmeticError, ValueError):
        return None


def _timestamp_ms(value: object) -> datetime | None:
    if value in (None, "", 0, "0"):
        return None
    try:
        return datetime.fromtimestamp(int(str(value)) / 1000, tz=timezone.utc)
    except (ValueError, OverflowError):
        return None


@dataclass(frozen=True)
class CollectorConfiguration:
    artifact_dir: Path
    capture_cadence_seconds: int
    rest_refresh_seconds: int
    funding_refresh_seconds: int
    report_refresh_seconds: int
    universe_size: int
    minimum_leg_turnover_usdt: Decimal
    maximum_selection_spread_bps: Decimal
    maximum_source_age_ms: Decimal
    maximum_sync_gap_ms: Decimal
    notionals_usdt: tuple[Decimal, ...]
    spot_ws_url: str
    linear_ws_url: str
    public_rest_url: str
    candidate_symbols: tuple[str, ...]
    account_fees_bps: dict[str, Decimal | None]
    fee_source: str | None
    fee_schedule: str | None
    offline_fee_scenarios: dict[str, Any]

    @classmethod
    def from_settings(cls, settings: Settings, root: Path) -> "CollectorConfiguration":
        artifact = Path(settings.microstructure_artifact_directory)
        if not artifact.is_absolute():
            artifact = root / artifact
        return cls(
            artifact_dir=artifact.resolve(),
            capture_cadence_seconds=settings.microstructure_capture_cadence_seconds,
            rest_refresh_seconds=settings.microstructure_rest_refresh_seconds,
            funding_refresh_seconds=settings.microstructure_funding_refresh_seconds,
            report_refresh_seconds=settings.microstructure_report_refresh_seconds,
            universe_size=settings.microstructure_universe_size,
            minimum_leg_turnover_usdt=(
                settings.microstructure_min_leg_turnover_24h_usdt
            ),
            maximum_selection_spread_bps=(
                settings.microstructure_max_selection_spread_bps
            ),
            maximum_source_age_ms=settings.microstructure_max_source_age_ms,
            maximum_sync_gap_ms=settings.microstructure_max_sync_gap_ms,
            notionals_usdt=tuple(settings.microstructure_notionals_usdt),
            spot_ws_url=settings.microstructure_spot_ws_url,
            linear_ws_url=settings.microstructure_linear_ws_url,
            public_rest_url=settings.bybit_public_base_url,
            candidate_symbols=tuple(settings.v2_universe_symbols),
            account_fees_bps={
                "spot_maker": settings.v5_spot_maker_fee_bps,
                "spot_taker": settings.v5_spot_taker_fee_bps,
                "perp_maker": settings.v5_perp_maker_fee_bps,
                "perp_taker": settings.v5_perp_taker_fee_bps,
            },
            fee_source=settings.v5_fee_source,
            fee_schedule=settings.v5_fee_schedule,
            offline_fee_scenarios={
                "MNT_DISCOUNT_SCENARIO": {
                    "enabled_for_offline_research": (
                        settings.v5_mnt_discount_scenario_enabled
                    ),
                    "authoritative_account_rate": False,
                    "fees": {
                        "spot_maker_fee_bps": str(
                            settings.v5_mnt_spot_maker_fee_bps
                        ),
                        "spot_taker_fee_bps": str(
                            settings.v5_mnt_spot_taker_fee_bps
                        ),
                        "perp_maker_fee_bps": str(
                            settings.v5_mnt_perp_maker_fee_bps
                        ),
                        "perp_taker_fee_bps": str(
                            settings.v5_mnt_perp_taker_fee_bps
                        ),
                    },
                }
            },
        )

    def public_payload(self) -> dict[str, Any]:
        return {
            "mode": "READ_ONLY_MARKET_TELEMETRY",
            "capture_cadence_seconds": self.capture_cadence_seconds,
            "rest_refresh_seconds": self.rest_refresh_seconds,
            "funding_refresh_seconds": self.funding_refresh_seconds,
            "report_refresh_seconds": self.report_refresh_seconds,
            "universe_size": self.universe_size,
            "minimum_leg_turnover_24h_usdt": str(self.minimum_leg_turnover_usdt),
            "maximum_selection_spread_bps": str(self.maximum_selection_spread_bps),
            "maximum_source_age_ms": str(self.maximum_source_age_ms),
            "maximum_sync_gap_ms": str(self.maximum_sync_gap_ms),
            "notionals_usdt": [str(value) for value in self.notionals_usdt],
            "spot_ws_url": self.spot_ws_url,
            "linear_ws_url": self.linear_ws_url,
            "public_rest_url": self.public_rest_url,
            "candidate_symbols": list(self.candidate_symbols),
            "exchange_execution_capability": False,
            "portfolio_capacity_access": False,
            "production_risk_mutation": False,
        }

    def cost_payload(self) -> dict[str, Any]:
        values = {
            f"{key}_fee_bps": str(value) if value is not None else "UNKNOWN"
            for key, value in self.account_fees_bps.items()
        }
        return {
            "source": "MANUALLY_CONFIGURABLE_ACCOUNT_SPECIFIC_SETTINGS",
            "fee_source": self.fee_source or "UNCONFIRMED",
            "fee_schedule": self.fee_schedule or "UNCONFIRMED",
            "status": (
                "CONFIGURED"
                if all(value is not None for value in self.account_fees_bps.values())
                else "UNKNOWN"
            ),
            "fees": values,
            "offline_research_scenarios": self.offline_fee_scenarios,
            "public_fee_schedule_substituted": False,
            "legacy_directional_cost_substituted": False,
        }


class StageTimingMetrics:
    """Low-overhead, bounded in-memory stage timing distribution."""

    def __init__(self, max_samples: int = 10_000) -> None:
        self._samples: dict[str, deque[float]] = defaultdict(
            lambda: deque(maxlen=max_samples)
        )
        self._last: dict[str, float] = {}
        self._lock = Lock()

    def record(self, name: str, milliseconds: float) -> None:
        with self._lock:
            value = max(0.0, float(milliseconds))
            self._samples[name].append(value)
            self._last[name] = value

    def snapshot(self) -> dict[str, dict[str, float | int | None]]:
        with self._lock:
            copied = {name: sorted(values) for name, values in self._samples.items()}
            last = dict(self._last)
        return {
            name: {
                "count": len(values),
                "p50": _float_percentile(values, 0.50),
                "p90": _float_percentile(values, 0.90),
                "p95": _float_percentile(values, 0.95),
                "p99": _float_percentile(values, 0.99),
                "max": values[-1] if values else None,
                "last": last.get(name),
            }
            for name, values in copied.items()
        }


def advance_fixed_deadline(deadline: float, current: float, cadence: float) -> float:
    """Advance to the first future slot without replaying missed snapshots."""
    if cadence <= 0:
        raise ValueError("cadence must be positive")
    candidate = deadline + cadence
    if candidate <= current:
        missed = int((current - candidate) // cadence) + 1
        candidate += missed * cadence
    return candidate


async def _wait_until(stop_event: asyncio.Event, deadline: float) -> bool:
    timeout = max(0.0, deadline - asyncio.get_running_loop().time())
    if timeout == 0:
        return not stop_event.is_set()
    try:
        await asyncio.wait_for(stop_event.wait(), timeout=timeout)
        return False
    except asyncio.TimeoutError:
        return True


async def _next_worker_signal(
    queue: asyncio.Queue[None], stop_event: asyncio.Event,
) -> bool:
    while not stop_event.is_set():
        try:
            await asyncio.wait_for(queue.get(), timeout=0.5)
            queue.task_done()
            return True
        except asyncio.TimeoutError:
            continue
    return False


class MicrostructureCollector:
    """Managed public-data collector with no private exchange dependency."""

    def __init__(
        self,
        config: CollectorConfiguration,
        *,
        storage: MicrostructureStorage | None = None,
        client: BybitPublicReadOnlyClient | None = None,
        state: MicrostructureMarketState | None = None,
        now: Callable[[], datetime] = utc_now,
    ) -> None:
        self.config = config
        self.storage = storage or MicrostructureStorage(config.artifact_dir)
        self.client = client or BybitPublicReadOnlyClient(config.public_rest_url)
        self.state = state or MicrostructureMarketState()
        self.now = now
        self.symbols: tuple[str, ...] = ()
        self.clock_offset_ms: Decimal | None = None
        self.clock_round_trip_ms: Decimal | None = None
        self.capture_cycles = 0
        self.last_rest_failures: dict[str, str] = {}
        self.last_funding_failures: dict[str, str] = {}
        self.initialized_at: datetime | None = None
        self._closed = False
        self.timings = StageTimingMetrics()
        self._worker_queues: dict[str, asyncio.Queue[None]] = {}
        self._worker_storages: dict[str, MicrostructureStorage] = {}
        self._worker_stats: dict[str, dict[str, Any]] = {
            "maker": {
                "pending_count": 0, "processed_count": 0,
                "last_processed_count": 0, "coalesced_wakeups": 0,
            },
            "label": {
                "pending_count": 0, "processed_count": 0,
                "last_processed_count": 0, "coalesced_wakeups": 0,
            },
            "maintenance": {
                "pending_count": 0, "processed_count": 0,
                "last_processed_count": 0, "coalesced_wakeups": 0,
            },
        }
        self._last_capture_started_monotonic: float | None = None
        self._last_capture_interval_seconds: float | None = None
        self._last_capture_lateness_seconds = 0.0

    def initialize(self) -> dict[str, Any]:
        self._write_static_artifacts()
        selected = self.storage.selected_universe()
        if not selected:
            decisions = select_liquid_spot_perp_universe(
                self.client,
                candidates=self.config.candidate_symbols,
                size=self.config.universe_size,
                minimum_leg_turnover_usdt=self.config.minimum_leg_turnover_usdt,
                maximum_spread_bps=self.config.maximum_selection_spread_bps,
            )
            self.storage.persist_universe(decisions)
            selected = [row for row in decisions if row["selected"]]
        self.symbols = tuple(str(row["symbol"]) for row in selected)
        bootstrap = bootstrap_market_state(self.client, self.state, self.symbols)
        try:
            self.clock_offset_ms, self.clock_round_trip_ms = self.client.clock_offset_ms()
        except Exception as exc:
            self.last_rest_failures["clock"] = type(exc).__name__
        self.initialized_at = self.now()
        self.storage.set_state("collector_initialized_at", self.initialized_at)
        self.storage.set_state("selected_symbols", self.symbols)
        self.write_reports()
        return {
            "initialized_at": self.initialized_at,
            "symbols": self.symbols,
            "bootstrap": bootstrap,
            "clock_offset_ms": self.clock_offset_ms,
            "clock_round_trip_ms": self.clock_round_trip_ms,
        }

    def capture_once(self, *, completed_at: datetime | None = None) -> dict[str, int]:
        capture_started = perf_counter()
        assembly_ms = 0.0
        persistence_ms = 0.0
        counts = {"captures": 0, "complete": 0, "costs": 0, "quotes": 0}
        at = completed_at or self.now()
        for symbol in self.symbols:
            stage_started = perf_counter()
            spot = self.state.snapshot("spot", symbol)
            perpetual = self.state.snapshot("linear", symbol)
            receipt_times = [
                leg.local_receive_timestamp
                for leg in (spot, perpetual) if leg is not None
            ]
            at = max([completed_at or self.now(), *receipt_times])
            prior = self.storage.last_capture_at(symbol)
            snapshot = synchronize_snapshot(
                symbol=symbol,
                spot=spot,
                perpetual=perpetual,
                completed_at=at,
                clock_offset_ms=self.clock_offset_ms,
                max_source_age_ms=self.config.maximum_source_age_ms,
                max_sync_gap_ms=self.config.maximum_sync_gap_ms,
            )
            assembly_ms += (perf_counter() - stage_started) * 1000
            stage_started = perf_counter()
            if prior is not None:
                gap_seconds = (at - prior).total_seconds()
                if gap_seconds > self.config.capture_cadence_seconds * 3:
                    self.storage.record_gap(
                        gap_id=stable_id("capture-gap", symbol, prior, at),
                        symbol=symbol,
                        prior_at=prior,
                        current_at=at,
                        expected_cadence_seconds=self.config.capture_cadence_seconds,
                    )
            if not self.storage.save_capture(snapshot):
                continue
            counts["captures"] += 1
            counts["complete"] += int(snapshot.complete)
            candidate, costs = build_carry_candidate(
                snapshot,
                notionals=self.config.notionals_usdt,
                account_fees_bps=self.config.account_fees_bps,
                max_alignment_ms=self.config.maximum_sync_gap_ms,
            )
            self.storage.save_carry_candidate(candidate)
            for cost in costs:
                counts["costs"] += int(self.storage.save_taker_cost(cost, at))
            for quote in hypothetical_quotes(snapshot):
                counts["quotes"] += int(self.storage.save_quote(quote))
            persistence_ms += (perf_counter() - stage_started) * 1000
        self.capture_cycles += 1
        self.storage.set_state("last_capture_cycle_at", at)
        self.storage.set_state("capture_cycles", self.capture_cycles)
        self.timings.record("snapshot_assembly_ms", assembly_ms)
        self.timings.record("snapshot_persistence_ms", persistence_ms)
        self.timings.record("capture_total_ms", (perf_counter() - capture_started) * 1000)
        return counts

    def refresh_rest(
        self, *, storage: MicrostructureStorage | None = None,
    ) -> dict[str, str]:
        store = storage or self.storage
        stage_timings: dict[str, float] = {}
        started = perf_counter()
        failures = refresh_market_state(
            self.client,
            self.state,
            self.symbols,
            stage_timings_ms=stage_timings,
        )
        total_ms = (perf_counter() - started) * 1000
        self.timings.record("required_rest_refresh_ms", total_ms)
        self.timings.record(
            "oi_refresh_ms", stage_timings.get("oi_refresh_ms", 0.0)
        )
        self.last_rest_failures = failures
        store.set_state("last_rest_refresh", {
            "at": self.now(), "failures": failures,
            "clock_offset_ms": self.clock_offset_ms,
            "clock_round_trip_ms": self.clock_round_trip_ms,
        })
        return failures

    def refresh_clock_diagnostic(self) -> bool:
        started = perf_counter()
        success = True
        try:
            self.clock_offset_ms, self.clock_round_trip_ms = self.client.clock_offset_ms()
            self.last_rest_failures.pop("clock", None)
        except Exception as exc:
            self.last_rest_failures["clock"] = type(exc).__name__
            success = False
        self.timings.record("clock_diagnostic_ms", (perf_counter() - started) * 1000)
        return success

    def refresh_funding_events(
        self, *, storage: MicrostructureStorage | None = None,
    ) -> dict[str, int]:
        store = storage or self.storage
        started = perf_counter()
        observed_at = self.now()
        inserted = 0
        failures: dict[str, str] = {}
        for symbol in self.symbols:
            try:
                rows = self.client.funding_history(symbol, limit=20)
                for raw in rows:
                    timestamp = _timestamp_ms(raw.get("fundingRateTimestamp"))
                    rate = _decimal(raw.get("fundingRate"))
                    if timestamp is None or rate is None or timestamp > observed_at:
                        continue
                    context_at = timestamp - timedelta(
                        milliseconds=float(self.clock_offset_ms or Decimal("0"))
                    )
                    context = store.capture_at_or_before(symbol, context_at)
                    complete_context = bool(
                        context is not None
                        and context.spot is not None
                        and context.perpetual is not None
                    )
                    event = FundingEventRecord(
                        event_id=stable_id("funding-event", symbol, timestamp),
                        symbol=symbol,
                        funding_timestamp=timestamp,
                        observed_at=observed_at,
                        funding_rate=rate,
                        mark_price=(
                            context.perpetual.mark_price
                            if context is not None and context.perpetual is not None
                            else None
                        ),
                        index_price=(
                            context.perpetual.index_price
                            if context is not None and context.perpetual is not None
                            else None
                        ),
                        spot_perp_basis_bps=(
                            context.perp_mid_vs_spot_mid_bps if context is not None else None
                        ),
                        open_interest=(
                            context.perpetual.open_interest
                            if context is not None and context.perpetual is not None
                            else None
                        ),
                        volatility_context_bps=(
                            context.perpetual.volatility_5m_bps
                            if context is not None and context.perpetual is not None
                            else None
                        ),
                        context_coverage=(
                            CoverageState.AVAILABLE
                            if complete_context else CoverageState.PARTIAL
                        ),
                    )
                    inserted += int(store.save_funding_event(event))
            except Exception as exc:
                failures[symbol] = type(exc).__name__
        self.last_funding_failures = failures
        store.set_state("last_funding_refresh", {
            "at": observed_at, "inserted": inserted, "failures": failures,
        })
        self.timings.record("funding_history_ms", (perf_counter() - started) * 1000)
        return {"inserted": inserted, "failures": len(failures)}

    def evaluate_maker_telemetry(
        self,
        *,
        evaluated_at: datetime | None = None,
        batch_limit: int = MAKER_BATCH_SIZE,
        storage: MicrostructureStorage | None = None,
    ) -> int:
        store = storage or self.storage
        at = evaluated_at or self.now()
        work = store.pending_maker_work(
            evaluated_at=at,
            horizons_seconds=TOUCH_HORIZONS_SECONDS,
            limit=batch_limit,
        )
        grouped: dict[
            tuple[str, str], list[tuple[Any, int]]
        ] = defaultdict(list)
        for quote, horizon in work:
            grouped[(quote.symbol, quote.venue_leg)].append((quote, horizon))
        saved = 0
        for (symbol, venue_leg), rows in grouped.items():
            category = "spot" if venue_leg == "spot" else "linear"
            trades, mids = self.state.quote_paths(category, symbol)
            first_quote_at = min(quote.quote_time for quote, _ in rows)
            stored_trades, stored_mids = self._paths_from_captures(
                symbol, venue_leg, first_quote_at, at, storage=store
            )
            exchange_trades = self._exchange_path_to_local((*stored_trades, *trades))
            exchange_mids = self._exchange_path_to_local(mids)
            trade_path = _deduplicate_path(exchange_trades)
            mid_path = _deduplicate_path((*stored_mids, *exchange_mids))
            for quote, horizon in rows:
                result = evaluate_hypothetical_touch(
                    quote,
                    horizon_seconds=horizon,
                    evaluated_at=at,
                    trades=trade_path,
                    midpoints=mid_path,
                    paths_sorted=True,
                )
                saved += int(store.save_touch_outcome(result))
        self._worker_stats["maker"]["last_processed_count"] = len(work)
        self._worker_stats["maker"]["processed_count"] += len(work)
        self._worker_stats["maker"]["pending_count"] = store.pending_maker_count(
            evaluated_at=at,
            horizons_seconds=TOUCH_HORIZONS_SECONDS,
        )
        return saved

    def mature_future_labels(
        self,
        *,
        evaluated_at: datetime | None = None,
        batch_limit: int = LABEL_BATCH_SIZE,
        storage: MicrostructureStorage | None = None,
    ) -> int:
        store = storage or self.storage
        at = evaluated_at or self.now()
        saved = 0
        processed = 0
        for horizon, spec in FUTURE_LABEL_HORIZONS:
            for notional in self.config.notionals_usdt:
                remaining = batch_limit - processed
                if remaining <= 0:
                    break
                rows = store.due_opportunities_missing_label(
                    horizon=horizon,
                    notional_usdt=notional,
                    evaluated_at=at,
                    fixed_delta=spec if isinstance(spec, timedelta) else None,
                    funding_intervals=spec if isinstance(spec, int) else None,
                    limit=remaining,
                )
                for row in rows:
                    processed += 1
                    canonical_payload = row.get("canonical_opportunity") or {}
                    try:
                        opportunity = CarryOpportunity.model_validate(canonical_payload)
                    except Exception:
                        continue
                    if isinstance(spec, int):
                        if opportunity.funding_interval_hours is None:
                            continue
                        delta = timedelta(seconds=float(
                            opportunity.funding_interval_hours * Decimal("3600") * spec
                        ))
                    else:
                        delta = spec
                    target = opportunity.timestamp + delta
                    if target > at:
                        continue
                    captures = store.captures_between(
                        opportunity.symbol, opportunity.timestamp, target
                    )
                    funding = store.funding_between(
                        opportunity.symbol, opportunity.timestamp, target
                    )
                    label = self._build_label(
                        opportunity, captures, funding,
                        horizon=horizon, target=target, notional=notional,
                        research_blockers=list(row.get("blockers") or []),
                        classification=str(row.get("classification") or "UNKNOWN"),
                    )
                    payload = label.model_dump(mode="json")
                    payload["execution_scenario"] = "TAKER_TAKER"
                    payload["maker_scenario_separate"] = "HYPOTHETICAL_MAKER"
                    payload["failure_modes"] = self._failure_modes(
                        opportunity, captures, funding, label
                    )
                    payload["fee_configuration"] = self.config.cost_payload()
                    saved += int(store.save_label(
                        label_id=stable_id(
                            "carry-label", opportunity.opportunity_id, horizon, notional
                        ),
                        opportunity_id=str(opportunity.opportunity_id),
                        horizon=horizon,
                        notional_usdt=notional,
                        target_at=target,
                        coverage=label.coverage.value,
                        payload=payload,
                    ))
            if processed >= batch_limit:
                break
        self._worker_stats["label"]["last_processed_count"] = processed
        self._worker_stats["label"]["processed_count"] += processed
        self._worker_stats["label"]["pending_count"] = self._due_label_count(
            at, storage=store
        )
        return saved

    def write_reports(
        self, *, storage: MicrostructureStorage | None = None,
    ) -> dict[str, Any]:
        store = storage or self.storage
        started = perf_counter()
        quality = store.data_quality()
        funding = store.funding_summary()
        quality["funding_persistence"] = funding
        quality["websocket_reconnects"] = dict(self.state.reconnects)
        quality["last_websocket_errors"] = dict(self.state.last_error)
        quality["clock_offset_ms"] = (
            str(self.clock_offset_ms) if self.clock_offset_ms is not None else None
        )
        quality["clock_round_trip_ms"] = (
            str(self.clock_round_trip_ms)
            if self.clock_round_trip_ms is not None else None
        )
        readiness = store.readiness(symbol_count=len(self.symbols))
        readiness["funding_persistence"] = funding
        readiness["gates"]["account_specific_fees_configured"] = all(
            value is not None for value in self.config.account_fees_bps.values()
        )
        readiness["ready_for_frozen_v5_carry_analysis"] = all(
            readiness["gates"].values()
        )
        worker_status = self._research_worker_status()
        quality["stage_timings_ms"] = self.timings.snapshot()
        quality["research_workers"] = worker_status
        quality["collector_health"] = self._health_state()
        readiness["stage_timings_ms"] = quality["stage_timings_ms"]
        readiness["research_workers"] = worker_status
        readiness["collector_health"] = quality["collector_health"]
        write_json_atomic(self.config.artifact_dir / "data-quality.json", quality)
        write_json_atomic(
            self.config.artifact_dir / "collection-readiness.json", readiness
        )
        elapsed_ms = (perf_counter() - started) * 1000
        self.timings.record("readiness_generation_ms", elapsed_ms)
        store.set_state("stage_timing_metrics", self.timings.snapshot())
        store.set_state("research_worker_status", worker_status)
        return {"quality": quality, "readiness": readiness}

    def status(self) -> dict[str, Any]:
        return {
            "at": self.now(),
            "mode": "READ_ONLY_MARKET_TELEMETRY",
            "initialized_at": self.initialized_at,
            "symbols": self.symbols,
            "capture_cycles": self.capture_cycles,
            "clock_offset_ms": self.clock_offset_ms,
            "clock_round_trip_ms": self.clock_round_trip_ms,
            "rest_failures": self.last_rest_failures,
            "funding_failures": self.last_funding_failures,
            "websocket_reconnects": dict(self.state.reconnects),
            "websocket_last_message_at": dict(self.state.last_message_at),
            "health": self._health_state(),
            "stage_timings_ms": self.timings.snapshot(),
            "research_workers": self._research_worker_status(),
            "last_capture_interval_seconds": self._last_capture_interval_seconds,
            "last_capture_lateness_seconds": self._last_capture_lateness_seconds,
            "exchange_execution_capability": self.client.exchange_mutation_capable,
        }

    async def run(
        self,
        stop_event: asyncio.Event,
        *,
        max_capture_cycles: int | None = None,
        heartbeat: Callable[[dict[str, Any]], None] | None = None,
    ) -> None:
        if not self.symbols:
            await asyncio.to_thread(self.initialize)
        pumps = (
            PublicWebSocketPump(
                category="spot", url=self.config.spot_ws_url, state=self.state
            ),
            PublicWebSocketPump(
                category="linear", url=self.config.linear_ws_url, state=self.state
            ),
        )
        self._worker_queues = {
            "maker": asyncio.Queue(maxsize=1),
            "label": asyncio.Queue(maxsize=1),
            "maintenance": asyncio.Queue(maxsize=1),
        }
        self._worker_storages = {
            name: MicrostructureStorage(self.config.artifact_dir)
            for name in self._worker_queues
        }
        pump_tasks = [asyncio.create_task(pump.run(self.symbols)) for pump in pumps]
        worker_tasks = [
            asyncio.create_task(
                self._capture_worker(stop_event, max_capture_cycles=max_capture_cycles)
            ),
            asyncio.create_task(self._maker_outcome_worker(stop_event)),
            asyncio.create_task(self._future_label_worker(stop_event)),
            asyncio.create_task(self._periodic_maintenance_worker(stop_event)),
        ]
        for name in self._worker_queues:
            self._signal_worker(name)
        try:
            while not stop_event.is_set():
                if heartbeat is not None:
                    heartbeat(self.status())
                try:
                    await asyncio.wait_for(stop_event.wait(), timeout=0.5)
                except asyncio.TimeoutError:
                    pass
        finally:
            stop_event.set()
            for pump in pumps:
                pump.stop()
            for task in pump_tasks:
                task.cancel()
            await asyncio.gather(*pump_tasks, return_exceptions=True)
            await asyncio.gather(*worker_tasks, return_exceptions=True)
            await asyncio.to_thread(
                self.evaluate_maker_telemetry,
                batch_limit=MAKER_BATCH_SIZE,
                storage=self._worker_storages["maker"],
            )
            await asyncio.to_thread(
                self.mature_future_labels,
                batch_limit=LABEL_BATCH_SIZE,
                storage=self._worker_storages["label"],
            )
            await asyncio.to_thread(
                self.write_reports, storage=self._worker_storages["maintenance"]
            )
            self.storage.set_state("last_clean_shutdown_at", self.now())

    async def _capture_worker(
        self,
        stop_event: asyncio.Event,
        *,
        max_capture_cycles: int | None,
    ) -> None:
        loop = asyncio.get_running_loop()
        next_capture = loop.time()
        next_rest = next_capture + self.config.rest_refresh_seconds
        while not stop_event.is_set():
            if not await _wait_until(stop_event, next_capture):
                break
            cycle_started = loop.time()
            self._last_capture_lateness_seconds = max(0.0, cycle_started - next_capture)
            self.timings.record(
                "capture_schedule_lateness_ms",
                self._last_capture_lateness_seconds * 1000,
            )
            if self._last_capture_started_monotonic is not None:
                self._last_capture_interval_seconds = (
                    cycle_started - self._last_capture_started_monotonic
                )
                self.timings.record(
                    "capture_start_interval_ms",
                    self._last_capture_interval_seconds * 1000,
                )
            self._last_capture_started_monotonic = cycle_started
            if cycle_started >= next_rest:
                await asyncio.to_thread(self.refresh_rest)
                next_rest = advance_fixed_deadline(
                    next_rest,
                    loop.time(),
                    self.config.rest_refresh_seconds,
                )
            await asyncio.to_thread(self.capture_once)
            for name in self._worker_queues:
                self._signal_worker(name)
            next_capture = advance_fixed_deadline(
                next_capture,
                loop.time(),
                self.config.capture_cadence_seconds,
            )
            if max_capture_cycles is not None and self.capture_cycles >= max_capture_cycles:
                stop_event.set()

    async def _maker_outcome_worker(self, stop_event: asyncio.Event) -> None:
        queue = self._worker_queues["maker"]
        while await _next_worker_signal(queue, stop_event):
            started = perf_counter()
            await asyncio.to_thread(
                self.evaluate_maker_telemetry,
                batch_limit=MAKER_BATCH_SIZE,
                storage=self._worker_storages["maker"],
            )
            self.timings.record("maker_worker_ms", (perf_counter() - started) * 1000)

    async def _future_label_worker(self, stop_event: asyncio.Event) -> None:
        queue = self._worker_queues["label"]
        while await _next_worker_signal(queue, stop_event):
            started = perf_counter()
            await asyncio.to_thread(
                self.mature_future_labels,
                batch_limit=LABEL_BATCH_SIZE,
                storage=self._worker_storages["label"],
            )
            self.timings.record("label_worker_ms", (perf_counter() - started) * 1000)

    async def _periodic_maintenance_worker(self, stop_event: asyncio.Event) -> None:
        queue = self._worker_queues["maintenance"]
        loop = asyncio.get_running_loop()
        current = loop.time()
        next_funding = current
        next_clock = current + self.config.rest_refresh_seconds
        next_report = current
        while await _next_worker_signal(queue, stop_event):
            current = loop.time()
            processed = 0
            if current >= next_funding:
                await asyncio.to_thread(
                    self.refresh_funding_events,
                    storage=self._worker_storages["maintenance"],
                )
                processed += 1
                next_funding = advance_fixed_deadline(
                    next_funding, loop.time(), self.config.funding_refresh_seconds
                )
            current = loop.time()
            if current >= next_clock:
                await asyncio.to_thread(self.refresh_clock_diagnostic)
                processed += 1
                next_clock = advance_fixed_deadline(
                    next_clock, loop.time(), self.config.rest_refresh_seconds
                )
            current = loop.time()
            if current >= next_report:
                await asyncio.to_thread(
                    self.write_reports,
                    storage=self._worker_storages["maintenance"],
                )
                processed += 1
                next_report = advance_fixed_deadline(
                    next_report, loop.time(), self.config.report_refresh_seconds
                )
            self._worker_stats["maintenance"]["last_processed_count"] = processed
            self._worker_stats["maintenance"]["processed_count"] += processed

    def _signal_worker(self, name: str) -> None:
        queue = self._worker_queues.get(name)
        if queue is None:
            return
        try:
            queue.put_nowait(None)
        except asyncio.QueueFull:
            self._worker_stats[name]["coalesced_wakeups"] += 1

    def _due_label_count(
        self,
        evaluated_at: datetime,
        *,
        storage: MicrostructureStorage | None = None,
    ) -> int:
        store = storage or self.storage
        total = 0
        for horizon, spec in FUTURE_LABEL_HORIZONS:
            for notional in self.config.notionals_usdt:
                total += store.due_label_count(
                    horizon=horizon,
                    notional_usdt=notional,
                    evaluated_at=evaluated_at,
                    fixed_delta=spec if isinstance(spec, timedelta) else None,
                    funding_intervals=spec if isinstance(spec, int) else None,
                )
        return total

    def _research_worker_status(self) -> dict[str, Any]:
        return {
            name: {
                **stats,
                "queue_depth": self._worker_queues[name].qsize()
                if name in self._worker_queues else 0,
                "queue_capacity": self._worker_queues[name].maxsize
                if name in self._worker_queues else 1,
                "durable_source_of_truth": True,
                "wakeups_may_coalesce_without_dropping_durable_work": True,
            }
            for name, stats in self._worker_stats.items()
        }

    def _health_state(self) -> str:
        if (
            self._last_capture_interval_seconds is not None
            and self._last_capture_interval_seconds
            > self.config.capture_cadence_seconds * 3
        ) or self._last_capture_lateness_seconds > self.config.capture_cadence_seconds:
            return "CAPTURE_DEGRADED"
        if (
            int(self._worker_stats["maker"]["pending_count"]) > MAKER_BATCH_SIZE
            or int(self._worker_stats["label"]["pending_count"]) > LABEL_BATCH_SIZE
        ):
            return "RESEARCH_BACKLOG"
        return "HEALTHY"

    def close(self) -> None:
        if not self._closed:
            for storage in self._worker_storages.values():
                storage.close()
            self._worker_storages.clear()
            self.storage.close()
            self._closed = True

    def _paths_from_captures(
        self,
        symbol: str,
        venue_leg: str,
        start: datetime,
        end: datetime,
        *,
        storage: MicrostructureStorage | None = None,
    ) -> tuple[list[tuple[datetime, Decimal]], list[tuple[datetime, Decimal]]]:
        store = storage or self.storage
        captures = store.captures_between(symbol, start, end)
        trades: list[tuple[datetime, Decimal]] = []
        mids: list[tuple[datetime, Decimal]] = []
        for capture in captures:
            leg = capture.spot if venue_leg == "spot" else capture.perpetual
            if leg is None:
                continue
            mids.append((capture.snapshot_completed_at, leg.mid))
            if leg.recent_trade_timestamp is not None and leg.recent_trade_price is not None:
                trades.append((leg.recent_trade_timestamp, leg.recent_trade_price))
        return trades, mids

    def _exchange_path_to_local(
        self, rows: Sequence[tuple[datetime, Decimal]],
    ) -> list[tuple[datetime, Decimal]]:
        offset = timedelta(milliseconds=float(self.clock_offset_ms or Decimal("0")))
        return [(timestamp - offset, value) for timestamp, value in rows]

    def _build_label(
        self,
        opportunity: CarryOpportunity,
        captures: Sequence[Any],
        funding: Sequence[FundingEventRecord],
        *,
        horizon: str,
        target: datetime,
        notional: Decimal,
        research_blockers: Sequence[str],
        classification: str,
    ) -> CarryLabel:
        if classification != "POSITIVE_FUNDING_CARRY":
            return CarryLabel(
                opportunity_id=opportunity.opportunity_id,
                symbol=opportunity.symbol,
                horizon=horizon,
                horizon_end=target,
                coverage=DataAvailability.UNKNOWN,
                blockers=[f"UNSUPPORTED_CARRY_CLASSIFICATION:{classification}"],
            )
        quality_blockers = [
            blocker for blocker in research_blockers
            if blocker != "ACCOUNT_FEE_CONFIGURATION_UNKNOWN"
        ]
        if quality_blockers:
            return CarryLabel(
                opportunity_id=opportunity.opportunity_id,
                symbol=opportunity.symbol,
                horizon=horizon,
                horizon_end=target,
                coverage=DataAvailability.UNKNOWN,
                blockers=["LOW_QUALITY_ENTRY_OBSERVATION", *quality_blockers],
            )
        complete_captures = [
            row for row in captures
            if row.complete and row.spot is not None and row.perpetual is not None
        ]
        latest = complete_captures[-1] if complete_captures else None
        maximum_end_gap = timedelta(seconds=self.config.capture_cadence_seconds * 3)
        if latest is None or target - latest.snapshot_completed_at > maximum_end_gap:
            return CarryLabel(
                opportunity_id=opportunity.opportunity_id,
                symbol=opportunity.symbol,
                horizon=horizon,
                horizon_end=target,
                coverage=DataAvailability.UNKNOWN,
                blockers=["COMPLETE_EXIT_OBSERVATION_MISSING_AT_HORIZON"],
            )
        path = [
            CarryPathPoint(
                timestamp=row.snapshot_completed_at,
                spot_bid=row.spot.best_bid,
                spot_ask=row.spot.best_ask,
                perp_bid=row.perpetual.best_bid,
                perp_ask=row.perpetual.best_ask,
            )
            for row in complete_captures
        ]
        payments = [
            FundingPayment(
                timestamp=row.funding_timestamp,
                rate=row.funding_rate,
                interval_hours=(
                    opportunity.funding_interval_hours or Decimal("8")
                ),
                source="BYBIT_V5_FUNDING_HISTORY",
                authoritative=True,
            )
            for row in funding
        ]
        return build_carry_label(
            opportunity,
            path,
            payments,
            horizon=horizon,
            horizon_end=target,
            notional_usdt=notional,
        )

    def _failure_modes(
        self,
        opportunity: CarryOpportunity,
        captures: Sequence[Any],
        funding: Sequence[FundingEventRecord],
        label: CarryLabel,
    ) -> dict[str, Any]:
        complete = [
            row for row in captures
            if row.complete and row.spot is not None and row.perpetual is not None
        ]
        initial_rate = opportunity.current_funding_rate
        rates = [(row.funding_timestamp, row.funding_rate) for row in funding]
        initial_basis = opportunity.basis_bps
        basis = [
            (row.snapshot_completed_at, row.perp_mid_vs_spot_mid_bps)
            for row in complete if row.perp_mid_vs_spot_mid_bps is not None
        ]
        initial_spot_spread = opportunity.spot_spread_bps
        initial_perp_spread = opportunity.perp_spread_bps
        spread_widening_at = next((
            row.snapshot_completed_at for row in complete
            if (
                initial_spot_spread is not None
                and row.spot.spread_bps > initial_spot_spread * 2
            ) or (
                initial_perp_spread is not None
                and row.perpetual.spread_bps > initial_perp_spread * 2
            )
        ), None)
        sign_reversal_at = next((
            timestamp for timestamp, rate in rates
            if initial_rate is not None and initial_rate != 0 and rate * initial_rate < 0
        ), None)
        collapse_at = next((
            timestamp for timestamp, rate in rates
            if initial_rate is not None and initial_rate != 0
            and abs(rate) <= abs(initial_rate) / 2
        ), None)
        basis_widening_at = next((
            timestamp for timestamp, value in basis
            if initial_basis is not None and value > initial_basis
        ), None)
        basis_convergence_at = next((
            timestamp for timestamp, value in basis
            if initial_basis is not None and abs(value) < abs(initial_basis)
        ), None)
        initial_depth = None
        if opportunity.spot_bid_depth_usdt is not None and opportunity.perp_ask_depth_usdt is not None:
            initial_depth = min(
                opportunity.spot_bid_depth_usdt, opportunity.perp_ask_depth_usdt
            )
        liquidity_at = next((
            row.snapshot_completed_at for row in complete
            if initial_depth is not None and min(
                row.spot.depth_bps_usdt["50"]["bid"],
                row.perpetual.depth_bps_usdt["50"]["ask"],
            ) < initial_depth / 2
        ), None)
        oi_shock_at = next((
            row.snapshot_completed_at for row in complete
            if row.perpetual.open_interest_change_pct is not None
            and abs(row.perpetual.open_interest_change_pct) >= Decimal("10")
        ), None)
        initial_vol = complete[0].perpetual.volatility_5m_bps if complete else None
        volatility_at = next((
            row.snapshot_completed_at for row in complete
            if initial_vol is not None and initial_vol > 0
            and row.perpetual.volatility_5m_bps is not None
            and row.perpetual.volatility_5m_bps >= initial_vol * 2
        ), None)
        break_even_at = (
            opportunity.timestamp + timedelta(seconds=float(label.time_to_break_even_seconds))
            if label.time_to_break_even_seconds is not None else None
        )

        def classify(timestamp: datetime | None) -> dict[str, Any]:
            return {
                "occurred": timestamp is not None,
                "first_at": timestamp,
                "relative_to_break_even": (
                    "BEFORE_BREAK_EVEN"
                    if timestamp is not None and break_even_at is not None
                    and timestamp < break_even_at
                    else "AFTER_BREAK_EVEN"
                    if timestamp is not None and break_even_at is not None
                    else "UNKNOWN"
                ),
            }

        return {
            "funding_sign_reversal": classify(sign_reversal_at),
            "funding_collapse_50pct": classify(collapse_at),
            "basis_widening": classify(basis_widening_at),
            "basis_convergence": classify(basis_convergence_at),
            "liquidity_deterioration_50pct": classify(liquidity_at),
            "spread_widening_2x": classify(spread_widening_at),
            "open_interest_shock_10pct": classify(oi_shock_at),
            "volatility_shock_2x": classify(volatility_at),
            "definitions_are_research_diagnostics_not_strategy_thresholds": True,
        }

    def _write_static_artifacts(self) -> None:
        write_json_atomic(
            self.config.artifact_dir / "collector-config.json",
            self.config.public_payload(),
        )
        write_json_atomic(
            self.config.artifact_dir / "cost-config.json",
            self.config.cost_payload(),
        )
        (self.config.artifact_dir / "audit.md").write_text(_AUDIT, encoding="utf-8")
        (self.config.artifact_dir / "schema.md").write_text(_SCHEMA, encoding="utf-8")


def _deduplicate_path(
    rows: Sequence[tuple[datetime, Decimal]],
) -> list[tuple[datetime, Decimal]]:
    return sorted({(timestamp, value) for timestamp, value in rows}, key=lambda row: row[0])


def _float_percentile(values: Sequence[float], probability: float) -> float | None:
    if not values:
        return None
    position = max(0.0, min(len(values) - 1, (len(values) - 1) * probability))
    lower = int(position)
    upper = min(len(values) - 1, lower + 1)
    if lower == upper:
        return float(values[lower])
    fraction = position - lower
    return float(values[lower] + (values[upper] - values[lower]) * fraction)


_AUDIT = """# Market microstructure collector audit

- Reused V5 carry models and causal future-label arithmetic.
- Reused the V2 public WebSocket/REST patterns without importing execution, risk, or capacity.
- PostgreSQL migration `20260811_0015` is additive V4 research storage; collector telemetry is
  isolated in artifact-local SQLite plus append-only JSONL.
- The HTTP client allowlists only `/v5/market/*` GET endpoints and exposes
  `exchange_mutation_capable=false`.
- No Demo, private account, order, admission, portfolio, or production risk component is used.
"""


_SCHEMA = """# Microstructure telemetry schema

`telemetry.sqlite` is the normalized query store. `raw/<record-type>/<UTC-date>.jsonl`
is the append-only evidence stream and `columnar-csv/<record-type>/<UTC-date>.csv` provides
stable daily tabular partitions without adding a heavyweight Parquet dependency. Tables cover
universe decisions, synchronized captures,
carry candidates, four-notional taker costs, exact funding events, hypothetical quotes and
touch/markout outcomes, future labels, collection gaps, and collector state.

Snapshot payloads preserve source/exchange/local/completion timing; complete/partial status;
spot/perpetual books and 5/10/25/50 bps depth; recent trades; mark/index; funding schedule;
open interest; volatility; basis; and explicit unavailable fields. Research labels use only
observations at or before their horizon and never claim an exchange fill.
"""
