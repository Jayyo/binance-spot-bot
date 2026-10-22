"""Plain data types shared by the backtester, the paper venue and the live venue."""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from datetime import UTC, datetime
from decimal import Decimal
from enum import StrEnum

INTERVAL_MS = {
    "1m": 60_000,
    "3m": 180_000,
    "5m": 300_000,
    "15m": 900_000,
    "30m": 1_800_000,
    "1h": 3_600_000,
    "2h": 7_200_000,
    "4h": 14_400_000,
    "6h": 21_600_000,
    "8h": 28_800_000,
    "12h": 43_200_000,
    "1d": 86_400_000,
}
DAY_MS = 86_400_000


def interval_ms(interval: str) -> int:
    try:
        return INTERVAL_MS[interval]
    except KeyError:
        raise ValueError(
            f"unsupported interval {interval!r}; use one of {', '.join(INTERVAL_MS)}"
        ) from None


def utc_day(ms: int) -> str:
    return datetime.fromtimestamp(ms / 1000, UTC).strftime("%Y-%m-%d")


def day_start_ms(ms: int) -> int:
    return ms - ms % DAY_MS


def fmt_time(ms: int) -> str:
    return datetime.fromtimestamp(ms / 1000, UTC).strftime("%Y-%m-%d %H:%M")


def fmt_price(value: Decimal | float) -> str:
    """Price with as many decimals as its size needs: 108,000.12 / 2.4567 / 0.00001234."""
    x = float(value)
    if x == 0:
        return "0"
    decimals = min(8, max(2, 4 - math.floor(math.log10(abs(x)))))
    return f"{x:,.{decimals}f}"


def fmt_qty(value: Decimal) -> str:
    return f"{value.normalize():f}"


def dec(value: object) -> Decimal:
    """Decimal from str/int/float without binary float noise."""
    if isinstance(value, Decimal):
        return value
    if isinstance(value, float):
        return Decimal(repr(value))
    return Decimal(str(value))


@dataclass(frozen=True, slots=True)
class Bar:
    open_time: int  # ms, UTC
    open: float
    high: float
    low: float
    close: float
    volume: float
    close_time: int  # ms, Binance style: open_time + interval - 1


class Side(StrEnum):
    BUY = "BUY"
    SELL = "SELL"


class Status(StrEnum):
    # Local states: written before / instead of an exchange answer.
    PENDING_SEND = "PENDING_SEND"
    UNKNOWN = "UNKNOWN"
    NOT_FOUND = "NOT_FOUND"
    # Exchange states.
    NEW = "NEW"
    PARTIALLY_FILLED = "PARTIALLY_FILLED"
    FILLED = "FILLED"
    CANCELED = "CANCELED"
    PENDING_CANCEL = "PENDING_CANCEL"
    REJECTED = "REJECTED"
    EXPIRED = "EXPIRED"
    EXPIRED_IN_MATCH = "EXPIRED_IN_MATCH"

    @property
    def terminal(self) -> bool:
        return self in TERMINAL


TERMINAL = frozenset(
    {
        Status.NOT_FOUND,
        Status.FILLED,
        Status.CANCELED,
        Status.REJECTED,
        Status.EXPIRED,
        Status.EXPIRED_IN_MATCH,
    }
)


@dataclass(frozen=True, slots=True)
class Fill:
    trade_id: int
    order_id: int
    client_id: str
    side: Side
    price: Decimal
    qty: Decimal
    quote_qty: Decimal
    commission: Decimal
    commission_asset: str
    time: int
    # Commission paid in a third asset (BNB), valued in the quote asset at the ticker
    # price when the fill was recorded. None when no price was available.
    commission_quote: Decimal | None = None


@dataclass(slots=True)
class OrderUpdate:
    """What the venue knows about one order."""

    client_id: str
    order_id: int
    status: Status
    executed_qty: Decimal
    cumulative_quote: Decimal
    time: int
    fills: list[Fill] = field(default_factory=list)
