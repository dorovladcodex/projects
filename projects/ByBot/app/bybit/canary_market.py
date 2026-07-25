from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from time import monotonic, sleep
from typing import Callable, Iterable

from app.models import MarketSnapshot, Symbol


@dataclass(frozen=True)
class CanaryMarketObservation:
    snapshot: MarketSnapshot
    source: str
    exchange_timestamp: datetime
    received_at: datetime


@dataclass(frozen=True)
class CanaryMarketReadiness:
    observation: CanaryMarketObservation
    age_seconds: float
    waited_seconds: float
    attempts: int

    def as_payload(self) -> dict[str, object]:
        return {
            "symbol": self.observation.snapshot.symbol.value,
            "source": self.observation.source,
            "exchange_timestamp": self.observation.exchange_timestamp.isoformat(),
            "received_at": self.observation.received_at.isoformat(),
            "age_seconds": self.age_seconds,
            "waited_seconds": self.waited_seconds,
            "attempts": self.attempts,
        }


class CanaryMarketReadinessError(RuntimeError):
    def __init__(self, reason: str, report: dict[str, object]) -> None:
        super().__init__(reason)
        self.report = report


ObservationProvider = Callable[[Symbol], CanaryMarketObservation | None]


def wait_for_canary_market_data(
    symbol: Symbol,
    *,
    accepted_symbols: Iterable[Symbol],
    websocket_provider: ObservationProvider | None,
    rest_provider: ObservationProvider,
    timeout_seconds: float,
    websocket_warmup_seconds: float,
    freshness_seconds: float,
    clock: Callable[[], datetime] | None = None,
    monotonic_clock: Callable[[], float] = monotonic,
    sleeper: Callable[[float], None] = sleep,
    poll_seconds: float = 0.25,
) -> CanaryMarketReadiness:
    """Wait for a symbol-specific authoritative observation without mutation."""

    accepted = set(accepted_symbols)
    if symbol not in accepted:
        raise CanaryMarketReadinessError(
            "symbol is unavailable in the accepted Demo universe",
            {
                "symbol": symbol.value,
                "status": "UNAVAILABLE",
                "reason": "symbol_not_available_on_demo",
                "attempts": 0,
                "waited_seconds": 0.0,
            },
        )
    if timeout_seconds <= 0 or freshness_seconds <= 0:
        raise ValueError("market-data readiness and freshness limits must be positive")

    now_fn = clock or (lambda: datetime.now(timezone.utc))
    started = monotonic_clock()
    deadline = started + timeout_seconds
    ws_deadline = min(deadline, started + max(0.0, websocket_warmup_seconds))
    attempts = 0
    last_reason = "no market observation received"
    last_source: str | None = None

    while monotonic_clock() <= deadline:
        provider: ObservationProvider | None
        if websocket_provider is not None and monotonic_clock() <= ws_deadline:
            provider = websocket_provider
        else:
            provider = rest_provider
        attempts += 1
        try:
            observation = provider(symbol) if provider is not None else None
        except Exception as exc:
            last_reason = f"{type(exc).__name__}: market source unavailable"
            observation = None
        if observation is not None:
            last_source = observation.source
            valid, age, reason = _validate_observation(
                observation, now_fn(), freshness_seconds
            )
            if valid:
                return CanaryMarketReadiness(
                    observation=observation,
                    age_seconds=age,
                    waited_seconds=max(0.0, monotonic_clock() - started),
                    attempts=attempts,
                )
            last_reason = reason
        remaining = deadline - monotonic_clock()
        if remaining <= 0:
            break
        sleeper(min(poll_seconds, remaining))

    waited = max(0.0, monotonic_clock() - started)
    raise CanaryMarketReadinessError(
        "fresh authoritative Demo market data was not ready before timeout",
        {
            "symbol": symbol.value,
            "status": "TIMEOUT",
            "reason": last_reason,
            "last_source": last_source,
            "attempts": attempts,
            "waited_seconds": waited,
            "timeout_seconds": timeout_seconds,
            "freshness_seconds": freshness_seconds,
        },
    )


def _validate_observation(
    observation: CanaryMarketObservation,
    now: datetime,
    freshness_seconds: float,
) -> tuple[bool, float, str]:
    timestamps = (observation.exchange_timestamp, observation.received_at, now)
    if any(value.tzinfo is None for value in timestamps):
        return False, float("inf"), "market timestamp is not timezone-aware"
    if observation.snapshot.symbol is None:
        return False, float("inf"), "market symbol is missing"
    if observation.snapshot.ask_price < observation.snapshot.bid_price:
        return False, float("inf"), "market bid/ask is invalid"

    age = (now - observation.exchange_timestamp).total_seconds()
    receive_age = (now - observation.received_at).total_seconds()
    # More than one second in the future is a clock/timestamp defect. Tiny
    # transport clock skew is represented as age zero.
    if age < -1.0 or receive_age < -1.0:
        return False, age, "market timestamp is in the future"
    age = max(0.0, age)
    if age > freshness_seconds:
        return False, age, "market snapshot is stale"
    return True, age, "accepted"
