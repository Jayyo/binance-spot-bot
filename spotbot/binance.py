"""Minimal Binance Spot REST client, written against the official API docs.

Only the endpoints this bot needs. Two rules matter more than the rest:

- Signed requests go to exactly one fixed origin per mode, never follow
  redirects, and are the only requests that carry the API key header.
- A failed POST /api/v3/order is never retried here. If the outcome is unknown
  (timeout, 5xx, -1006/-1007) the caller gets `Ambiguous` and must ask the
  exchange what happened before doing anything else.
"""

from __future__ import annotations

import logging
import time
from typing import Any
from urllib.parse import quote, urlencode

import requests

from .keys import Credentials

log = logging.getLogger(__name__)

LIVE = "https://api.binance.com"
TESTNET = "https://testnet.binance.vision"
MARKET_DATA = "https://data-api.binance.vision"  # public market data only, no keys

SIGNED_ORIGINS = {"live": LIVE, "testnet": TESTNET}
BUILD_ERRORS = (
    requests.exceptions.InvalidHeader,
    requests.exceptions.InvalidURL,
    requests.exceptions.MissingSchema,
    requests.exceptions.InvalidSchema,
)
UNKNOWN_STATUS_CODES = {-1006, -1007}  # "execution status unknown"
NO_SUCH_ORDER = -2013


class BinanceError(Exception):
    def __init__(self, message: str, *, code: int | None = None, http_status: int | None = None):
        super().__init__(message)
        self.code = code
        self.http_status = http_status


class Rejected(BinanceError):
    """The exchange answered and did not execute the request."""


class RateLimited(Rejected):
    def __init__(self, message: str, retry_after: float, http_status: int):
        super().__init__(message, http_status=http_status)
        self.retry_after = retry_after


class NotSent(BinanceError):
    """The request never reached the exchange (connection could not be opened)."""


class Ambiguous(BinanceError):
    """The request may or may not have been executed."""


def _hint(code: int | None) -> str:
    return {
        -2015: " (wrong key, key not allowed from this IP, or missing permission)",
        -2014: " (API key format is wrong)",
        -1021: " (clock drift: timestamp outside recvWindow)",
        -1022: " (signature rejected: check the secret / private key matches the API key)",
        -2010: " (order rejected by the matching engine)",
        -1013: " (order failed an exchange filter)",
    }.get(code or 0, "")


class PublicClient:
    def __init__(self, origin: str, session: requests.Session | None = None, timeout: float = 10.0):
        if not origin.startswith("https://"):
            raise ValueError("only https origins are allowed")
        self.origin = origin
        self.session = session or requests.Session()
        # Ignore proxy variables and ~/.netrc so nothing outside our control adds headers.
        self.session.trust_env = False
        self.timeout = timeout
        self._blocked_until = 0.0
        self.offset_ms = 0
        self.offset_error_ms = 0

    def sync_time(self) -> int:
        """Estimate the server clock offset from the midpoint of a /time round trip."""
        t0 = time.time() * 1000
        server = self.server_time()
        t1 = time.time() * 1000
        self.offset_ms = int(server - (t0 + t1) / 2)
        self.offset_error_ms = int((t1 - t0) / 2)
        return self.offset_ms

    def now_ms(self) -> int:
        """Current time on the exchange's clock (local clock plus measured offset)."""
        return int(time.time() * 1000) + self.offset_ms

    def _send(self, method: str, url: str, headers: dict[str, str] | None = None) -> Any:
        now = time.monotonic()
        if now < self._blocked_until:
            raise RateLimited(
                f"rate limited for another {self._blocked_until - now:.0f}s",
                self._blocked_until - now,
                429,
            )
        try:
            resp = self.session.request(
                method, url, headers=headers or {}, timeout=self.timeout, allow_redirects=False
            )
        except requests.exceptions.ConnectTimeout as exc:
            raise NotSent(f"could not connect: {exc.__class__.__name__}") from None
        except requests.exceptions.ConnectionError as exc:
            if _never_connected(exc):
                raise NotSent(f"could not connect: {exc.__class__.__name__}") from None
            raise Ambiguous(f"connection dropped: {exc.__class__.__name__}") from None
        except requests.exceptions.Timeout as exc:
            raise Ambiguous(f"no response: {exc.__class__.__name__}") from None
        except BUILD_ERRORS as exc:
            # The request was never sent (bad header value, bad URL). The message can quote
            # the header, i.e. the API key, so only the type is kept.
            raise NotSent(f"request could not be built: {exc.__class__.__name__}") from None
        except requests.exceptions.RequestException as exc:
            # Anything else may have happened after the exchange got the request, e.g. the
            # response body broke off mid-transfer.
            raise Ambiguous(f"request failed: {exc.__class__.__name__}") from None
        return self._decode(resp)

    def _decode(self, resp: requests.Response) -> Any:
        status = resp.status_code
        if 300 <= status < 400:
            raise Rejected(
                f"unexpected redirect ({status}); refusing to follow", http_status=status
            )
        if status in (418, 429):
            try:
                retry = max(1.0, float(resp.headers.get("Retry-After", "60")))
            except ValueError:
                retry = 60.0
            self._blocked_until = time.monotonic() + retry
            raise RateLimited(f"HTTP {status}: backing off {retry:.0f}s", retry, status)
        try:
            body = resp.json()
        except ValueError:
            body = None
        code = body.get("code") if isinstance(body, dict) else None
        msg = body.get("msg", "") if isinstance(body, dict) else (resp.text or "")[:200]
        if status >= 500 or code in UNKNOWN_STATUS_CODES:
            raise Ambiguous(
                f"HTTP {status} {code or ''} {msg}".strip(), code=code, http_status=status
            )
        if status >= 400:
            raise Rejected(f"{code} {msg}{_hint(code)}", code=code, http_status=status)
        if body is None:
            # A success status without a readable body: whatever was asked may have happened.
            raise Ambiguous(f"HTTP {status} with an unreadable body", http_status=status)
        return body

    def get(self, path: str, params: dict[str, Any] | None = None) -> Any:
        query = urlencode(params or {})
        return self._send("GET", f"{self.origin}{path}" + (f"?{query}" if query else ""))

    # public endpoints
    def server_time(self) -> int:
        return int(self.get("/api/v3/time")["serverTime"])

    def exchange_info(self, symbol: str) -> dict[str, Any]:
        info = self.get("/api/v3/exchangeInfo", {"symbol": symbol})
        symbols = info.get("symbols") or []
        if not symbols:
            raise BinanceError(f"unknown symbol {symbol}")
        return symbols[0]

    def klines(
        self,
        symbol: str,
        interval: str,
        *,
        start: int | None = None,
        end: int | None = None,
        limit: int = 500,
    ) -> list[list[Any]]:
        params: dict[str, Any] = {"symbol": symbol, "interval": interval, "limit": limit}
        if start is not None:
            params["startTime"] = start
        if end is not None:
            params["endTime"] = end
        return self.get("/api/v3/klines", params)

    def book_ticker(self, symbol: str) -> dict[str, Any]:
        return self.get("/api/v3/ticker/bookTicker", {"symbol": symbol})

    def ticker_price(self, symbol: str) -> dict[str, Any]:
        return self.get("/api/v3/ticker/price", {"symbol": symbol})


def _never_connected(exc: requests.exceptions.ConnectionError) -> bool:
    text = repr(exc)
    return any(
        s in text for s in ("NewConnectionError", "NameResolutionError", "Failed to establish")
    )


class SignedClient(PublicClient):
    """Client for TRADE / USER_DATA endpoints. The origin is fixed by the mode."""

    def __init__(
        self,
        credentials: Credentials,
        session: requests.Session | None = None,
        timeout: float = 10.0,
        recv_window: int = 5000,
    ):
        super().__init__(SIGNED_ORIGINS[credentials.mode], session=session, timeout=timeout)
        self.mode = credentials.mode
        self._creds = credentials
        self.recv_window = recv_window

    @property
    def fingerprint(self) -> str:
        return self._creds.fingerprint

    def signed(self, method: str, path: str, params: dict[str, Any] | None = None) -> Any:
        params = {k: v for k, v in (params or {}).items() if v is not None}
        params["recvWindow"] = self.recv_window
        params["timestamp"] = self.now_ms()
        payload = urlencode(params)
        signature = self._creds.signer.sign(payload.encode("ascii"))
        url = f"{self.origin}{path}?{payload}&signature={quote(signature, safe='')}"
        return self._send(method, url, headers={"X-MBX-APIKEY": self._creds.api_key.reveal()})

    # USER_DATA / TRADE endpoints
    def account(self) -> dict[str, Any]:
        return self.signed("GET", "/api/v3/account", {"omitZeroBalances": "true"})

    def api_restrictions(self) -> dict[str, Any]:
        if self.mode != "live":
            raise BinanceError("key permissions can only be read on the live exchange")
        return self.signed("GET", "/sapi/v1/account/apiRestrictions")

    def commission(self, symbol: str) -> dict[str, Any]:
        return self.signed("GET", "/api/v3/account/commission", {"symbol": symbol})

    def new_market_order(
        self,
        symbol: str,
        side: str,
        client_id: str,
        *,
        quote_qty: str | None = None,
        qty: str | None = None,
    ) -> dict[str, Any]:
        if (quote_qty is None) == (qty is None):
            raise ValueError("pass exactly one of quote_qty / qty")
        return self.signed(
            "POST",
            "/api/v3/order",
            {
                "symbol": symbol,
                "side": side,
                "type": "MARKET",
                "quoteOrderQty": quote_qty,
                "quantity": qty,
                "newClientOrderId": client_id,
                "newOrderRespType": "FULL",
            },
        )

    def query_order(self, symbol: str, client_id: str) -> dict[str, Any] | None:
        try:
            return self.signed(
                "GET", "/api/v3/order", {"symbol": symbol, "origClientOrderId": client_id}
            )
        except Rejected as exc:
            if exc.code == NO_SUCH_ORDER:
                return None
            raise

    def all_orders(
        self,
        symbol: str,
        *,
        start_time: int | None = None,
        end_time: int | None = None,
        order_id: int | None = None,
        limit: int = 1000,
    ) -> list[dict[str, Any]]:
        return self.signed(
            "GET",
            "/api/v3/allOrders",
            {
                "symbol": symbol,
                "startTime": start_time,
                "endTime": end_time,
                "orderId": order_id,
                "limit": limit,
            },
        )

    def my_trades(
        self,
        symbol: str,
        *,
        order_id: int | None = None,
        from_id: int | None = None,
        limit: int = 1000,
    ) -> list[dict[str, Any]]:
        return self.signed(
            "GET",
            "/api/v3/myTrades",
            {"symbol": symbol, "orderId": order_id, "fromId": from_id, "limit": limit},
        )
