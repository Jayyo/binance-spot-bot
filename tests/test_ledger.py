from decimal import Decimal as D

import pytest

from spotbot.ledger import LedgerConflict, OrderRow, Store, book_from_fills
from spotbot.model import Fill, Side, Status


def fill(trade_id, side, price, qty, commission="0", asset="USDT", valued=None, cid="c1", t=1):
    return Fill(trade_id, 1, cid, side, D(price), D(qty), D(price) * D(qty), D(commission), asset, t,
                None if valued is None else D(valued))  # fmt: skip


def order(cid, status=Status.PENDING_SEND, created=1):
    return OrderRow(cid, "BTCUSDT", Side.BUY, "entry", D(100), None, None, status, None, D(0), D(0),
                    created, created, 0, 0, "")  # fmt: skip


def test_book_charges_each_fee_asset_correctly():
    fills = [
        fill(
            1, Side.BUY, "100", "1", commission="0.001", asset="BTC"
        ),  # fee taken from what we bought
        fill(
            2, Side.SELL, "110", "0.5", commission="0.055", asset="USDT"
        ),  # fee taken from proceeds
        fill(3, Side.SELL, "110", "0.2", commission="0.01", asset="BNB", valued="6"),
        fill(4, Side.SELL, "110", "0.1", commission="0.02", asset="XYZ"),  # cannot be valued
    ]
    book = book_from_fills(D(1000), fills, "BTC", "USDT")
    assert book.qty == D("0.199")
    assert book.cash == D(1000) - D(100) + D(55) - D("0.055") + D(22) + D(11)
    assert book.outside_fees == D(6)
    assert book.unvalued_fees == {"XYZ": D("0.02")}
    assert book.fees_quote == D("0.1") + D("0.055") + D(6)
    assert book.equity(D(100)) == book.cash + D("19.9") - D(6)


def test_fills_are_recorded_once():
    store = Store()
    f = fill(7, Side.BUY, "100", "1")
    assert store.insert_fills("BTCUSDT", [f, f]) == [f]
    assert store.insert_fills("BTCUSDT", [f]) == []
    assert store.insert_fills("ETHUSDT", [f]) == [f]  # trade ids are per symbol
    assert len(store.fills("BTCUSDT")) == 1


def test_a_reused_trade_id_for_another_order_is_a_conflict():
    store = Store()
    store.insert_fills("BTCUSDT", [fill(7, Side.BUY, "100", "1", cid="old")])
    with pytest.raises(LedgerConflict, match="reset"):
        store.insert_fills("BTCUSDT", [fill(7, Side.BUY, "120", "1", cid="new")])


def test_open_orders_only_lists_unfinished_ones():
    store = Store()
    for i, status in enumerate([Status.PENDING_SEND, Status.UNKNOWN, Status.NEW, Status.PARTIALLY_FILLED,
                                Status.FILLED, Status.REJECTED, Status.NOT_FOUND, Status.EXPIRED]):  # fmt: skip
        store.insert_order(order(f"o{i}", status, created=i))
    assert [o.client_id for o in store.open_orders("BTCUSDT")] == ["o0", "o1", "o2", "o3"]


def test_transaction_rolls_back_on_error():
    store = Store()
    with pytest.raises(RuntimeError), store.tx():
        store.insert_order(order("a"))
        raise RuntimeError("boom")
    assert store.get_order("a") is None


def test_state_file_is_private(tmp_path):
    path = tmp_path / "x.db"
    Store(path).close()
    assert (path.stat().st_mode & 0o077) == 0
