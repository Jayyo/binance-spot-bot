import json
from decimal import Decimal as D
from pathlib import Path

import pytest

from spotbot.filters import SymbolRules, floor_to_step

INFO = json.loads((Path(__file__).parent / "fixtures" / "btcusdt_exchange_info.json").read_text())


def test_parses_real_btcusdt_rules():
    r = SymbolRules.from_exchange_info(INFO)
    assert (r.base_asset, r.quote_asset, r.status) == ("BTC", "USDT", "TRADING")
    assert r.step == D("0.00001") and r.min_qty == D("0.00001")
    assert r.max_qty == D(
        "131.23134950"
    )  # MARKET_LOT_SIZE caps market orders below LOT_SIZE's 9000
    assert r.min_notional == D(5) and r.min_notional_market
    assert r.quote_market_allowed and r.quote_precision == 8
    assert r.tradable_problem() is None


@pytest.mark.parametrize(
    ("qty", "expected"),
    [("0.123456789", "0.12345"), ("0.00001", "0.00001"), ("0.000009", "0"), ("500", "131.23134")],
)
def test_sell_qty_rounds_down_and_drops_dust(qty, expected):
    r = SymbolRules.from_exchange_info(INFO)
    assert r.sell_qty(D(qty)) == D(expected)


def test_floor_to_step_is_exact_in_decimal():
    assert floor_to_step(D("0.3"), D("0.1")) == D("0.3")  # 0.1 + 0.2 issues do not apply
    assert floor_to_step(D("1.29999999"), D("0.01")) == D("1.29")
    assert floor_to_step(D("7"), D(0)) == D(7)


def test_buy_quote_precision():
    r = SymbolRules.from_exchange_info({**INFO, "quoteAssetPrecision": 2})
    assert r.buy_quote(D("99.999")) == D("99.99")


def test_notional_checks_follow_market_flags():
    r = SymbolRules.from_exchange_info(INFO)
    assert "below the exchange minimum" in r.notional_problem(D("4.99"))
    assert r.notional_problem(D(5)) is None
    assert r.notional_problem(D("1e9")) is None  # applyMaxToMarket is false for BTCUSDT
    info = json.loads(json.dumps(INFO))
    for f in info["filters"]:
        if f["filterType"] == "NOTIONAL":
            f["applyMinToMarket"] = False
    assert SymbolRules.from_exchange_info(info).notional_problem(D(1)) is None


def test_not_trading_symbol_is_reported():
    r = SymbolRules.from_exchange_info({**INFO, "status": "BREAK"})
    assert "BREAK" in r.tradable_problem()
