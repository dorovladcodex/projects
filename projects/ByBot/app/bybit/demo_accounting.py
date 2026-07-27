from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal
from typing import Any, Iterable

from app.models import DemoFill, Side, Symbol


@dataclass(frozen=True)
class DemoAccountingResult:
    entry_quantity: Decimal
    close_quantity: Decimal
    entry_average_price: Decimal
    close_average_price: Decimal
    gross_price_pnl: Decimal
    entry_fees: Decimal
    close_fees: Decimal
    funding_pnl: Decimal
    calculated_net_pnl: Decimal
    authoritative_closed_pnl: Decimal | None
    funding_transaction_ids: tuple[str, ...]
    entry_execution_ids: tuple[str, ...]
    close_execution_ids: tuple[str, ...]
    entry_fill_components: tuple[dict[str, Any], ...]
    close_fill_components: tuple[dict[str, Any], ...]
    fee_currencies: tuple[str, ...]
    status: str
    mismatch: Decimal | None

    @property
    def final(self) -> bool:
        return self.status == "FINAL"

    def payload(self) -> dict[str, Any]:
        return {
            "entry_quantity": str(self.entry_quantity),
            "close_quantity": str(self.close_quantity),
            "entry_average_price": str(self.entry_average_price),
            "close_average_price": str(self.close_average_price),
            "gross_price_pnl": str(self.gross_price_pnl),
            "entry_fees": str(self.entry_fees),
            "close_fees": str(self.close_fees),
            "funding_pnl": str(self.funding_pnl),
            "calculated_net_pnl": str(self.calculated_net_pnl),
            "authoritative_closed_pnl": (
                str(self.authoritative_closed_pnl)
                if self.authoritative_closed_pnl is not None else None
            ),
            "funding_transaction_ids": list(self.funding_transaction_ids),
            "entry_execution_ids": list(self.entry_execution_ids),
            "close_execution_ids": list(self.close_execution_ids),
            "entry_fill_components": list(self.entry_fill_components),
            "close_fill_components": list(self.close_fill_components),
            "fee_currencies": list(self.fee_currencies),
            "status": self.status,
            "mismatch": str(self.mismatch) if self.mismatch is not None else None,
        }


def calculate_fill_level_accounting(
    *,
    symbol: Symbol | str,
    side: Side | str,
    entry_order_id: str,
    close_order_id: str,
    entry_fills: Iterable[DemoFill | dict[str, Any]],
    close_fills: Iterable[DemoFill | dict[str, Any]],
    transaction_log: Iterable[dict[str, Any]] = (),
    closed_pnl_rows: Iterable[dict[str, Any]] = (),
) -> DemoAccountingResult:
    """Build one exact, idempotent accounting result from owned exchange IDs."""

    symbol_value = symbol.value if isinstance(symbol, Symbol) else str(symbol)
    side_value = side.value if isinstance(side, Side) else str(side).upper()
    entries = _deduplicate_fills(entry_fills)
    closes = _deduplicate_fills(close_fills)
    if not entry_order_id or not close_order_id or not entries or not closes:
        raise ValueError("complete exact entry and close fill evidence is required")
    if any(
        row["order_id"] and row["order_id"] != entry_order_id
        for row in entries
    ):
        raise ValueError("entry fill order identity is contradictory")
    if any(
        row["order_id"] and row["order_id"] != close_order_id
        for row in closes
    ):
        raise ValueError("close fill order identity is contradictory")
    entry_quantity = sum((row["quantity"] for row in entries), Decimal("0"))
    close_quantity = sum((row["quantity"] for row in closes), Decimal("0"))
    if entry_quantity <= 0 or close_quantity != entry_quantity:
        raise ValueError("entry and close quantities are not an exact full match")
    entry_average = (
        sum((row["quantity"] * row["price"] for row in entries), Decimal("0"))
        / entry_quantity
    )
    close_average = (
        sum((row["quantity"] * row["price"] for row in closes), Decimal("0"))
        / close_quantity
    )
    direction = Decimal("1") if side_value == Side.BUY.value else Decimal("-1")
    gross = (close_average - entry_average) * close_quantity * direction
    entry_fees = sum((row["fee"] for row in entries), Decimal("0"))
    close_fees = sum((row["fee"] for row in closes), Decimal("0"))
    entry_at = min(row["executed_at"] for row in entries)
    close_at = max(row["executed_at"] for row in closes)
    funding_rows = _owned_funding_rows(
        transaction_log,
        symbol=symbol_value,
        side=side_value,
        quantity=entry_quantity,
        entry_at=entry_at,
        close_at=close_at,
    )
    funding_pnl = sum(
        (_decimal(row.get("funding")) for row in funding_rows), Decimal("0")
    )
    calculated_net = gross - entry_fees - close_fees + funding_pnl
    closed_rows = [
        row for row in closed_pnl_rows
        if str(row.get("orderId") or "") == close_order_id
    ]
    authoritative = (
        _decimal(closed_rows[0].get("closedPnl"))
        if len(closed_rows) == 1 else None
    )
    mismatch = (
        calculated_net - authoritative
        if authoritative is not None else None
    )
    currencies = tuple(sorted({
        str(row["fee_currency"] or "USDT").upper()
        for row in [*entries, *closes]
    }))
    supported_currency = all(value == "USDT" for value in currencies)
    status = (
        "FINAL"
        if authoritative is not None
        and mismatch == 0
        and supported_currency
        else "INCOMPLETE"
        if authoritative is not None
        else "PROVISIONAL"
    )
    return DemoAccountingResult(
        entry_quantity=entry_quantity,
        close_quantity=close_quantity,
        entry_average_price=entry_average,
        close_average_price=close_average,
        gross_price_pnl=gross,
        entry_fees=entry_fees,
        close_fees=close_fees,
        funding_pnl=funding_pnl,
        calculated_net_pnl=calculated_net,
        authoritative_closed_pnl=authoritative,
        funding_transaction_ids=tuple(
            str(row.get("id") or "") for row in funding_rows
        ),
        entry_execution_ids=tuple(row["execution_id"] for row in entries),
        close_execution_ids=tuple(row["execution_id"] for row in closes),
        entry_fill_components=tuple(_fill_component(row) for row in entries),
        close_fill_components=tuple(_fill_component(row) for row in closes),
        fee_currencies=currencies,
        status=status,
        mismatch=mismatch,
    )


def _fill_component(row: dict[str, Any]) -> dict[str, Any]:
    return {
        "execution_id": row["execution_id"],
        "order_id": row["order_id"],
        "quantity": str(row["quantity"]),
        "price": str(row["price"]),
        "fee": str(row["fee"]),
        "fee_currency": row["fee_currency"],
        "is_maker": row["is_maker"],
        "fee_rate": (
            str(row["fee_rate"]) if row["fee_rate"] is not None else None
        ),
        "executed_at": row["executed_at"].isoformat(),
    }


def _deduplicate_fills(
    rows: Iterable[DemoFill | dict[str, Any]],
) -> list[dict[str, Any]]:
    by_id: dict[str, dict[str, Any]] = {}
    for row in rows:
        normalized = _fill(row)
        execution_id = normalized["execution_id"]
        if not execution_id:
            raise ValueError("exchange execution identity is missing")
        existing = by_id.get(execution_id)
        if existing is not None and existing != normalized:
            raise ValueError("duplicate execution identity has conflicting evidence")
        by_id[execution_id] = normalized
    return sorted(by_id.values(), key=lambda item: item["executed_at"])


def _fill(row: DemoFill | dict[str, Any]) -> dict[str, Any]:
    if isinstance(row, DemoFill):
        return {
            "execution_id": row.execution_id,
            "order_id": row.order_id,
            "quantity": row.quantity,
            "price": row.price,
            "fee": row.fee,
            "fee_currency": row.fee_currency,
            "is_maker": row.is_maker,
            "fee_rate": row.fee_rate,
            "executed_at": _aware(row.executed_at),
        }
    return {
        "execution_id": str(row.get("execId") or row.get("execution_id") or ""),
        "order_id": str(row.get("orderId") or row.get("order_id") or ""),
        "quantity": _decimal(row.get("execQty") or row.get("quantity")),
        "price": _decimal(row.get("execPrice") or row.get("price")),
        "fee": _decimal(row.get("execFee") or row.get("fee")),
        "fee_currency": row.get("feeCurrency") or row.get("fee_currency"),
        "is_maker": row.get("isMaker", row.get("is_maker")),
        "fee_rate": (
            _decimal(row.get("feeRate") or row.get("fee_rate"))
            if row.get("feeRate") not in (None, "")
            or row.get("fee_rate") not in (None, "")
            else None
        ),
        "executed_at": _timestamp(
            row.get("execTime") or row.get("executed_at")
        ),
    }


def _owned_funding_rows(
    rows: Iterable[dict[str, Any]],
    *,
    symbol: str,
    side: str,
    quantity: Decimal,
    entry_at: datetime,
    close_at: datetime,
) -> list[dict[str, Any]]:
    selected: dict[str, dict[str, Any]] = {}
    for row in rows:
        if (
            str(row.get("type") or "").upper() != "SETTLEMENT"
            or str(row.get("symbol") or "") != symbol
            or str(row.get("currency") or "USDT").upper() != "USDT"
            or str(row.get("side") or "").upper() not in {"", side}
            or _decimal(row.get("funding")) == 0
        ):
            continue
        stamp = _timestamp(
            row.get("transactionTime") or row.get("createdTime")
        )
        row_quantity = _decimal(row.get("qty") or row.get("size"))
        if (
            stamp < entry_at
            or stamp > close_at
            or (row_quantity > 0 and row_quantity != quantity)
        ):
            continue
        identity = str(row.get("id") or "")
        if not identity:
            raise ValueError("funding transaction identity is missing")
        existing = selected.get(identity)
        if existing is not None and existing != row:
            raise ValueError("funding transaction identity has conflicting evidence")
        selected[identity] = row
    return sorted(
        selected.values(),
        key=lambda row: _timestamp(
            row.get("transactionTime") or row.get("createdTime")
        ),
    )


def _decimal(value: Any) -> Decimal:
    if value in (None, ""):
        return Decimal("0")
    result = Decimal(str(value))
    if not result.is_finite():
        raise ValueError("non-finite accounting decimal")
    return result


def _timestamp(value: Any) -> datetime:
    if isinstance(value, datetime):
        return _aware(value)
    text = str(value or "")
    if not text:
        raise ValueError("accounting timestamp is missing")
    if text.isdigit():
        return datetime.fromtimestamp(int(text) / 1000, tz=timezone.utc)
    return _aware(datetime.fromisoformat(text.replace("Z", "+00:00")))


def _aware(value: datetime) -> datetime:
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)
