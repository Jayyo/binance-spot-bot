"""Runner + engine + HTTP client against the in-process fake exchange (tests/fake_exchange.py)."""

from decimal import Decimal as D

from spotbot import runner
from spotbot.binance import SignedClient
from spotbot.filters import SymbolRules
from spotbot.keys import Credentials, HmacSigner, Secret
from spotbot.model import Side, Status
from spotbot.risk import RiskConfig
from spotbot.runner import RunConfig, Runner
from tests.fake_exchange import INFO, FakeBinance, M

KEY, SECRET = "k" * 64, "s" * 64  # obviously fake; the fake exchange checks them
NOW = 1_760_000_000_000 - 1_760_000_000_000 % M + 30_000  # 30 s into a minute


def history():
    flat = [30_000 + (15 if i % 2 else -15) for i in range(280)]
    return flat + [30_000 - 60 * i for i in range(1, 21)]


def start(tmp_path, mode="testnet", **risk):
    ex = FakeBinance(api_key=KEY, secret=SECRET, now=NOW, closes=history())
    client = SignedClient(
        Credentials(mode, Secret(KEY), HmacSigner(Secret(SECRET)), "test"), session=ex.session
    )
    client.now_ms = lambda: ex.now
    client.sync_time = lambda: 0
    cfg = RunConfig(
        mode=mode,
        symbol="BTCUSDT",
        interval="1m",
        budget=D(1000),
        state_dir=tmp_path / "state",
        risk=RiskConfig(daily_loss_pct=D(0), **risk),
    )
    bot = Runner(cfg, public=client, signed=client)
    bot.setup()
    return ex, bot


def next_minute(ex, bot, close, seconds=0):
    ex.closes.append(close)
    ex.now += M + seconds * 1000
    bot.step()


def test_round_trip_matches_the_exchange_books(tmp_path):
    ex, bot = start(tmp_path)
    engine = bot.engine
    assert ex.posts == 0  # warm-up never trades, even on an oversold last bar

    next_minute(ex, bot, 28_700)  # the bar that just closed is oversold
    assert ex.posts == 1
    assert engine.book.qty == ex.balances["BTC"] > 0
    assert engine.book.cash == D(1000) - (D(10_000) - ex.balances["USDT"])
    assert engine.state.stop_price is not None

    price = 28_700
    for _ in range(30):
        if not engine.has_position():
            break
        price += 120
        next_minute(ex, bot, price)
    assert ex.posts == 2
    assert not engine.has_position()
    assert engine.book.qty == ex.balances["BTC"]  # only dust below the lot step is left
    sides = [o.side for o in engine.store.orders("BTCUSDT")]
    assert sides == [Side.BUY, Side.SELL]
    assert all(o.status is Status.FILLED for o in engine.store.orders("BTCUSDT"))


def test_lost_response_is_recovered_without_a_duplicate_order(tmp_path):
    ex, bot = start(tmp_path)
    ex.drop_next_response = True
    ex.hide_next_queries = 1
    next_minute(ex, bot, 28_700)
    (order,) = bot.engine.store.orders("BTCUSDT")
    assert order.status is Status.UNKNOWN
    assert ex.posts == 1
    for _ in range(5):
        ex.now += 5_000
        bot.step()
    assert bot.engine.store.get_order(order.client_id).status is Status.FILLED
    assert ex.posts == 1
    assert bot.engine.book.qty == ex.balances["BTC"]


def test_restart_resumes_from_the_ledger(tmp_path):
    ex, bot = start(tmp_path)
    next_minute(ex, bot, 28_700)
    qty = bot.engine.book.qty
    bot.close()

    client = bot.signed
    again = Runner(bot.cfg, public=client, signed=client)
    again.setup()
    assert again.engine.book.qty == qty
    assert again.engine.state.stop_price == bot.engine.state.stop_price
    assert ex.posts == 1


def test_live_preflight_refuses_a_key_that_can_withdraw(tmp_path):
    ex = FakeBinance(api_key=KEY, secret=SECRET, now=NOW, closes=history())
    ex.permissions["enableWithdrawals"] = True
    client = SignedClient(
        Credentials("live", Secret(KEY), HmacSigner(Secret(SECRET)), "test"), session=ex.session
    )
    client.now_ms = lambda: ex.now
    cfg = RunConfig(
        mode="live",
        symbol="BTCUSDT",
        interval="1m",
        budget=D(100),
        risk=RiskConfig(max_position=D(50)),
    )
    checks, account = runner.preflight(cfg, client, SymbolRules.from_exchange_info(INFO))
    assert any(c.level == "fail" and "withdrawals" in c.text for c in checks)
    assert account["uid"] == 42
    assert ex.posts == 0


def test_stop_file_is_honoured_when_the_quote_request_fails(tmp_path):
    ex, bot = start(tmp_path)
    (bot.cfg.state_dir / "STOP").touch()
    original = ex.handle

    def no_book(request):
        if request.url.split("?")[0].endswith("/ticker/bookTicker"):
            return 503, {"code": -1001, "msg": "Internal error"}
        return original(request)

    ex.transport.handler = no_book
    next_minute(ex, bot, 28_700)  # oversold bar closes, but entries are paused
    assert "stop_file" in bot.engine.state.flags
    assert ex.posts == 0


def test_body_lost_after_execution_does_not_duplicate_the_order(tmp_path):
    import requests

    ex, bot = start(tmp_path)
    original = ex.handle

    def execute_then_break(request):
        result = original(request)
        if request.method == "POST":
            raise requests.exceptions.ChunkedEncodingError("body cut off")
        return result

    ex.transport.handler = execute_then_break
    next_minute(ex, bot, 28_700)
    ex.transport.handler = original
    for _ in range(5):
        ex.now += 5_000
        bot.step()
    assert ex.posts == 1
    assert bot.engine.book.qty == ex.balances["BTC"] > 0
