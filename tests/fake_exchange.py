"""An in-process stand-in for Binance Spot, reached through the real HTTP client.

It checks HMAC signatures and timestamps itself (independently of spotbot's
signer), enforces LOT_SIZE and NOTIONAL, fills market orders at the book and
keeps balances, orders and trades. This is a local integration test, not a
substitute for running against the real Spot Testnet.
"""

from __future__ import annotations

import hashlib
import hmac
import json
from decimal import ROUND_DOWN, Decimal
from pathlib import Path
from urllib.parse import parse_qsl, unquote, urlsplit

import requests

from tests.httpfake import session_with

INFO = json.loads((Path(__file__).parent / "fixtures" / "btcusdt_exchange_info.json").read_text())
STEP = Decimal("0.00001")
MIN_NOTIONAL = Decimal(5)
M = 60_000


class FakeBinance:
    def __init__(
        self, *, api_key: str, secret: str, now: int, closes: list[float], usdt="10000", btc="0"
    ):
        self.api_key, self.secret = api_key, secret
        self.now = now
        self.start = (
            now - len(closes) * M
        )  # closes[i] belongs to the minute starting at start + i*M
        self.closes = list(closes)
        self.spread = Decimal("0.01")
        self.balances = {"USDT": Decimal(usdt), "BTC": Decimal(btc)}
        self.orders: dict[str, dict] = {}
        self.trades: list[dict] = []
        self.posts = 0
        self.drop_next_response = False
        self.hide_next_queries = 0
        # Shape of GET /sapi/v1/account/apiRestrictions for a trade-only, IP-restricted key.
        self.permissions = {
            "ipRestrict": True,
            "createTime": 1700000000000,
            "enableReading": True,
            "enableWithdrawals": False,
            "enableInternalTransfer": False,
            "enableMargin": False,
            "enableFutures": False,
            "permitsUniversalTransfer": False,
            "enableVanillaOptions": False,
            "enableFixApiTrade": False,
            "enableFixReadOnly": False,
            "enableSpotAndMarginTrading": True,
            "enablePortfolioMarginTrading": False,
        }
        self.session, self.transport = session_with(self.handle)

    # market --------------------------------------------------------------
    def price(self) -> Decimal:
        index = min((self.now - self.start) // M, len(self.closes) - 1)
        return Decimal(str(self.closes[index]))

    def kline_rows(self):
        rows = []
        for i, c in enumerate(self.closes):
            t = self.start + i * M
            if t > self.now:
                break
            o = self.closes[i - 1] if i else c
            rows.append(
                [
                    t,
                    str(o),
                    str(max(o, c)),
                    str(min(o, c)),
                    str(c),
                    "1",
                    t + M - 1,
                    "1",
                    1,
                    "0",
                    "0",
                    "0",
                ]
            )
        return rows

    # http ------------------------------------------------------------------
    def handle(self, request: requests.PreparedRequest):
        url = urlsplit(request.url)
        params = dict(parse_qsl(url.query))
        route = (request.method, url.path)
        public = {
            ("GET", "/api/v3/time"): lambda: {"serverTime": self.now},
            ("GET", "/api/v3/exchangeInfo"): lambda: {"symbols": [INFO]},
            ("GET", "/api/v3/ticker/bookTicker"): lambda: {
                "symbol": "BTCUSDT",
                "bidPrice": str(self.price() - self.spread),
                "askPrice": str(self.price() + self.spread),
            },
            ("GET", "/api/v3/klines"): lambda: self._klines(params),
        }
        if route in public:
            assert "X-MBX-APIKEY" not in request.headers, "public call carried the API key"
            return 200, public[route]()
        problem = self._auth(request, url.query, params)
        if problem:
            return problem
        signed = {
            ("GET", "/api/v3/account"): self._account,
            ("POST", "/api/v3/order"): self._new_order,
            ("GET", "/api/v3/order"): self._query_order,
            ("GET", "/api/v3/myTrades"): self._my_trades,
            ("GET", "/api/v3/allOrders"): self._all_orders,
            ("GET", "/sapi/v1/account/apiRestrictions"): lambda p: (200, self.permissions),
        }
        return signed[route](params)

    def _auth(self, request, raw_query, params):
        if request.headers.get("X-MBX-APIKEY") != self.api_key:
            return 401, {"code": -2015, "msg": "Invalid API-key, IP, or permissions for action."}
        payload, _, sig = raw_query.rpartition("&signature=")
        want = hmac.new(self.secret.encode(), payload.encode(), hashlib.sha256).hexdigest()
        if not hmac.compare_digest(unquote(sig).lower(), want):
            return 400, {"code": -1022, "msg": "Signature for this request is not valid."}
        if abs(int(params["timestamp"]) - self.now) > int(params.get("recvWindow", 5000)):
            return 400, {
                "code": -1021,
                "msg": "Timestamp for this request is outside of the recvWindow.",
            }
        return None

    def _klines(self, p):
        rows = self.kline_rows()
        if "startTime" in p:
            rows = [r for r in rows if r[0] >= int(p["startTime"])]
        return (
            rows[: int(p.get("limit", 500))]
            if "startTime" in p
            else rows[-int(p.get("limit", 500)) :]
        )

    def _account(self, p):
        balances = [{"asset": a, "free": str(v), "locked": "0"} for a, v in self.balances.items()]
        return 200, {"canTrade": True, "permissions": ["SPOT"], "uid": 42, "balances": balances}

    def _new_order(self, p):
        self.posts += 1
        side, cid = p["side"], p["newClientOrderId"]
        if p.get("type") != "MARKET":
            return 400, {"code": -1116, "msg": "Invalid orderType."}
        bid, ask = self.price() - self.spread, self.price() + self.spread
        if side == "BUY":
            if "quoteOrderQty" in p:
                qty = (Decimal(p["quoteOrderQty"]) / ask).quantize(STEP, ROUND_DOWN)
            else:
                qty = Decimal(p["quantity"])
            price = ask
        else:
            qty, price = Decimal(p["quantity"]), bid
        if qty % STEP or qty < STEP:
            return 400, {"code": -1013, "msg": "Filter failure: LOT_SIZE"}
        notional = qty * price
        if notional < MIN_NOTIONAL:
            return 400, {"code": -1013, "msg": "Filter failure: NOTIONAL"}
        if side == "BUY":
            if notional > self.balances["USDT"]:
                return 400, {
                    "code": -2010,
                    "msg": "Account has insufficient balance for requested action.",
                }
            fee, fee_asset = qty * Decimal("0.001"), "BTC"
            self.balances["USDT"] -= notional
            self.balances["BTC"] += qty - fee
        else:
            if qty > self.balances["BTC"]:
                return 400, {
                    "code": -2010,
                    "msg": "Account has insufficient balance for requested action.",
                }
            fee, fee_asset = notional * Decimal("0.001"), "USDT"
            self.balances["BTC"] -= qty
            self.balances["USDT"] += notional - fee
        order_id = len(self.orders) + 1
        trade = {
            "symbol": "BTCUSDT", "id": len(self.trades) + 1, "orderId": order_id, "price": str(price),
            "qty": str(qty), "quoteQty": str(notional), "commission": str(fee), "commissionAsset": fee_asset,
            "time": self.now, "isBuyer": side == "BUY",
        }  # fmt: skip
        self.trades.append(trade)
        order = {
            "symbol": "BTCUSDT", "orderId": order_id, "clientOrderId": cid, "transactTime": self.now,
            "updateTime": self.now, "status": "FILLED", "side": side, "type": "MARKET",
            "executedQty": str(qty), "cummulativeQuoteQty": str(notional),
        }  # fmt: skip
        self.orders[cid] = order
        if self.drop_next_response:
            self.drop_next_response = False
            raise requests.exceptions.ReadTimeout("response lost")
        fills = [{"price": trade["price"], "qty": trade["qty"], "commission": trade["commission"],
                  "commissionAsset": fee_asset, "tradeId": trade["id"]}]  # fmt: skip
        return 200, {**order, "fills": fills}

    def _query_order(self, p):
        if self.hide_next_queries:
            self.hide_next_queries -= 1
            return 400, {"code": -2013, "msg": "Order does not exist."}
        order = self.orders.get(p["origClientOrderId"])
        if order is None:
            return 400, {"code": -2013, "msg": "Order does not exist."}
        return 200, {k: v for k, v in order.items() if k != "transactTime"} | {
            "time": order["transactTime"]
        }

    def _my_trades(self, p):
        return 200, [t for t in self.trades if t["orderId"] == int(p["orderId"])]

    def _all_orders(self, p):
        return 200, [o for o in self.orders.values() if o["transactTime"] >= int(p["startTime"])]
