"""A deliberately plain re-implementation of the backtest rules.

It shares no code with the engine (own RSI/EMA, float arithmetic, one loop) and
is used to check that the engine, ledger and venue plumbing add up to the
documented rules.
"""

from __future__ import annotations

from datetime import UTC, datetime


def rsi_series(closes, period):
    out = [None] * len(closes)
    gains = losses = 0.0
    avg_g = avg_l = None
    for i in range(1, len(closes)):
        ch = closes[i] - closes[i - 1]
        up, down = max(ch, 0.0), max(-ch, 0.0)
        if i <= period:
            gains += up
            losses += down
            if i == period:
                avg_g, avg_l = gains / period, losses / period
            else:
                continue
        else:
            avg_g = (avg_g * (period - 1) + up) / period
            avg_l = (avg_l * (period - 1) + down) / period
        if avg_g == 0 and avg_l == 0:
            out[i] = 50.0
        elif avg_l == 0:
            out[i] = 100.0
        else:
            out[i] = 100 - 100 / (1 + avg_g / avg_l)
    return out


def ema_series(closes, period):
    out = [None] * len(closes)
    if period <= 0 or len(closes) < period:
        return out
    value = sum(closes[:period]) / period
    out[period - 1] = value
    k = 2 / (period + 1)
    for i in range(period, len(closes)):
        value = value + k * (closes[i] - value)
        out[i] = value
    return out


def day_of(ms):
    return datetime.fromtimestamp(ms / 1000, UTC).date()


def simulate(
    bars,
    n_warm,
    *,
    budget,
    fee,
    slip,
    period=14,
    oversold=30,
    overbought=70,
    trend_ema=0,
    stop_pct=3.0,
    daily_pct=2.0,
    max_entries=10,
    cooldown=1,
    position_pct=100.0,
    interval=3_600_000,
):
    closes = [b.close for b in bars]
    rsi = rsi_series(closes, period)
    ema = ema_series(closes, trend_ema) if trend_ema else None
    trade = bars[n_warm:]
    cash, qty = float(budget), 0.0
    stop = None
    last_exit_bar = None
    day = day_start = None
    latched = False
    entries = 0
    n_trades = 0
    curve = []

    def sell(price, t):
        nonlocal cash, qty, stop, last_exit_bar
        p = price * (1 - slip)
        proceeds = qty * p
        cash += proceeds - proceeds * fee
        qty, stop = 0.0, None
        last_exit_bar = t - t % interval

    def latch_check(price):
        nonlocal latched
        if (
            not latched
            and daily_pct
            and day_start
            and (cash + qty * price - day_start) / day_start * 100 <= -daily_pct
        ):
            latched = True

    for j, bar in enumerate(trade):
        i = n_warm + j  # index into bars
        t = bar.open_time
        if day_of(t) != day:
            day, day_start, latched, entries = day_of(t), cash + qty * bar.open, False, 0
        if qty > 0 and stop is not None and bar.open <= stop:
            sell(bar.open, t)
        latch_check(bar.open)
        # The bar before (the last warm-up bar when j == 0) is decided at this open.
        prev = bars[i - 1]
        r = rsi[i - 1]
        fresh = t - prev.close_time <= interval
        ready = r is not None and (ema is None or ema[i - 1] is not None)
        if fresh and ready and r > overbought and qty > 0:
            sell(bar.open, t)
        elif (
            fresh
            and ready
            and r < oversold
            and qty == 0
            and (ema is None or prev.close > ema[i - 1])
        ):
            cooled = last_exit_bar is None or prev.open_time >= last_exit_bar + cooldown * interval
            if not latched and entries < max_entries and cooled:
                amount = min(cash * position_pct / 100, cash)
                p = bar.open * (1 + slip)
                q = amount / p
                cash -= q * p
                qty = q * (1 - fee)
                stop = p * (1 - stop_pct / 100) if stop_pct else None
                entries += 1
                n_trades += 1
        if qty > 0 and stop is not None and bar.low <= stop:
            p = min(bar.open, stop)
            sell(p, t + 1)
            latch_check(p)
        latch_check(bar.close)
        curve.append(cash + qty * bar.close)
    if qty > 0:
        sell(trade[-1].close, trade[-1].close_time)
        curve[-1] = cash
    return {"final": curve[-1], "entries": n_trades, "curve": curve}
