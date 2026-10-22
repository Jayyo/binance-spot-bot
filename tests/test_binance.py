import base64
from urllib.parse import unquote

import pytest
import requests
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from spotbot import binance
from spotbot.binance import Ambiguous, NotSent, PublicClient, RateLimited, Rejected, SignedClient
from spotbot.keys import Credentials, HmacSigner, KeyPairSigner, Secret
from tests.httpfake import path, query, session_with

# Example key pair from Binance's REST API documentation (SIGNED endpoint examples, HMAC).
DOC_KEY = "vmPUZE6mv9SD5VNHk4HlWFsOr6aKE2zvsw0MuIgwCIPy6utIco14y7Ju91duEh8A"  # gitleaks:allow
DOC_SECRET = "NhqPtmdSJYdKjVHjA7PZj4Mge3R5YNiP1e3UZjInClVN65XAbvqqM6A7H5fATj0j"  # gitleaks:allow
DOC_SIGNATURE = "c8db56825ae71d6d79447849e617115f4a920fa2acdcab2b053c4b2838bd6b71"

# RFC 8032, section 7.1, TEST 2 (one-byte message 0x72).
RFC_SEED = "4ccd089b28ff96da9db6c346ec114e0f5b8a319f35aba624da8cf6ed4fb8a6fb"
RFC_SIG = (
    "92a009a9f0d4cab8720e820b5f642540a2b27b5416503f8fb3762223ebdb69da"
    "085ac1e43e15996e458f3613d0f11d8c387b2eaeb4302aeeb00d291612bb0c00"
)


def hmac_creds(mode="live"):
    return Credentials(mode, Secret(DOC_KEY), HmacSigner(Secret(DOC_SECRET)), "test")


def test_signed_request_matches_the_documented_example_byte_for_byte():
    session, transport = session_with(lambda r: (200, {"orderId": 1}))
    client = SignedClient(hmac_creds(), session=session)
    client.now_ms = lambda: 1499827319559
    client.signed(
        "POST",
        "/api/v3/order",
        {
            "symbol": "LTCBTC",
            "side": "BUY",
            "type": "LIMIT",
            "timeInForce": "GTC",
            "quantity": "1",
            "price": "0.1",
        },
    )
    (req,) = transport.sent
    assert req.method == "POST"
    assert req.url == (
        "https://api.binance.com/api/v3/order?symbol=LTCBTC&side=BUY&type=LIMIT&timeInForce=GTC"
        f"&quantity=1&price=0.1&recvWindow=5000&timestamp=1499827319559&signature={DOC_SIGNATURE}"
    )
    assert req.headers["X-MBX-APIKEY"] == DOC_KEY


def test_ed25519_signer_matches_rfc8032_and_survives_url_encoding():
    key = Ed25519PrivateKey.from_private_bytes(bytes.fromhex(RFC_SEED))
    signer = KeyPairSigner(key)
    assert signer.kind == "ed25519"
    assert base64.b64decode(signer.sign(b"\x72")).hex() == RFC_SIG

    session, transport = session_with(lambda r: (200, {"balances": []}))
    client = SignedClient(Credentials("testnet", Secret("k" * 64), signer, "test"), session=session)
    client.account()
    url = transport.sent[0].url
    assert url.startswith("https://testnet.binance.vision/api/v3/account?")
    payload, sig = url.split("?", 1)[1].rsplit("&signature=", 1)
    assert "+" not in sig and "/" not in sig and "=" not in sig  # base64 is percent-encoded
    key.public_key().verify(base64.b64decode(unquote(sig)), payload.encode())


def test_public_requests_never_carry_the_api_key():
    session, transport = session_with(lambda r: (200, {"serverTime": 1}))
    PublicClient(binance.MARKET_DATA, session=session).server_time()
    signed = SignedClient(hmac_creds(), session=session)
    signed.server_time()
    assert all("X-MBX-APIKEY" not in r.headers for r in transport.sent)


def test_signed_origin_is_fixed_by_mode():
    assert SignedClient(hmac_creds("live")).origin == "https://api.binance.com"
    assert SignedClient(hmac_creds("testnet")).origin == "https://testnet.binance.vision"
    with pytest.raises(ValueError):
        PublicClient("http://api.binance.com")


def test_redirects_are_not_followed():
    session, transport = session_with(lambda r: (302, b"", {"Location": "https://example.com/"}))
    client = SignedClient(hmac_creds(), session=session)
    with pytest.raises(Rejected, match="redirect"):
        client.account()
    assert len(transport.sent) == 1


def test_environment_proxies_and_netrc_are_ignored():
    assert PublicClient(binance.LIVE).session.trust_env is False


def test_rate_limit_backs_off_without_more_requests():
    session, transport = session_with(
        lambda r: (429, {"code": -1003, "msg": "slow down"}, {"Retry-After": "30"})
    )
    client = PublicClient(binance.LIVE, session=session)
    with pytest.raises(RateLimited) as err:
        client.server_time()
    assert err.value.retry_after == 30
    with pytest.raises(RateLimited):
        client.server_time()
    assert len(transport.sent) == 1


@pytest.mark.parametrize(
    ("answer", "error"),
    [
        ((503, {"code": -1001, "msg": "Internal error"}), Ambiguous),
        (
            (408, {"code": -1007, "msg": "Timeout waiting for response from backend server"}),
            Ambiguous,
        ),
        ((400, {"code": -2010, "msg": "Account has insufficient balance"}), Rejected),
        (
            (401, {"code": -2015, "msg": "Invalid API-key, IP, or permissions for action."}),
            Rejected,
        ),
    ],
)
def test_order_errors_are_classified(answer, error):
    session, _ = session_with(lambda r: answer)
    client = SignedClient(hmac_creds(), session=session)
    with pytest.raises(error):
        client.new_market_order("BTCUSDT", "BUY", "c1", quote_qty="10")


def test_permission_errors_carry_a_hint():
    session, _ = session_with(
        lambda r: (401, {"code": -2015, "msg": "Invalid API-key, IP, or permissions"})
    )
    with pytest.raises(Rejected, match="not allowed from this IP"):
        SignedClient(hmac_creds(), session=session).account()


def test_network_failures_are_classified():
    def connect_fail(r):
        raise requests.exceptions.ConnectTimeout("connect timed out")

    def read_fail(r):
        raise requests.exceptions.ReadTimeout("read timed out")

    for handler, error in ((connect_fail, NotSent), (read_fail, Ambiguous)):
        session, _ = session_with(handler)
        with pytest.raises(error):
            SignedClient(hmac_creds(), session=session).new_market_order(
                "BTCUSDT", "BUY", "c1", quote_qty="10"
            )


def test_query_order_not_found_returns_none():
    session, transport = session_with(
        lambda r: (400, {"code": -2013, "msg": "Order does not exist."})
    )
    client = SignedClient(hmac_creds(), session=session)
    assert client.query_order("BTCUSDT", "c1") is None
    assert query(transport.sent[0])["origClientOrderId"] == "c1"
    assert path(transport.sent[0]) == "/api/v3/order"


def test_market_order_parameters():
    session, transport = session_with(lambda r: (200, {"orderId": 1}))
    client = SignedClient(hmac_creds("testnet"), session=session)
    client.new_market_order("BTCUSDT", "SELL", "sbtx1", qty="0.00100")
    q = query(transport.sent[0])
    assert {k: q[k] for k in ("symbol", "side", "type", "quantity", "newClientOrderId", "newOrderRespType")} == {
        "symbol": "BTCUSDT", "side": "SELL", "type": "MARKET", "quantity": "0.00100",
        "newClientOrderId": "sbtx1", "newOrderRespType": "FULL",
    }  # fmt: skip
    assert "quoteOrderQty" not in q
    with pytest.raises(ValueError):
        client.new_market_order("BTCUSDT", "SELL", "x", qty="1", quote_qty="1")


def test_key_permissions_are_only_read_on_live():
    client = SignedClient(hmac_creds("testnet"))
    with pytest.raises(binance.BinanceError):
        client.api_restrictions()


def test_a_bad_header_value_never_shows_up_in_the_error():
    session, transport = session_with(lambda r: (200, {}))
    creds = Credentials(
        "live", Secret("abcdef0123456789\nX-Injected: 1"), HmacSigner(Secret("s" * 64)), "t"
    )
    with pytest.raises(NotSent) as err:
        SignedClient(creds, session=session).account()
    assert "abcdef0123456789" not in str(err.value)
    assert transport.sent == []


def test_a_response_that_breaks_off_is_an_unknown_outcome():
    def broken(r):
        raise requests.exceptions.ChunkedEncodingError("connection broken while reading the body")

    session, _ = session_with(broken)
    with pytest.raises(Ambiguous):
        SignedClient(hmac_creds(), session=session).new_market_order(
            "BTCUSDT", "BUY", "c1", quote_qty="10"
        )


def test_a_success_status_without_json_is_an_unknown_outcome():
    session, _ = session_with(lambda r: (200, b"<html>gateway</html>"))
    with pytest.raises(Ambiguous):
        SignedClient(hmac_creds(), session=session).new_market_order(
            "BTCUSDT", "BUY", "c1", quote_qty="10"
        )


def test_odd_retry_after_header_falls_back_to_a_minute():
    session, _ = session_with(lambda r: (429, {"code": -1003}, {"Retry-After": "soon"}))
    with pytest.raises(RateLimited) as err:
        PublicClient(binance.LIVE, session=session).server_time()
    assert err.value.retry_after == 60
