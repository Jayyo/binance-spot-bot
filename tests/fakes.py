"""Test doubles: a venue whose failures can be scripted, and helpers to drive the engine."""

from __future__ import annotations

from dataclasses import dataclass, field, replace
from decimal import Decimal as D

from spotbot.binance import Ambiguous, Rejected
from spotbot.engine import Engine, EngineConfig
from spotbot.filters import SymbolRules
from spotbot.ledger import Store
from spotbot.model import Bar, Fill, OrderUpdate, Side, Status
from spotbot.risk import RiskConfig
from spotbot.strategy import RsiParams, RsiReversion
from spotbot.venues import Quote

H = 3_600_000
T0 = 1_735_689_600_000  # 2025-01-01 00:00 UTC

RULES = SymbolRules(
    symbol="BTCUSDT",
    base_asset="BTC",
    quote_asset="USDT",
    min_qty=D("0.00001"),
    step=D("0.00001"),
    min_notional=D(5),
)


@dataclass
class ScriptedVenue:
    """Fills market orders at the quote; failures and delays are opt-in per test."""

    quote: Quote
    rules: SymbolRules = RULES
    mode: str = "testnet"
    fee_rate: D = D("0.001")
    commission_asset: str | None = None  # None = base for buys, quote for sells
    free: dict[str, D] | None = None  # None = do not report balances
    ambiguous_next: int = 0  # raise Ambiguous on the next N placements...
    execute_when_ambiguous: bool = True  # ...after executing them anyway
    hidden_queries: int = 0  # answer "not found" this many times per order before telling the truth
    partial: D | None = None  # fill only this fraction, then EXPIRED
    reject_next: int = 0
    price_at_value: D | None = D(100)
    seen_answer: bool | None = None  # what seen_since reports for unknown orders
    open_next: bool = False  # next order fills half now and the rest when queried later
    complete_after: int = 2  # queries before such an order reports the rest
    orders: dict[str, OrderUpdate] = field(default_factory=dict)
    later: dict[str, OrderUpdate] = field(default_factory=dict)
    placements: list[str] = field(default_factory=list)
    queries: dict[str, int] = field(default_factory=dict)
    _trade: int = 0

    def place_market(self, side, client_id, *, quote_qty=None, qty=None):
        self.placements.append(client_id)
        if self.reject_next:
            self.reject_next -= 1
            raise Rejected("-2010 rejected", code=-2010)
        price = self.quote.ask if side is Side.BUY else self.quote.bid
        base = (
            (quote_qty / price).quantize(D("0.00001"), "ROUND_DOWN")
            if quote_qty is not None
            else qty
        )
        if self.partial is not None:
            base = (base * self.partial).quantize(D("0.00001"), "ROUND_DOWN")
        notional = base * price
        if side is Side.BUY:
            commission, asset = base * self.fee_rate, self.commission_asset or "BTC"
        else:
            commission, asset = notional * self.fee_rate, self.commission_asset or "USDT"
        self._trade += 1
        fills = [
            Fill(
                self._trade,
                len(self.orders) + 1,
                client_id,
                side,
                price,
                base,
                notional,
                commission,
                asset,
                self.quote.time,
            )
        ]
        status = Status.FILLED if self.partial is None else Status.EXPIRED
        update = OrderUpdate(
            client_id, len(self.orders) + 1, status, base, notional, self.quote.time, fills
        )
        if self.free is not None:
            sign = 1 if side is Side.BUY else -1
            self.free["BTC"] = (
                self.free.get("BTC", D(0)) + sign * base - (commission if asset == "BTC" else 0)
            )
            self.free["USDT"] = (
                self.free.get("USDT", D(0))
                - sign * notional
                - (commission if asset == "USDT" else 0)
            )
        if self.open_next:
            self.open_next = False
            half = (base / 2).quantize(D("0.00001"), "ROUND_DOWN")
            first = replace(
                fills[0], qty=half, quote_qty=half * price, commission=half * self.fee_rate
            )
            self._trade += 1
            rest = replace(
                fills[0],
                trade_id=self._trade,
                qty=base - half,
                quote_qty=(base - half) * price,
                commission=(base - half) * self.fee_rate,
            )
            self.later[client_id] = replace(update, fills=[first, rest])
            update = replace(
                update, status=Status.PARTIALLY_FILLED, executed_qty=half, fills=[first]
            )
        if self.ambiguous_next:
            self.ambiguous_next -= 1
            if self.execute_when_ambiguous:
                self.orders[client_id] = update
            raise Ambiguous("read timeout")
        self.orders[client_id] = update
        return update

    def query(self, client_id):
        n = self.queries.get(client_id, 0)
        self.queries[client_id] = n + 1
        if n < self.hidden_queries:
            return None
        if client_id in self.later and n + 1 >= self.complete_after:
            self.orders[client_id] = self.later.pop(client_id)
        return self.orders.get(client_id)

    def order_fills(self, update):
        return list(self.orders[update.client_id].fills)

    def seen_since(self, client_id, since_ms):
        if client_id in self.orders:
            return True
        return self.seen_answer

    def free_balance(self, asset):
        return None if self.free is None else self.free.get(asset, D(0))

    def price_at(self, time_ms):
        return self.price_at_value


def bar(i, close, *, open_=None, low=None, high=None, start=T0, step=H):
    o = close if open_ is None else open_
    lo = min(o, close) if low is None else low
    hi = max(o, close) if high is None else high
    t = start + i * step
    return Bar(t, float(o), float(hi), float(lo), float(close), 1.0, t + step - 1)


def make_engine(
    *,
    store=None,
    venue=None,
    budget=D(1000),
    risk=None,
    params=None,
    kill_switch=None,
    quote=None,
    interval="1h",
    account="testnet:1",
):
    quote = quote or Quote()
    store = store or Store()
    venue = venue or ScriptedVenue(quote)
    engine = Engine(
        EngineConfig(
            symbol="BTCUSDT",
            interval=interval,
            budget=budget,
            risk=risk or RiskConfig(),
            account=account,
            kill_switch=kill_switch,
        ),
        venue,
        store,
        RsiReversion(params or RsiParams()),
        quote,
    )
    return engine, venue, store, quote


def set_price(quote, price, t):
    p = D(str(price))
    quote.set(p, p, t)


def oversold_history(n=30, start=200.0, step=H):
    """Closes that leave RSI(14) far below 30."""
    return [bar(i - n, start - i, step=step) for i in range(n)]
