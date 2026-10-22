import random
from dataclasses import replace
from decimal import Decimal as D

import pytest

from spotbot import backtest
from spotbot.data import Dataset
from spotbot.metrics import max_drawdown, sharpe
from spotbot.model import Bar
from spotbot.risk import RiskConfig
from spotbot.strategy import RsiParams
from tests.reference import simulate

H = 3_600_000
DEC_2024 = 1_733_011_200_000  # 2024-12-01 00:00 UTC, one month of warm-up before 2025-01


def walk(n, seed, start=100.0, vol=0.012, drift=0.0):
    rng = random.Random(seed)
    bars, price = [], start
    for i in range(n):
        o = price
        c = max(1.0, o * (1 + rng.gauss(drift, vol)))
        hi = max(o, c) * (1 + abs(rng.gauss(0, vol / 2)))
        lo = min(o, c) * (1 - abs(rng.gauss(0, vol / 2)))
        t = DEC_2024 + i * H
        bars.append(Bar(t, o, hi, lo, c, 1.0, t + H - 1))
        price = c
    return bars


def dataset(bars):
    return Dataset("TESTUSDT", "1h", bars, files=[])


def config(**kw):
    return backtest.BacktestConfig("TESTUSDT", "1h", "2025-01", "2025-03", **kw)


@pytest.mark.parametrize("seed", [1, 2, 3, 4])
@pytest.mark.parametrize("ema", [0, 200])
def test_engine_matches_independent_reference(seed, ema):
    # An upward drift so the EMA filter lets some entries through.
    bars = walk(24 * 31 + 24 * 90, seed, drift=0.001 if ema else 0.0)
    res = backtest.run(config(params=RsiParams(trend_ema=ema)), dataset(bars))
    ref = simulate(bars, res.warmup_bars, budget=10_000, fee=0.001, slip=0.0005, trend_ema=ema)
    entries = sum(1 for o in res.store.orders("TESTUSDT") if o.purpose == "entry")
    assert entries == ref["entries"] > 0
    assert res.points[-1][1] == pytest.approx(ref["final"], rel=1e-9)
    assert [v for _, v in res.points[1:]] == pytest.approx(ref["curve"], rel=1e-9)


def test_reference_agreement_holds_with_other_risk_settings():
    bars = walk(24 * 31 + 24 * 90, 7, vol=0.02)
    risk = RiskConfig(
        position_pct=D(50),
        stop_loss_pct=D(5),
        daily_loss_pct=D(4),
        max_entries_per_day=2,
        cooldown_bars=3,
    )
    res = backtest.run(config(risk=risk, fee_bps=D(20), slippage_bps=D(10)), dataset(bars))
    ref = simulate(bars, res.warmup_bars, budget=10_000, fee=0.002, slip=0.001, stop_pct=5, daily_pct=4,
                   max_entries=2, cooldown=3, position_pct=50)  # fmt: skip
    assert res.points[-1][1] == pytest.approx(ref["final"], rel=1e-9)


def test_future_bars_do_not_change_earlier_decisions():
    bars = walk(24 * 31 + 24 * 90, 11)
    cut = 24 * 31 + 24 * 45
    changed = bars[:cut] + [
        replace(b, close=b.close * 0.5, low=b.low * 0.5, open=b.open * 0.5) for b in bars[cut:]
    ]
    a = backtest.run(config(), dataset(bars))
    b = backtest.run(config(), dataset(changed))
    end = bars[cut - 1].close_time
    fills_a = [f for f in a.store.fills("TESTUSDT") if f.time <= end]
    fills_b = [f for f in b.store.fills("TESTUSDT") if f.time <= end]
    assert fills_a == fills_b
    assert [v for t, v in a.points if t <= end] == [v for t, v in b.points if t <= end]


def test_one_round_trip_by_hand():
    # Flat warm-up, then a straight fall (RSI 0) and a straight rise (RSI 100).
    warm = [
        Bar(DEC_2024 + i * H, 100, 100, 100, 100, 1, DEC_2024 + i * H + H - 1) for i in range(744)
    ]
    t0 = DEC_2024 + 744 * H
    closes = [99.0, 98.0] + [98.0 + 2 * i for i in range(1, 40)]
    bars = []
    for i, c in enumerate(closes):
        o = 100.0 if i == 0 else closes[i - 1]
        bars.append(Bar(t0 + i * H, o, max(o, c), min(o, c), c, 1, t0 + i * H + H - 1))
    res = backtest.run(
        config(risk=RiskConfig(stop_loss_pct=D(0)), fee_bps=D(10), slippage_bps=D(0)),
        dataset(warm + bars),
    )
    trade = res.trades[0]
    # RSI(14) of the first bar's close (99) is 0 -> buy at the next open, 99.
    assert trade.entry_price == pytest.approx(99.0)
    qty = 10_000 / 99 * 0.999
    exit_price = trade.exit_price
    assert res.trades[0].proceeds == pytest.approx(qty * exit_price * 0.999)
    assert res.summary.fees == pytest.approx(10_000 / 99 * 0.001 * 99 + qty * exit_price * 0.001)


def test_buy_and_hold_pays_the_same_costs():
    bars = walk(24 * 31 + 24 * 90, 3)
    res = backtest.run(config(fee_bps=D(10), slippage_bps=D(5)), dataset(bars))
    traded = res.bars
    expected = 10_000 / (traded[0].open * 1.0005) * 0.999 * traded[-1].close * 0.9995 * 0.999
    assert res.hold_points[-1][1] == pytest.approx(expected)


def test_needs_enough_warm_up():
    bars = walk(24 * 31 + 24 * 90, 3)[24 * 30 :]  # only one day before 2025-01
    with pytest.raises(ValueError, match="warm up"):
        backtest.run(config(params=RsiParams(trend_ema=200)), dataset(bars))


def test_metric_definitions():
    assert max_drawdown([100, 120, 90, 130, 65]) == pytest.approx(0.5)
    day = 86_400_000
    flat = [(i * day, 100.0) for i in range(10)]
    assert sharpe(flat) is None  # no variance, no ratio
    assert backtest.split_symbol("ETHBTC") == ("ETH", "BTC")


def test_bars_after_the_end_month_are_not_traded():
    bars = walk(24 * 31 + 24 * 120, 9)  # data runs into April
    res = backtest.run(config(), dataset(bars))  # config ends in March
    assert res.bars[-1].open_time < 1_743_465_600_000  # 2025-04-01 00:00 UTC
    assert all(f.time < 1_743_465_600_000 for f in res.store.fills("TESTUSDT"))


def test_first_bar_of_the_period_can_trade():
    warm = [Bar(DEC_2024 + i * H, 100 - i * 0.1, 100, 99, 100 - (i + 1) * 0.1, 1, DEC_2024 + i * H + H - 1)
            for i in range(744)]  # fmt: skip
    t0 = DEC_2024 + 744 * H
    bars = [Bar(t0 + i * H, 25.6, 25.7, 25.5, 25.6, 1, t0 + i * H + H - 1) for i in range(24 * 90)]
    res = backtest.run(config(risk=RiskConfig(stop_loss_pct=D(0))), dataset(warm + bars))
    assert res.store.fills("TESTUSDT")[0].time == t0  # bought at the period's first open


def test_a_bar_entered_and_stopped_inside_counts_as_in_market():
    flat = [
        Bar(DEC_2024 + i * H, 100, 100, 100, 100, 1, DEC_2024 + i * H + H - 1) for i in range(744)
    ]
    t0 = DEC_2024 + 744 * H
    closes = [99.0, 98.0, 98.0] + [98.0] * (24 * 90 - 3)
    bars = []
    for i, c in enumerate(closes):
        o = 100.0 if i == 0 else closes[i - 1]
        low = 90.0 if i == 1 else min(o, c)  # bar 1: bought at its open, stopped inside
        bars.append(Bar(t0 + i * H, o, max(o, c), low, c, 1, t0 + i * H + H - 1))
    res = backtest.run(config(), dataset(flat + bars))
    assert res.trades[0].exit_reason == "stop"
    assert res.in_market[1] is True and res.in_market[2] is False
