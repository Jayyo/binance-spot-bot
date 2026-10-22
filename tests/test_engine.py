from decimal import Decimal as D

import pytest

from spotbot.engine import EngineError
from spotbot.ledger import Store
from spotbot.model import Fill, Side, Status
from spotbot.risk import UNKNOWN_BASELINE, RiskConfig
from spotbot.strategy import Signal
from tests.fakes import T0, H, bar, make_engine, oversold_history, set_price

NO_DAILY = RiskConfig(daily_loss_pct=D(0))


def enter(engine, quote, i=0, price=170):
    """Close bar i with RSI deep below 30 and let the engine act at the next open."""
    now = T0 + (i + 1) * H
    set_price(quote, price, now)
    engine.on_tick(now)
    engine.on_bar_close(bar(i, price), now)
    return now


def warmed(**kw):
    engine, venue, store, quote = make_engine(**kw)
    engine.warmup(oversold_history())
    return engine, venue, store, quote


def test_entry_fills_records_ledger_and_sets_stop():
    engine, venue, store, quote = warmed(risk=NO_DAILY)
    enter(engine, quote)
    assert len(venue.placements) == 1
    assert engine.book.qty == D("5.88235") - D("5.88235") * D("0.001")
    assert engine.state.entry_price == D(170)
    assert engine.state.stop_price == D(170) * D("0.97")
    assert engine.has_position()
    assert [o.status for o in store.orders("BTCUSDT")] == [Status.FILLED]


def test_stop_exit_then_cooldown_blocks_immediate_reentry():
    engine, venue, store, quote = warmed(risk=NO_DAILY)
    now = enter(engine, quote)
    set_price(quote, 160, now + 1000)
    engine.on_tick(now + 1000)  # stop at 164.90 hit
    assert not engine.has_position()
    assert engine.state.stop_price is None
    sells = [o for o in store.orders("BTCUSDT") if o.side is Side.SELL]
    assert [o.purpose for o in sells] == ["stop"]

    now2 = enter(engine, quote, i=1, price=159)  # bar 1 contains the exit: cooldown
    assert len(venue.placements) == 2
    enter(engine, quote, i=2, price=158)  # next bar: allowed again
    assert len(venue.placements) == 3
    assert now2 < T0 + 3 * H


def test_daily_loss_latch_blocks_entries_but_not_exits():
    engine, venue, store, quote = warmed(risk=RiskConfig(stop_loss_pct=D(0), daily_loss_pct=D(2)))
    now = enter(engine, quote)
    set_price(quote, 160, now + 1000)
    engine.on_tick(now + 1000)
    assert engine.state.latched
    # A sharp rally pushes RSI above 70: the exit still goes through.
    price, i = 160, 1
    while engine.has_position() and i < 40:
        price += 8
        set_price(quote, price, T0 + (i + 1) * H)
        engine.on_bar_close(bar(i, price), T0 + (i + 1) * H)
        i += 1
    assert not engine.has_position()
    # Fresh oversold signals later the same UTC day: ignored while latched.
    signals = []
    while i < 22:
        price -= 6
        set_price(quote, price, T0 + (i + 1) * H)
        signals.append(engine.on_bar_close(bar(i, price), T0 + (i + 1) * H))
        i += 1
    assert Signal.ENTER in signals
    assert [o.side for o in store.orders("BTCUSDT")] == [Side.BUY, Side.SELL]
    events = " ".join(m for _, _, m in store.recent_events(50))
    assert "entries paused" in events


def test_ambiguous_buy_is_reconciled_without_a_second_order():
    m = 60_000
    engine, venue, store, quote = make_engine(risk=NO_DAILY, interval="1m")
    engine.warmup(oversold_history(step=m))
    venue.ambiguous_next, venue.hidden_queries = 1, 3  # executed, but "not found" three times
    set_price(quote, 170, T0 + m)
    engine.on_bar_close(bar(0, 170, step=m), T0 + m)
    (order,) = store.orders("BTCUSDT")
    assert order.status is Status.UNKNOWN
    assert engine.book.qty == 0

    t = T0 + m
    for _ in range(2):
        t += 15_000
        set_price(quote, 169, t)
        engine.on_tick(t)
    assert store.get_order(order.client_id).status is Status.UNKNOWN

    # The next bar is another entry signal; nothing new is sent while the first is unresolved.
    t = T0 + 2 * m
    set_price(quote, 169, t)
    engine.on_bar_close(bar(1, 169, step=m), t)
    assert len(venue.placements) == 1
    assert store.get_order(order.client_id).status is Status.UNKNOWN

    for _ in range(
        4
    ):  # the exchange finally reports it filled; later answers repeat the same fills
        t += 10_000
        engine.on_tick(t)
    assert store.get_order(order.client_id).status is Status.FILLED
    assert len(venue.placements) == 1
    assert len(store.fills("BTCUSDT")) == 1
    assert engine.has_position() and engine.state.stop_price is not None


def test_order_never_seen_is_closed_after_the_grace_period():
    engine, venue, store, quote = warmed(risk=NO_DAILY)
    venue.ambiguous_next, venue.execute_when_ambiguous, venue.seen_answer = 1, False, False
    now = enter(engine, quote)
    t = now
    while t < now + 130_000:
        t += 31_000
        engine.on_tick(t)
    (order,) = store.orders("BTCUSDT")
    assert order.status is Status.NOT_FOUND
    assert engine.book.qty == 0
    enter(engine, quote, i=1, price=169)  # trading resumes
    assert len(venue.placements) == 2


def test_inconclusive_history_waits_and_then_asks_for_a_human():
    engine, venue, store, quote = warmed(risk=NO_DAILY)
    venue.ambiguous_next, venue.execute_when_ambiguous, venue.seen_answer = 1, False, None
    now = enter(engine, quote)
    t = now
    while t < now + 700_000:
        t += 30_000
        set_price(quote, 169, t)
        engine.on_tick(t)
    assert store.orders("BTCUSDT")[0].status is Status.UNKNOWN
    assert "stuck_order" in engine.state.flags
    snap = engine.snapshot(t)
    assert snap.flags and snap.open_orders


def test_partial_fill_then_expired_keeps_the_executed_part():
    engine, venue, store, quote = warmed(risk=NO_DAILY)
    venue.partial = D("0.5")
    enter(engine, quote)
    (order,) = store.orders("BTCUSDT")
    assert order.status is Status.EXPIRED
    assert engine.book.qty == D("2.94117") * D("0.999")
    assert engine.state.entry_price == D(170)


def test_only_sells_what_it_bought():
    engine, venue, store, quote = warmed(risk=NO_DAILY)
    venue.free = {"BTC": D(10), "USDT": D(5000)}  # the account already held 10 BTC
    now = enter(engine, quote)
    set_price(quote, 160, now + 1000)
    engine.on_tick(now + 1000)
    sell = [o for o in store.orders("BTCUSDT") if o.side is Side.SELL][0]
    assert sell.qty == D("5.87646")  # bot inventory rounded down to the lot step
    assert venue.free["BTC"] > D(10) - D("0.0001")  # the user's 10 BTC are untouched


def test_missing_coins_are_flagged_and_exit_is_limited_to_what_is_there():
    engine, venue, store, quote = warmed(risk=NO_DAILY)
    venue.free = {"BTC": D(0), "USDT": D(5000)}
    now = enter(engine, quote)
    venue.free["BTC"] = D(1)  # someone sold most of it by hand
    engine.check_balances(now)
    assert "balance" in engine.state.flags
    set_price(quote, 160, now + 1000)
    engine.on_tick(now + 1000)
    sell = [o for o in store.orders("BTCUSDT") if o.side is Side.SELL][0]
    assert sell.qty == D(1)
    enter(engine, quote, i=1, price=150)
    assert len([o for o in store.orders("BTCUSDT") if o.side is Side.BUY]) == 1


def test_restart_keeps_position_and_does_not_replay_bars(tmp_path):
    path = tmp_path / "state.db"
    engine, venue, store, quote = warmed(store=Store(path), risk=NO_DAILY)
    enter(engine, quote)
    qty, stop, last = engine.book.qty, engine.state.stop_price, engine.state.last_bar
    store.close()

    engine2, _, store2, quote2 = make_engine(
        store=Store(path), venue=venue, quote=quote, risk=NO_DAILY
    )
    engine2.warmup(oversold_history())
    assert (engine2.book.qty, engine2.state.stop_price, engine2.state.last_bar) == (qty, stop, last)
    assert engine2.on_bar_close(bar(0, 170), T0 + H) is None  # already processed
    assert len(venue.placements) == 1


def test_budget_change_is_refused(tmp_path):
    path = tmp_path / "state.db"
    make_engine(store=Store(path), budget=D(1000))
    with pytest.raises(EngineError, match="budget"):
        make_engine(store=Store(path), budget=D(2000))


def test_kill_switch_pauses_entries_but_stops_still_run(tmp_path):
    stop_file = tmp_path / "STOP"
    engine, venue, store, quote = warmed(risk=NO_DAILY, kill_switch=stop_file)
    now = enter(engine, quote)
    stop_file.touch()
    set_price(quote, 160, now + 1000)
    engine.on_tick(now + 1000)
    assert not engine.has_position()  # stop executed
    enter(engine, quote, i=2, price=150)
    assert len(venue.placements) == 2  # the entry signal was ignored
    stop_file.unlink()
    engine.on_tick(T0 + 4 * H)
    enter(engine, quote, i=3, price=149)
    assert len(venue.placements) == 3


def test_unknown_start_of_day_value_pauses_entries_until_recovered():
    engine, venue, store, quote = warmed(risk=NO_DAILY)
    enter(engine, quote, i=20, price=170)  # holding into the next UTC day
    venue.price_at_value = None
    midnight = T0 + 24 * H
    set_price(quote, 171, midnight + 5000)
    engine.on_tick(midnight + 5000)
    assert engine.state.latched and engine.state.latch_reason == UNKNOWN_BASELINE
    venue.price_at_value = D(172)
    engine.on_tick(midnight + 30_000)  # retry is throttled to once a minute
    assert engine.state.latched
    engine.on_tick(midnight + 70_000)
    assert not engine.state.latched
    assert engine.state.day_start_equity == engine.book.cash + engine.book.qty * D(172)


def test_fee_in_a_third_asset_without_a_price_is_reported():
    engine, venue, store, quote = warmed(risk=NO_DAILY)
    venue.commission_asset = "BNB"
    enter(engine, quote)
    assert "BNB" in engine.snapshot(T0 + H).unvalued_fees


def test_rejected_order_is_final_and_next_signal_can_trade():
    engine, venue, store, quote = warmed(risk=NO_DAILY)
    venue.reject_next = 1
    enter(engine, quote)
    assert store.orders("BTCUSDT")[0].status is Status.REJECTED
    enter(engine, quote, i=1, price=169)
    assert [o.status for o in store.orders("BTCUSDT")] == [Status.REJECTED, Status.FILLED]


def test_stale_market_data_blocks_entries():
    engine, venue, store, quote = warmed(risk=NO_DAILY)
    now = T0 + H
    set_price(quote, 170, now - 60_000)
    engine.on_bar_close(bar(0, 170), now)
    assert venue.placements == []
    assert "stale" in store.recent_events(1)[0][2]


def test_failing_stop_exit_is_retried_slowly():
    engine, venue, store, quote = warmed(risk=NO_DAILY)
    now = enter(engine, quote)
    venue.reject_next = 100
    for k in range(10):  # ten ticks over 20 seconds, all below the stop
        set_price(quote, 160, now + 1000 + k * 2000)
        engine.on_tick(now + 1000 + k * 2000)
    sells = [o for o in store.orders("BTCUSDT") if o.side is Side.SELL]
    assert len(sells) == 1
    venue.reject_next = 0
    set_price(quote, 160, now + 40_000)
    engine.on_tick(now + 40_000)
    assert not engine.has_position()


def test_state_is_bound_to_one_account(tmp_path):
    path = tmp_path / "state.db"
    make_engine(store=Store(path), account="live:111")
    with pytest.raises(EngineError, match="belongs to account live:111"):
        make_engine(store=Store(path), account="live:222")


def test_settings_change_only_while_flat(tmp_path):
    path = tmp_path / "state.db"
    engine, venue, store, quote = warmed(store=Store(path), risk=NO_DAILY)
    store.close()
    tighter = RiskConfig(daily_loss_pct=D(0), stop_loss_pct=D(1))
    engine, _, store, _ = make_engine(
        store=Store(path), venue=venue, quote=quote, risk=tighter
    )  # flat: fine
    engine.warmup(oversold_history())
    enter(engine, quote)
    store.close()
    with pytest.raises(EngineError, match="settings differ"):
        make_engine(store=Store(path), venue=venue, quote=quote, risk=NO_DAILY)


def test_client_ids_do_not_collide_across_state_directories():
    a, _, _, _ = make_engine()
    b, _, _, _ = make_engine()
    assert a._client_id("entry", T0) != b._client_id("entry", T0)


def test_position_below_the_minimum_order_value_keeps_its_stop():
    engine, venue, store, quote = warmed(budget=D(6), risk=NO_DAILY)
    now = enter(engine, quote)  # about 6 USDT of BTC
    stop = engine.state.stop_price
    set_price(quote, 100, now + 1000)  # 0.035 BTC is now worth 3.5 USDT, under the 5 USDT minimum
    engine.on_tick(now + 1000)
    assert engine.has_position() and engine.state.stop_price == stop
    assert engine.state.exit_reason == "stop"
    assert [o.side for o in store.orders("BTCUSDT")] == [Side.BUY]
    set_price(quote, 160, now + 40_000)  # sellable again, and the exit is still pending
    engine.on_tick(now + 40_000)
    assert not engine.has_position()
    assert [o.side for o in store.orders("BTCUSDT")] == [Side.BUY, Side.SELL]


def rally(engine, quote, start_i, price, steps=40):
    i = start_i
    while i < start_i + steps and engine.state.exit_reason is None and engine.has_position():
        price += 8
        set_price(quote, price, T0 + (i + 1) * H)
        engine.on_bar_close(bar(i, price), T0 + (i + 1) * H)
        i += 1
    return i, price


def test_rejected_rsi_exit_is_finished_on_later_ticks():
    engine, venue, store, quote = warmed(risk=RiskConfig(daily_loss_pct=D(0), stop_loss_pct=D(0)))
    enter(engine, quote)
    venue.reject_next = 1
    i, price = rally(engine, quote, 1, 170)
    assert engine.state.exit_reason == "exit" and engine.has_position()
    t = T0 + i * H
    set_price(quote, price, t + 10_000)
    engine.on_tick(t + 10_000)  # still inside the retry wait
    assert engine.has_position()
    set_price(quote, price - 20, t + 40_000)  # RSI has cooled off; the decided exit still goes
    engine.on_tick(t + 40_000)
    assert not engine.has_position()
    assert [o.status for o in store.orders("BTCUSDT")] == [
        Status.FILLED,
        Status.REJECTED,
        Status.FILLED,
    ]


def test_partial_exit_is_completed():
    engine, venue, store, quote = warmed(risk=RiskConfig(daily_loss_pct=D(0), stop_loss_pct=D(0)))
    enter(engine, quote)
    venue.partial = D("0.5")
    i, price = rally(engine, quote, 1, 170)
    venue.partial = None
    assert engine.has_position() and engine.state.exit_reason == "exit"
    engine.on_tick(T0 + i * H + 1000)
    assert not engine.has_position()
    assert [o.side for o in store.orders("BTCUSDT")] == [Side.BUY, Side.SELL, Side.SELL]


def test_fill_from_before_midnight_found_later_resets_the_baseline():
    m = 60_000
    engine, venue, store, quote = make_engine(risk=NO_DAILY, interval="1m")
    day = T0 + 86_400_000
    engine.warmup([bar(i - 30, 200.0 - i, start=day - 2 * m, step=m) for i in range(30)])
    venue.ambiguous_next, venue.hidden_queries, venue.price_at_value = 1, 50, D(180)
    set_price(quote, 170, day - m)  # 23:59 UTC
    engine.on_tick(day - m)
    engine.on_bar_close(bar(-1, 170, start=day - m, step=m), day - m)
    assert store.orders("BTCUSDT")[0].status is Status.UNKNOWN
    set_price(quote, 175, day + 5_000)
    engine.on_tick(day + 5_000)  # new day: the buy is not in the ledger yet
    assert engine.state.day_start_equity == D(1000)
    venue.hidden_queries = 0
    t = day + 5_000
    while store.orders("BTCUSDT")[0].status is Status.UNKNOWN:
        t += 31_000
        engine.on_tick(t)
    assert engine.state.day_start_equity == engine.book.cash + engine.book.qty * D(180)


def test_reused_trade_id_stops_new_entries():
    engine, venue, store, quote = warmed(risk=NO_DAILY)
    now = enter(engine, quote)
    set_price(quote, 160, now + 1000)
    engine.on_tick(now + 1000)  # stopped out: trades 1 and 2 recorded
    venue._trade = 0  # the exchange history was wiped (Spot Testnet reset)
    enter(engine, quote, i=2, price=150)
    assert "ledger" in engine.state.flags
    enter(engine, quote, i=3, price=149)
    assert len(venue.placements) == 3  # nothing new after the conflict


def test_partial_entry_is_protected_before_the_order_finishes():
    engine, venue, store, quote = warmed(risk=NO_DAILY)
    venue.open_next, venue.complete_after = True, 3
    now = enter(engine, quote)
    (order,) = store.orders("BTCUSDT")
    assert order.status is Status.PARTIALLY_FILLED
    assert engine.state.stop_price == D(170) * D("0.97")  # stop from the half already bought
    set_price(quote, 160, now + 1500)
    engine.on_tick(now + 1500)  # stop hit while the buy is still open: exit recorded, not sent
    assert engine.state.exit_reason == "stop"
    assert [o.side for o in store.orders("BTCUSDT")] == [Side.BUY]
    t = now + 1500
    while store.orders("BTCUSDT")[0].status is not Status.FILLED:
        t += 5_000
        set_price(quote, 172, t)  # price recovered; the decided exit still runs
        engine.on_tick(t)
    set_price(quote, 172, t + 5_000)
    engine.on_tick(t + 5_000)
    assert not engine.has_position()
    assert [o.side for o in store.orders("BTCUSDT")] == [Side.BUY, Side.SELL]


def test_exit_signal_without_a_fresh_price_waits_for_one():
    engine, venue, store, quote = warmed(risk=RiskConfig(daily_loss_pct=D(0), stop_loss_pct=D(0)))
    enter(engine, quote)
    price, i = 170, 1
    while engine.state.exit_reason is None and i < 40:
        price += 8
        set_price(quote, price, T0 + i * H)  # quote an hour old when the bar is handled
        engine.on_bar_close(bar(i, price), T0 + (i + 1) * H)
        i += 1
    assert engine.state.exit_reason == "exit" and engine.has_position()
    set_price(quote, price, T0 + i * H + 1000)
    engine.on_tick(T0 + i * H + 1000)
    assert not engine.has_position()


def test_bar_handled_after_midnight_counts_toward_the_new_day():
    one = RiskConfig(
        daily_loss_pct=D(0), stop_loss_pct=D(0), max_entries_per_day=1, cooldown_bars=0
    )
    engine, venue, store, quote = warmed(risk=one)
    enter(engine, quote, i=22, price=170)  # 23:00 bar, handled at 23:00 on day one
    assert engine.state.entries_today == 1
    set_price(quote, 300, T0 + 23 * H + 60_000)
    engine.on_tick(T0 + 23 * H + 60_000)
    engine.state.exit_reason = "exit"  # sell it so the next signal can enter
    engine.on_tick(T0 + 23 * H + 61_000)
    assert not engine.has_position()
    midnight = T0 + 24 * H
    set_price(quote, 150, midnight + 2_000)
    engine.on_bar_close(bar(23, 150), midnight + 2_000)  # no tick since 23:01: the day rolls here
    assert engine.state.day == "2025-01-02"
    assert engine.state.entries_today == 1 and engine.has_position()


def test_late_fill_does_not_lift_a_daily_loss_latch():
    engine, venue, store, quote = warmed(risk=RiskConfig(stop_loss_pct=D(0)))
    engine.on_tick(T0)
    engine.state.latched, engine.state.latch_reason = True, "daily loss -2.50% hit the -2% limit"
    engine.state.day = "2025-01-01"
    venue.price_at_value = None
    yesterday = Fill(99, 9, "x", Side.BUY, D(100), D(1), D(100), D(0), "USDT", T0 - 1000)
    engine.store.insert_fills("BTCUSDT", [yesterday])
    engine.book = engine._rebuild_book()
    engine._fills_before_midnight(T0 + 1000, [yesterday])
    assert engine.state.latched and engine.state.latch_reason.startswith("daily loss")
    venue.price_at_value = D(100)
    set_price(quote, 100, T0 + 120_000)
    engine.on_tick(T0 + 120_000)
    assert engine.state.latched and engine.state.latch_reason.startswith("daily loss")


def test_changing_the_interval_while_flat_restarts_bar_tracking(tmp_path):
    path = tmp_path / "state.db"
    engine, venue, store, quote = warmed(store=Store(path), risk=NO_DAILY)
    assert engine.state.last_bar is not None
    store.close()
    engine, _, store, _ = make_engine(
        store=Store(path), venue=venue, quote=quote, risk=NO_DAILY, interval="1m"
    )
    assert engine.state.last_bar is None


def test_equal_settings_written_differently_are_the_same(tmp_path):
    path = tmp_path / "state.db"
    engine, venue, store, quote = warmed(
        store=Store(path), risk=RiskConfig(daily_loss_pct=D(0), max_position=D(50))
    )
    enter(engine, quote)
    store.close()
    same = RiskConfig(daily_loss_pct=D("0.0"), max_position=D("50.00"))
    make_engine(store=Store(path), venue=venue, quote=quote, risk=same)  # no error while holding


def test_chunked_exit_leaves_a_sellable_remainder():
    from spotbot.filters import SymbolRules

    rules = SymbolRules(symbol="BTCUSDT", base_asset="BTC", quote_asset="USDT", step=D("0.1"), min_qty=D("0.1"),
                        min_notional=D(20), max_notional=D(100), max_notional_market=True)  # fmt: skip
    engine, venue, store, quote = make_engine(venue=None)
    engine.rules = rules
    assert engine._chunk(D("1.1"), D(100)) == D("0.9")  # 1.0 would leave 0.1 worth 10, below 20
    assert engine._chunk(D("0.8"), D(100)) == D("0.8")


def test_latch_set_while_handling_a_bar_is_saved(tmp_path):
    path = tmp_path / "state.db"
    engine, venue, store, quote = warmed(store=Store(path), risk=RiskConfig(stop_loss_pct=D(0)))
    enter(engine, quote)
    set_price(quote, 150, T0 + 2 * H)  # first seen while handling the next bar
    engine.on_bar_close(bar(1, 150), T0 + 2 * H)
    assert engine.state.latched
    assert Store(path).load_state("BTCUSDT").latched


def test_entry_is_rechecked_after_the_balance_request():
    engine, venue, store, quote = warmed(risk=NO_DAILY)
    venue.free = {"BTC": D(0), "USDT": D(5000)}
    engine._clock = lambda: T0 + H + 45_000  # the balance call "took" 45 s
    enter(engine, quote)
    assert venue.placements == []
    assert "changed while deciding" in store.recent_events(1)[0][2]


def test_chunk_respects_the_minimum_lot_of_the_remainder():
    from spotbot.filters import SymbolRules

    rules = SymbolRules(symbol="XUSDT", base_asset="X", quote_asset="USDT", step=D(1), min_qty=D(10),
                        min_notional=D(10), max_notional=D(100), max_notional_market=True)  # fmt: skip
    engine, _, _, _ = make_engine()
    engine.rules = rules
    assert engine._chunk(D(25), D(5)) == D(15)  # 20 would leave 5, below the 10 minimum lot
    assert engine._chunk(D(25), D(2)) == D(25)  # 50 USDT fits in one order


def test_balance_check_waits_for_unresolved_orders_and_clears_itself():
    engine, venue, store, quote = warmed(risk=NO_DAILY)
    venue.free = {"BTC": D(0), "USDT": D(5000)}
    now = enter(engine, quote)
    venue.ambiguous_next, venue.hidden_queries = 1, 3
    set_price(quote, 160, now + 1000)
    engine.on_tick(now + 1000)  # stop: sell sent, outcome unknown, coins already gone
    assert "balance" not in engine.state.flags  # not judged while the sell is unresolved
    t = now + 1000
    for _ in range(8):
        t += 10_000
        set_price(quote, 160, t)
        engine.on_tick(t)
    assert not engine.open_orders() and not engine.has_position()
    assert "balance" not in engine.state.flags
