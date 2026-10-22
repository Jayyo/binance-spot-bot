from spotbot.strategy import RsiParams, RsiReversion, Signal


def feed(strategy, closes):
    return [strategy.update(c) for c in closes]


def falling(n, start=100.0, step=1.0):
    return [start - i * step for i in range(n)]


def rising(n, start=100.0, step=1.0):
    return [start + i * step for i in range(n)]


def test_holds_until_rsi_is_ready():
    s = RsiReversion(RsiParams(rsi_period=14))
    signals = feed(s, falling(14))
    assert signals == [Signal.HOLD] * 14
    assert s.update(80.0) is Signal.ENTER  # 15th close: first RSI value (0)


def test_entry_is_level_based_and_exit_above_overbought():
    s = RsiReversion()
    signals = feed(s, falling(20))
    assert signals[-5:] == [Signal.ENTER] * 5  # every bar below 30 is an entry signal
    signals = feed(s, rising(30, start=signals and 81.0))
    assert Signal.EXIT in signals
    assert s.rsi.value > 70


def test_neutral_zone_holds():
    s = RsiReversion()
    closes = [100.0 + (1 if i % 2 else -1) for i in range(40)]
    assert set(feed(s, closes)) == {Signal.HOLD}


def zigzag(n, start, up, down):
    out = [start]
    for i in range(1, n):
        out.append(out[-1] + (up if i % 2 else -down))
    return out


def test_trend_filter_blocks_entries_below_the_ema():
    plain = RsiReversion(RsiParams(trend_ema=0))
    filtered = RsiReversion(RsiParams(trend_ema=20))
    closes = rising(40, start=50) + falling(20, start=89, step=2)
    assert Signal.ENTER in feed(plain, closes)
    assert Signal.ENTER not in feed(filtered, closes)  # the sell-off took price below EMA(20)


def test_trend_filter_allows_dips_in_an_uptrend():
    s = RsiReversion(RsiParams(trend_ema=200))
    closes = zigzag(400, 100.0, up=6, down=2)
    closes += [closes[-1] - 10 * (i + 1) for i in range(7)]
    signals = feed(s, closes)
    assert signals[-1] is Signal.ENTER
    assert s.rsi.value < 30 and closes[-1] > s.ema.value


def test_describe_mentions_every_rule():
    assert RsiParams().describe() == "RSI(14) < 30 enter, > 70 exit"
    assert "EMA(200)" in RsiParams(trend_ema=200).describe()
