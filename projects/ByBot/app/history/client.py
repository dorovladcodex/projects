from __future__ import annotations

import json
import time
from collections.abc import Callable, Iterator
from decimal import Decimal, InvalidOperation
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen

from app.history.models import (
    FundingRate,
    Kline,
    KlineInterval,
    OpenInterest,
    OpenInterestInterval,
)

HttpGet = Callable[[str, dict[str, str], float], dict[str, Any]]

KLINE_PAGE_LIMIT = 1000
FUNDING_PAGE_LIMIT = 200
OPEN_INTEREST_PAGE_LIMIT = 200

_USER_AGENT = "ByBot-History/1.0 READ_ONLY"


class HistoryRequestError(RuntimeError):
    """Raised when Bybit rejects a read or returns an unusable payload."""


def _default_http_get(url: str, params: dict[str, str], timeout: float) -> dict[str, Any]:
    query = urlencode(sorted(params.items()))
    request = Request(f"{url}?{query}" if query else url, headers={"User-Agent": _USER_AGENT})
    with urlopen(request, timeout=timeout) as response:
        payload = json.loads(response.read().decode("utf-8"))
    if not isinstance(payload, dict):
        raise HistoryRequestError("Bybit public response must be an object")
    return payload


def _decimal(raw: Any, field: str) -> Decimal:
    if raw is None or raw == "":
        raise HistoryRequestError(f"missing numeric field {field}")
    try:
        return Decimal(str(raw))
    except (InvalidOperation, ValueError) as exc:
        raise HistoryRequestError(f"unparsable numeric field {field}: {raw!r}") from exc


class BybitHistoryClient:
    """Public, unauthenticated Bybit V5 reader for historical research series.

    Only market-data paths are allowlisted. The client holds no credentials and
    has no method capable of mutating exchange state.
    """

    ALLOWED_PATHS = frozenset(
        {
            "/v5/market/kline",
            "/v5/market/funding/history",
            "/v5/market/open-interest",
            "/v5/market/instruments-info",
        }
    )

    def __init__(
        self,
        base_url: str = "https://api.bybit.com",
        *,
        timeout_seconds: float = 15.0,
        http_get: HttpGet | None = None,
        min_request_interval_seconds: float = 0.06,
        max_attempts: int = 5,
        sleep: Callable[[float], None] = time.sleep,
        monotonic: Callable[[], float] = time.monotonic,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.timeout_seconds = timeout_seconds
        self.http_get = http_get or _default_http_get
        self.min_request_interval_seconds = min_request_interval_seconds
        self.max_attempts = max_attempts
        self._sleep = sleep
        self._monotonic = monotonic
        self._last_request_at: float | None = None
        self.exchange_mutation_capable = False
        self.request_count = 0

    # ----------------------------------------------------------------- raw IO

    def _throttle(self) -> None:
        if self._last_request_at is None:
            return
        elapsed = self._monotonic() - self._last_request_at
        remaining = self.min_request_interval_seconds - elapsed
        if remaining > 0:
            self._sleep(remaining)

    def get(self, path: str, params: dict[str, str]) -> dict[str, Any]:
        if path not in self.ALLOWED_PATHS:
            raise ValueError(f"history client path is not allowlisted: {path}")

        last_error: Exception | None = None
        for attempt in range(1, self.max_attempts + 1):
            self._throttle()
            try:
                payload = self.http_get(f"{self.base_url}{path}", params, self.timeout_seconds)
            except (HTTPError, URLError, TimeoutError, OSError, json.JSONDecodeError) as exc:
                last_error = exc
            else:
                self._last_request_at = self._monotonic()
                self.request_count += 1
                ret_code = int(payload.get("retCode", -1))
                if ret_code == 0:
                    return payload
                # 10006/10018 are rate limits; retrying is the documented remedy.
                if ret_code not in (10006, 10018):
                    raise HistoryRequestError(
                        f"Bybit read failed: path={path} retCode={ret_code} "
                        f"retMsg={payload.get('retMsg')!r}"
                    )
                last_error = HistoryRequestError(f"rate limited: retCode={ret_code}")
            self._last_request_at = self._monotonic()
            if attempt < self.max_attempts:
                self._sleep(min(2.0 ** (attempt - 1), 30.0))

        raise HistoryRequestError(
            f"Bybit read failed after {self.max_attempts} attempts: path={path}"
        ) from last_error

    @staticmethod
    def _rows(payload: dict[str, Any]) -> list[Any]:
        result = payload.get("result")
        if not isinstance(result, dict):
            raise HistoryRequestError("Bybit response is missing result")
        rows = result.get("list")
        if rows is None:
            return []
        if not isinstance(rows, list):
            raise HistoryRequestError("Bybit result.list is not an array")
        return rows

    # -------------------------------------------------------------- one page

    def kline_page(
        self, symbol: str, interval: KlineInterval, start_ms: int, end_ms: int
    ) -> list[Kline]:
        payload = self.get(
            "/v5/market/kline",
            {
                "category": "linear",
                "symbol": symbol,
                "interval": interval.value,
                "start": str(start_ms),
                "end": str(end_ms),
                "limit": str(KLINE_PAGE_LIMIT),
            },
        )
        bars: list[Kline] = []
        for row in self._rows(payload):
            if not isinstance(row, list) or len(row) < 7:
                raise HistoryRequestError(f"unexpected kline row shape: {row!r}")
            bars.append(
                Kline(
                    symbol=symbol,
                    interval=interval,
                    start_ms=int(row[0]),
                    open=_decimal(row[1], "open"),
                    high=_decimal(row[2], "high"),
                    low=_decimal(row[3], "low"),
                    close=_decimal(row[4], "close"),
                    volume=_decimal(row[5], "volume"),
                    turnover=_decimal(row[6], "turnover"),
                )
            )
        bars.sort(key=lambda bar: bar.start_ms)
        return bars

    def funding_page(self, symbol: str, start_ms: int, end_ms: int) -> list[FundingRate]:
        payload = self.get(
            "/v5/market/funding/history",
            {
                "category": "linear",
                "symbol": symbol,
                "startTime": str(start_ms),
                "endTime": str(end_ms),
                "limit": str(FUNDING_PAGE_LIMIT),
            },
        )
        rates: list[FundingRate] = []
        for row in self._rows(payload):
            if not isinstance(row, dict):
                raise HistoryRequestError(f"unexpected funding row shape: {row!r}")
            rates.append(
                FundingRate(
                    symbol=symbol,
                    funding_rate=_decimal(row.get("fundingRate"), "fundingRate"),
                    funding_time_ms=int(row["fundingRateTimestamp"]),
                )
            )
        rates.sort(key=lambda rate: rate.funding_time_ms)
        return rates

    def open_interest_page(
        self, symbol: str, interval: OpenInterestInterval, start_ms: int, end_ms: int
    ) -> list[OpenInterest]:
        payload = self.get(
            "/v5/market/open-interest",
            {
                "category": "linear",
                "symbol": symbol,
                "intervalTime": interval.value,
                "startTime": str(start_ms),
                "endTime": str(end_ms),
                "limit": str(OPEN_INTEREST_PAGE_LIMIT),
            },
        )
        points: list[OpenInterest] = []
        for row in self._rows(payload):
            if not isinstance(row, dict):
                raise HistoryRequestError(f"unexpected open interest row shape: {row!r}")
            points.append(
                OpenInterest(
                    symbol=symbol,
                    interval=interval,
                    timestamp_ms=int(row["timestamp"]),
                    open_interest=_decimal(row.get("openInterest"), "openInterest"),
                )
            )
        points.sort(key=lambda point: point.timestamp_ms)
        return points

    # ------------------------------------------------------------ pagination

    def iter_klines(
        self, symbol: str, interval: KlineInterval, start_ms: int, end_ms: int
    ) -> Iterator[list[Kline]]:
        """Yield ascending kline pages covering [start_ms, end_ms).

        Empty windows are skipped rather than treated as the end of history, so
        exchange downtime or a pre-listing range cannot silently truncate a
        backfill.
        """
        window = KLINE_PAGE_LIMIT * interval.milliseconds
        cursor = start_ms
        while cursor < end_ms:
            window_end = min(cursor + window, end_ms)
            page = self.kline_page(symbol, interval, cursor, window_end)
            if page:
                yield page
                cursor = max(page[-1].start_ms + interval.milliseconds, cursor + 1)
            else:
                cursor = window_end

    def iter_funding(self, symbol: str, start_ms: int, end_ms: int) -> Iterator[list[FundingRate]]:
        # Funding cadence varies by symbol, so advance from observed data when a
        # page is full and by a conservative window when it is not.
        window = FUNDING_PAGE_LIMIT * 3_600_000
        cursor = start_ms
        while cursor < end_ms:
            window_end = min(cursor + window, end_ms)
            page = self.funding_page(symbol, cursor, window_end)
            if page:
                yield page
                if len(page) >= FUNDING_PAGE_LIMIT:
                    cursor = page[-1].funding_time_ms + 1
                else:
                    cursor = window_end
            else:
                cursor = window_end

    def iter_open_interest(
        self, symbol: str, interval: OpenInterestInterval, start_ms: int, end_ms: int
    ) -> Iterator[list[OpenInterest]]:
        window = OPEN_INTEREST_PAGE_LIMIT * interval.milliseconds
        cursor = start_ms
        while cursor < end_ms:
            window_end = min(cursor + window, end_ms)
            page = self.open_interest_page(symbol, interval, cursor, window_end)
            if page:
                yield page
                cursor = max(page[-1].timestamp_ms + interval.milliseconds, cursor + 1)
            else:
                cursor = window_end

    # ------------------------------------------------------------- discovery

    def earliest_kline_ms(
        self, symbol: str, interval: KlineInterval, *, floor_ms: int, ceiling_ms: int
    ) -> int | None:
        """Binary-search the first timestamp with data, for listing detection."""
        probe = interval.milliseconds * 8
        if self.kline_page(symbol, interval, floor_ms, floor_ms + probe):
            return floor_ms
        if not self.kline_page(symbol, interval, ceiling_ms - probe, ceiling_ms):
            return None

        low, high = floor_ms, ceiling_ms
        while high - low > interval.milliseconds * 1440:
            middle = (low + high) // 2
            if self.kline_page(symbol, interval, middle, middle + probe):
                high = middle
            else:
                low = middle
        page = self.kline_page(symbol, interval, low, high + probe)
        return page[0].start_ms if page else high
