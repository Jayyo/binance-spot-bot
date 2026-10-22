"""Where orders go: a simulated venue (backtest, paper) or Binance (testnet, live).

The engine talks to both through the same small surface, so the order life
cycle, fee accounting and risk handling are the same code in every mode.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from decimal import ROUND_DOWN, Decimal
from typing import Any, Protocol

from .binance import Ambiguous, BinanceError, PublicClient, SignedClient
from .filters import SymbolRules, floor_to_step
from .ledger import Store
from .model import DAY_MS, Fill, OrderUpdate, Side, Status, dec

TINY = Decimal("1e-10")
PAGE = 1000
MAX_PAGES = 20


@dataclass(slots=True)
class Quote:
    """Latest top of book, shared by the runner/driver, the engine and simulated venues."""

    bid: Decimal = Decimal(0)
    ask: Decimal = Decimal(0)
    time: int = 0

    def set(self, bid: Decimal, ask: Decimal, time: int) -> None:
        self.bid, self.ask, self.time = bid, ask, time


class Venue(Protocol):
    mode: str
    rules: SymbolRules

    def place_market(
        self,
        side: Side,
        client_id: str,
        *,
        quote_qty: Decimal | None = None,
        qty: Decimal | None = None,
    ) -> OrderUpdate: ...

    def query(self, client_id: str) -> OrderUpdate | None: ...

    def order_fills(self, update: OrderUpdate) -> list[Fill]: ...

    def seen_since(self, client_id: str, since_ms: int) -> bool | None: ...

    def free_balance(self, asset: str) -> Decimal | None: ...

    def price_at(self, time_ms: int) -> Decimal | None: ...


class SimVenue:
    """Fills market orders immediately at the current quote, with fee and slippage.

    Buys pay the fee in the base asset and sells in the quote asset, which is
    what Binance does when fees are not paid in BNB.
    """

    def __init__(
        self,
        mode: str,
        rules: SymbolRules,
        quote: Quote,
        store: Store,
        *,
        fee_rate: Decimal = Decimal("0.001"),
        slippage: Decimal = Decimal(0),
        prices: PublicClient | None = None,
    ) -> None:
        self.mode = mode
        self.rules = rules
        self.quote = quote
        self.store = store
        self.fee_rate = fee_rate
        self.slippage = slippage
        self._prices = prices  # paper mode: used for start-of-day prices

    def _next(self, key: str) -> int:
        value = int(self.store.get_meta(key) or "0") + 1
        self.store.set_meta(key, str(value))
        return value

    def place_market(
        self,
        side: Side,
        client_id: str,
        *,
        quote_qty: Decimal | None = None,
        qty: Decimal | None = None,
    ) -> OrderUpdate:
        from .binance import Rejected

        if (quote_qty is None) == (qty is None):
            raise ValueError("pass exactly one of quote_qty / qty")
        q = self.quote
        if q.bid <= 0 or q.ask <= 0:
            raise Rejected("no market price yet")
        if side is Side.BUY:
            price = q.ask * (1 + self.slippage)
            if quote_qty is not None:
                base = floor_to_step(quote_qty / price, self.rules.step).quantize(TINY, ROUND_DOWN)
            else:
                assert qty is not None
                base = qty
        else:
            price = q.bid * (1 - self.slippage)
            assert qty is not None, "simulated sells take a base quantity"
            base = qty
        if base <= 0 or (self.rules.min_qty and base < self.rules.min_qty):
            raise Rejected(f"quantity {base} below the minimum", code=-1013)
        if self.rules.max_qty and base > self.rules.max_qty:
            raise Rejected(f"quantity {base} above the maximum {self.rules.max_qty}", code=-1013)
        notional = (base * price).quantize(TINY)
        problem = self.rules.notional_problem(notional)
        if problem:
            raise Rejected(problem, code=-1013)
        if side is Side.BUY:
            commission, asset = (base * self.fee_rate).quantize(TINY), self.rules.base_asset
        else:
            commission, asset = (notional * self.fee_rate).quantize(TINY), self.rules.quote_asset
        order_id = self._next("sim_order_seq")
        fill = Fill(
            trade_id=self._next("sim_trade_seq"),
            order_id=order_id,
            client_id=client_id,
            side=side,
            price=price.quantize(TINY),
            qty=base,
            quote_qty=notional,
            commission=commission,
            commission_asset=asset,
            time=q.time,
        )
        update = OrderUpdate(
            client_id=client_id,
            order_id=order_id,
            status=Status.FILLED,
            executed_qty=base,
            cumulative_quote=notional,
            time=q.time,
            fills=[fill],
        )
        self.store.save_sim_order(client_id, _dump(update))
        return update

    def query(self, client_id: str) -> OrderUpdate | None:
        raw = self.store.load_sim_order(client_id)
        return _load(raw) if raw else None

    def order_fills(self, update: OrderUpdate) -> list[Fill]:
        return list(update.fills)

    def seen_since(self, client_id: str, since_ms: int) -> bool | None:
        return self.store.load_sim_order(client_id) is not None

    def free_balance(self, asset: str) -> Decimal | None:
        return None

    def price_at(self, time_ms: int) -> Decimal | None:
        if self._prices is None:
            # Backtest: the driver ticks the first bar of each day at its open.
            return self.quote.bid if self.quote.time >= time_ms else None
        return _open_price_at(self._prices, self.rules.symbol, time_ms)


def _open_price_at(client: PublicClient, symbol: str, time_ms: int) -> Decimal | None:
    try:
        rows = client.klines(symbol, "1m", start=time_ms, limit=1)
    except BinanceError:
        return None
    if rows and int(rows[0][0]) == time_ms:
        return dec(rows[0][1])
    return None


def _dump(u: OrderUpdate) -> str:
    return json.dumps(
        {
            "client_id": u.client_id,
            "order_id": u.order_id,
            "status": u.status.value,
            "executed_qty": str(u.executed_qty),
            "cumulative_quote": str(u.cumulative_quote),
            "time": u.time,
            "fills": [
                {
                    "trade_id": f.trade_id,
                    "side": f.side.value,
                    "price": str(f.price),
                    "qty": str(f.qty),
                    "quote_qty": str(f.quote_qty),
                    "commission": str(f.commission),
                    "commission_asset": f.commission_asset,
                    "time": f.time,
                }
                for f in u.fills
            ],
        }
    )


def _load(raw: str) -> OrderUpdate:
    d = json.loads(raw)
    fills = [
        Fill(
            trade_id=f["trade_id"],
            order_id=d["order_id"],
            client_id=d["client_id"],
            side=Side(f["side"]),
            price=dec(f["price"]),
            qty=dec(f["qty"]),
            quote_qty=dec(f["quote_qty"]),
            commission=dec(f["commission"]),
            commission_asset=f["commission_asset"],
            time=f["time"],
        )
        for f in d["fills"]
    ]
    return OrderUpdate(
        client_id=d["client_id"],
        order_id=d["order_id"],
        status=Status(d["status"]),
        executed_qty=dec(d["executed_qty"]),
        cumulative_quote=dec(d["cumulative_quote"]),
        time=d["time"],
        fills=fills,
    )


class BinanceVenue:
    """Spot Testnet or the live exchange, through a SignedClient."""

    def __init__(self, client: SignedClient, rules: SymbolRules) -> None:
        self.client = client
        self.mode = client.mode
        self.rules = rules

    def _side(self, raw: dict[str, Any]) -> Side:
        return Side(raw["side"])

    def _update(self, raw: dict[str, Any]) -> OrderUpdate:
        try:
            return self._parse_update(raw)
        except (KeyError, TypeError, ValueError, ArithmeticError) as exc:
            # An order answer we cannot read says nothing about what happened to the order.
            raise Ambiguous(f"unreadable order response ({exc.__class__.__name__})") from None

    def _parse_update(self, raw: dict[str, Any]) -> OrderUpdate:
        side = self._side(raw)
        when = int(raw.get("transactTime") or raw.get("updateTime") or raw.get("time") or 0)
        fills = [
            self._fill(
                trade_id=int(f["tradeId"]),
                order_id=int(raw["orderId"]),
                client_id=raw["clientOrderId"],
                side=side,
                price=dec(f["price"]),
                qty=dec(f["qty"]),
                quote_qty=dec(f["price"]) * dec(f["qty"]),
                commission=dec(f["commission"]),
                commission_asset=f["commissionAsset"],
                time=when,
            )
            for f in raw.get("fills", [])
        ]
        executed = dec(raw["executedQty"])  # no default: a missing amount is not "nothing executed"
        cumulative = dec(raw["cummulativeQuoteQty"])
        if executed < 0 or cumulative < 0 or not executed.is_finite():
            raise ValueError("negative or invalid executed amount")
        return OrderUpdate(
            client_id=raw["clientOrderId"],
            order_id=int(raw["orderId"]),
            status=Status(raw["status"]),
            executed_qty=executed,
            cumulative_quote=cumulative,
            time=when,
            fills=fills,
        )

    def _fill(self, **kw: Any) -> Fill:
        asset = kw["commission_asset"]
        valued: Decimal | None = None
        if kw["commission"] and asset not in (self.rules.base_asset, self.rules.quote_asset):
            valued = self._value_in_quote(asset, kw["commission"])
        return Fill(commission_quote=valued, **kw)

    def _value_in_quote(self, asset: str, amount: Decimal) -> Decimal | None:
        try:
            price = dec(self.client.ticker_price(asset + self.rules.quote_asset)["price"])
        except (BinanceError, KeyError):
            return None
        return amount * price

    def place_market(
        self,
        side: Side,
        client_id: str,
        *,
        quote_qty: Decimal | None = None,
        qty: Decimal | None = None,
    ) -> OrderUpdate:
        raw = self.client.new_market_order(
            self.rules.symbol,
            side.value,
            client_id,
            quote_qty=None if quote_qty is None else format(quote_qty, "f"),
            qty=None if qty is None else format(qty, "f"),
        )
        return self._update(raw)

    def query(self, client_id: str) -> OrderUpdate | None:
        raw = self.client.query_order(self.rules.symbol, client_id)
        return None if raw is None else self._update(raw)

    def order_fills(self, update: OrderUpdate) -> list[Fill]:
        """Every trade of one order, following myTrades pages until a short page."""
        trades: list[dict[str, Any]] = []
        from_id = None
        for _ in range(MAX_PAGES):
            page = self.client.my_trades(
                self.rules.symbol, order_id=update.order_id, from_id=from_id, limit=PAGE
            )
            trades += page
            if len(page) < PAGE:
                break
            from_id = int(page[-1]["id"]) + 1
        else:
            raise BinanceError(f"order {update.order_id} has more than {MAX_PAGES * PAGE} trades")
        return [
            self._fill(
                trade_id=int(t["id"]),
                order_id=int(t["orderId"]),
                client_id=update.client_id,
                side=Side.BUY if t["isBuyer"] else Side.SELL,
                price=dec(t["price"]),
                qty=dec(t["qty"]),
                quote_qty=dec(t["quoteQty"]),
                commission=dec(t["commission"]),
                commission_asset=t["commissionAsset"],
                time=int(t["time"]),
            )
            for t in trades
        ]

    def seen_since(self, client_id: str, since_ms: int) -> bool | None:
        """Whether the order exists in the account's order history since `since_ms`.

        False only when the whole window was read; a full last page or an error means
        "cannot tell" (None), and the caller keeps waiting.
        """
        end = min(since_ms + DAY_MS - 1, self.client.now_ms())
        try:
            page = self.client.all_orders(
                self.rules.symbol, start_time=since_ms, end_time=end, limit=PAGE
            )
            for _ in range(MAX_PAGES):
                if any(o.get("clientOrderId") == client_id for o in page):
                    return True
                if len(page) < PAGE:
                    return False
                last = max(int(o["orderId"]) for o in page)
                page = [
                    o
                    for o in self.client.all_orders(
                        self.rules.symbol, order_id=last + 1, limit=PAGE
                    )
                    if int(o.get("time", 0)) <= end
                ]
        except BinanceError:
            return None
        return None

    def free_balance(self, asset: str) -> Decimal | None:
        for b in self.client.account().get("balances", []):
            if b["asset"] == asset:
                return dec(b["free"])
        return Decimal(0)

    def price_at(self, time_ms: int) -> Decimal | None:
        return _open_price_at(self.client, self.rules.symbol, time_ms)
