from decimal import Decimal as D

import pytest

from spotbot import risk
from spotbot.risk import BotState, RiskConfig

H = 3_600_000


def test_roll_day_resets_counters_and_latch():
    s = BotState(day="2025-01-01", latched=True, latch_reason="x", entries_today=7)
    assert risk.roll_day(s, "2025-01-02", D(1000))
    assert (s.day, s.latched, s.entries_today, s.day_start_equity) == (
        "2025-01-02",
        False,
        0,
        D(1000),
    )
    assert not risk.roll_day(s, "2025-01-02", D(5))  # same day: nothing changes
    assert s.day_start_equity == D(1000)


def test_unknown_baseline_fails_closed():
    s = BotState()
    risk.roll_day(s, "2025-01-02", None)
    assert s.latched and s.latch_reason == risk.UNKNOWN_BASELINE


@pytest.mark.parametrize(
    ("equity", "latched"), [(D("980"), True), (D("980.01"), False), (D("900"), True)]
)
def test_daily_loss_latch_threshold(equity, latched):
    s = BotState(day="d", day_start_equity=D(1000))
    risk.check_daily_loss(s, RiskConfig(daily_loss_pct=D(2)), equity)
    assert s.latched is latched


def test_daily_loss_disabled():
    s = BotState(day="d", day_start_equity=D(1000))
    risk.check_daily_loss(s, RiskConfig(daily_loss_pct=D(0)), D(1))
    assert not s.latched


def test_entry_blocker_order_and_cooldown_boundary():
    cfg = RiskConfig(max_entries_per_day=2, cooldown_bars=2)
    s = BotState(last_exit_bar=10 * H)
    assert "cooldown" in risk.entry_blocker(s, cfg, 11 * H, H)
    assert risk.entry_blocker(s, cfg, 12 * H, H) is None
    s.entries_today = 2
    assert "entry cap" in risk.entry_blocker(s, cfg, 12 * H, H)
    s.latched, s.latch_reason = True, "daily loss"
    assert "paused" in risk.entry_blocker(s, cfg, 12 * H, H)


def test_exit_bar_itself_never_reenters_even_without_cooldown():
    cfg = RiskConfig(cooldown_bars=0)
    s = BotState(last_exit_bar=10 * H)
    assert risk.entry_blocker(s, cfg, 9 * H, H) is not None  # a bar that closed before the exit
    assert risk.entry_blocker(s, cfg, 10 * H, H) is None


def test_entry_quote_uses_the_smallest_limit():
    assert risk.entry_quote(RiskConfig(), D(1000), D(1000)) == D(1000)
    assert risk.entry_quote(RiskConfig(position_pct=D(25)), D(1000), D(1000)) == D(250)
    assert risk.entry_quote(RiskConfig(max_position=D(100)), D(1000), D(1000)) == D(100)
    assert risk.entry_quote(RiskConfig(), D(1000), D(40)) == D(40)
    assert risk.entry_quote(RiskConfig(), D(1000), D(-5)) == D(0)


def test_stop_for():
    assert risk.stop_for(D(100), RiskConfig(stop_loss_pct=D(3))) == D(97)
    assert risk.stop_for(D(100), RiskConfig(stop_loss_pct=D(0))) is None


@pytest.mark.parametrize(
    "kwargs",
    [{"position_pct": D(0)}, {"position_pct": D(101)}, {"max_position": D(0)}, {"stop_loss_pct": D(100)},
     {"daily_loss_pct": D(-1)}, {"max_entries_per_day": 0}, {"cooldown_bars": -1}],
)  # fmt: skip
def test_config_validation(kwargs):
    with pytest.raises(ValueError):
        RiskConfig(**kwargs)


def test_state_json_round_trip_keeps_decimals():
    s = BotState(last_bar=1, day="2025-01-01", day_start_equity=D("1000.5"), entry_price=D("0.1"),
                 stop_price=D("0.097"), flags={"stop_file": "x"})  # fmt: skip
    again = BotState.from_json(s.to_json())
    assert again == s
    assert isinstance(again.entry_price, D)
