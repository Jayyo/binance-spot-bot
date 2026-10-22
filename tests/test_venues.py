from decimal import Decimal as D

import pytest

from spotbot.binance import Rejected
from spotbot.filters import SymbolRules
from spotbot.ledger import Store
from spotbot.model import OrderUpdate, Side, Status
from spotbot.venues import PAGE, BinanceVenue, Quote, SimVenue

RULES = SymbolRules(
    symbol="BTCUSDT", base_asset="BTC", quote_asset="USDT", step=D("0.001"), max_qty=D(1)
)


class StubClient:
    mode = "testnet"

    def __init__(self, orders=(), trades=()):
        self.orders = list(orders)
        self.trades = list(trades)
        self.calls = []

    def now_ms(self):
        return 10**13

    def all_orders(self, symbol, *, start_time=None, end_time=None, order_id=None, limit=1000):
        self.calls.append(("all_orders", start_time, order_id))
        rows = [o for o in self.orders if (order_id is None or o["orderId"] >= order_id)]
        if start_time is not None:
            rows = [o for o in rows if start_time <= o["time"] <= end_time]
        return rows[:limit]

    def my_trades(self, symbol, *, order_id=None, from_id=None, limit=1000):
        rows = [
            t
            for t in self.trades
            if t["orderId"] == order_id and (from_id is None or t["id"] >= from_id)
        ]
        return rows[:limit]


def orders(n, *, with_id=None):
    out = [{"orderId": i, "clientOrderId": f"other-{i}", "time": 1000 + i} for i in range(1, n + 1)]
    if with_id:
        out.append({"orderId": n + 1, "clientOrderId": with_id, "time": 1000 + n + 1})
    return out


def test_order_history_is_read_past_a_full_page():
    venue = BinanceVenue(StubClient(orders(PAGE + 10, with_id="mine")), RULES)
    assert venue.seen_since("mine", 0) is True
    venue = BinanceVenue(StubClient(orders(PAGE + 10)), RULES)
    assert venue.seen_since("mine", 0) is False  # the whole window was read


def test_order_history_that_never_ends_is_inconclusive():
    venue = BinanceVenue(StubClient(orders(PAGE * 25)), RULES)
    assert venue.seen_since("mine", 0) is None


def test_fills_follow_my_trades_pages():
    trades = [
        {"id": i, "orderId": 7, "price": "100", "qty": "0.001", "quoteQty": "0.1", "commission": "0",
         "commissionAsset": "USDT", "time": 1, "isBuyer": True}
        for i in range(1, PAGE + 6)
    ]  # fmt: skip
    venue = BinanceVenue(StubClient(trades=trades), RULES)
    update = OrderUpdate("c", 7, Status.FILLED, D("1.005"), D("100.5"), 1)
    assert len(venue.order_fills(update)) == PAGE + 5


def test_paper_buy_respects_the_maximum_quantity():
    quote = Quote(D(100), D(100), 1)
    venue = SimVenue("paper", RULES, quote, Store())
    with pytest.raises(Rejected, match="above the maximum"):
        venue.place_market(Side.BUY, "c1", quote_qty=D(500))  # 5 BTC > max 1
    assert venue.place_market(Side.BUY, "c2", quote_qty=D(50)).executed_qty == D("0.5")


def test_incompatible_lot_steps_are_refused():
    info = {
        "symbol": "X", "baseAsset": "X", "quoteAsset": "USDT",
        "filters": [{"filterType": "LOT_SIZE", "minQty": "0.03", "maxQty": "10", "stepSize": "0.03"},
                    {"filterType": "MARKET_LOT_SIZE", "minQty": "0.02", "maxQty": "10", "stepSize": "0.02"}],
    }  # fmt: skip
    with pytest.raises(ValueError, match="not multiples"):
        SymbolRules.from_exchange_info(info)


def test_an_order_answer_missing_fields_is_an_unknown_outcome():
    from spotbot.binance import Ambiguous

    class Client(StubClient):
        def new_market_order(self, *a, **k):
            return {"orderId": 5, "status": "FILLED"}  # no clientOrderId, no quantities

    venue = BinanceVenue(Client(), RULES)
    with pytest.raises(Ambiguous):
        venue.place_market(Side.BUY, "c1", quote_qty=D(10))


def test_an_order_answer_without_amounts_is_not_read_as_nothing_executed():
    from spotbot.binance import Ambiguous

    class Client(StubClient):
        def new_market_order(self, *a, **k):
            return {"side": "BUY", "clientOrderId": "c1", "orderId": 123, "status": "FILLED"}

    with pytest.raises(Ambiguous):
        BinanceVenue(Client(), RULES).place_market(Side.BUY, "c1", quote_qty=D(10))
