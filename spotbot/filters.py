"""Order-size rules from /api/v3/exchangeInfo, applied with Decimal arithmetic."""

from __future__ import annotations

from dataclasses import dataclass
from decimal import ROUND_DOWN, ROUND_UP, Decimal
from typing import Any

from .model import dec


def floor_to_step(value: Decimal, step: Decimal) -> Decimal:
    if step <= 0:
        return value
    return (value / step).to_integral_value(rounding=ROUND_DOWN) * step


def ceil_to_step(value: Decimal, step: Decimal) -> Decimal:
    if step <= 0:
        return value
    return (value / step).to_integral_value(rounding=ROUND_UP) * step


@dataclass(frozen=True, slots=True)
class SymbolRules:
    symbol: str
    base_asset: str
    quote_asset: str
    status: str = "TRADING"
    spot_allowed: bool = True
    quote_precision: int = 8
    quote_market_allowed: bool = True  # quoteOrderQtyMarketAllowed
    min_qty: Decimal = Decimal(0)
    max_qty: Decimal = Decimal(0)  # 0 = no limit
    step: Decimal = Decimal(0)
    min_notional: Decimal = Decimal(0)
    min_notional_market: bool = True  # applyMinToMarket
    max_notional: Decimal = Decimal(0)  # 0 = no limit
    max_notional_market: bool = False  # applyMaxToMarket

    @classmethod
    def unrestricted(cls, symbol: str, base: str, quote: str) -> SymbolRules:
        """Rules for simulations that do not model exchange filters."""
        return cls(symbol=symbol, base_asset=base, quote_asset=quote)

    @classmethod
    def from_exchange_info(cls, info: dict[str, Any]) -> SymbolRules:
        filters = {f["filterType"]: f for f in info.get("filters", [])}
        # LOT_SIZE and MARKET_LOT_SIZE both apply to market orders; a zero there means "no limit".
        lots = [filters[k] for k in ("LOT_SIZE", "MARKET_LOT_SIZE") if k in filters]
        min_qty = max((dec(x.get("minQty", "0")) for x in lots), default=Decimal(0))
        steps = [dec(x.get("stepSize", "0")) for x in lots if dec(x.get("stepSize", "0")) > 0]
        step = max(steps, default=Decimal(0))
        if any(step % s for s in steps):
            raise ValueError(
                f"{info.get('symbol')}: lot steps {steps} are not multiples of each other"
            )
        maxes = [dec(x.get("maxQty", "0")) for x in lots]
        max_qty = min((v for v in maxes if v > 0), default=Decimal(0))

        min_notional = Decimal(0)
        min_market = True
        max_notional = Decimal(0)
        max_market = False
        if "NOTIONAL" in filters:
            f = filters["NOTIONAL"]
            min_notional = dec(f.get("minNotional", "0"))
            min_market = bool(f.get("applyMinToMarket", True))
            max_notional = dec(f.get("maxNotional", "0"))
            max_market = bool(f.get("applyMaxToMarket", False))
        elif "MIN_NOTIONAL" in filters:
            f = filters["MIN_NOTIONAL"]
            min_notional = dec(f.get("minNotional", "0"))
            min_market = bool(f.get("applyToMarket", True))

        return cls(
            symbol=info["symbol"],
            base_asset=info["baseAsset"],
            quote_asset=info["quoteAsset"],
            status=info.get("status", ""),
            spot_allowed=bool(info.get("isSpotTradingAllowed", True)),
            quote_precision=int(info.get("quoteAssetPrecision", info.get("quotePrecision", 8))),
            quote_market_allowed=bool(info.get("quoteOrderQtyMarketAllowed", False)),
            min_qty=min_qty,
            max_qty=max_qty,
            step=step,
            min_notional=min_notional,
            min_notional_market=min_market,
            max_notional=max_notional,
            max_notional_market=max_market,
        )

    def lots(self, qty: Decimal) -> Decimal:
        """`qty` rounded down to the lot step; 0 below the minimum lot (dust)."""
        q = floor_to_step(qty, self.step)
        return q if q > 0 and q >= self.min_qty else Decimal(0)

    def sell_qty(self, qty: Decimal) -> Decimal:
        """Largest sellable market quantity not above `qty`; 0 if it is dust."""
        q = floor_to_step(qty, self.step)
        if self.max_qty > 0:
            q = min(q, floor_to_step(self.max_qty, self.step))
        if q <= 0 or q < self.min_qty:
            return Decimal(0)
        return q

    def buy_quote(self, amount: Decimal) -> Decimal:
        """Quote amount rounded down to the precision Binance accepts for quoteOrderQty."""
        return floor_to_step(amount, Decimal(1).scaleb(-self.quote_precision))

    def notional_problem(self, notional: Decimal) -> str | None:
        """Why a market order of this notional would fail NOTIONAL/MIN_NOTIONAL, if it would."""
        if self.min_notional_market and self.min_notional > 0 and notional < self.min_notional:
            return f"order value {notional:.2f} is below the exchange minimum {self.min_notional}"
        if self.max_notional_market and self.max_notional > 0 and notional > self.max_notional:
            return f"order value {notional:.2f} is above the exchange maximum {self.max_notional}"
        return None

    def tradable_problem(self) -> str | None:
        if self.status != "TRADING":
            return f"{self.symbol} status is {self.status or 'unknown'}"
        if not self.spot_allowed:
            return f"{self.symbol} is not open for spot trading"
        return None
