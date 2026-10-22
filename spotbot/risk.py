"""Risk rules. They gate entries only; exits are never blocked by a limit.

Everything here is plain functions over a small state object so the same rules
run in the backtest, in paper mode and against the exchange.
"""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field
from decimal import Decimal

from .model import dec


@dataclass(frozen=True, slots=True)
class RiskConfig:
    position_pct: Decimal = Decimal(100)  # share of bot equity per entry
    max_position: Decimal | None = None  # absolute cap per entry, in quote asset
    stop_loss_pct: Decimal = Decimal(3)  # 0 disables the stop
    daily_loss_pct: Decimal = Decimal(2)  # 0 disables the daily latch
    max_entries_per_day: int = 10
    cooldown_bars: int = 1

    def __post_init__(self) -> None:
        if not 0 < self.position_pct <= 100:
            raise ValueError("position_pct must be in (0, 100]")
        if self.max_position is not None and self.max_position <= 0:
            raise ValueError("max_position must be positive")
        if not 0 <= self.stop_loss_pct < 100:
            raise ValueError("stop_loss_pct must be in [0, 100)")
        if not 0 <= self.daily_loss_pct < 100:
            raise ValueError("daily_loss_pct must be in [0, 100)")
        if self.max_entries_per_day < 1 or self.cooldown_bars < 0:
            raise ValueError("max_entries_per_day >= 1 and cooldown_bars >= 0")


@dataclass(slots=True)
class BotState:
    """Everything the engine must remember across restarts besides the ledger."""

    last_bar: int | None = None  # open time of the last closed bar processed
    day: str | None = None  # UTC date the counters below belong to
    day_start_equity: Decimal | None = None
    latched: bool = False  # entries blocked for the rest of the UTC day
    latch_reason: str = ""
    entries_today: int = 0
    last_exit_bar: int | None = None  # open time of the bar during which the last exit filled
    entry_price: Decimal | None = None
    stop_price: Decimal | None = None
    exit_reason: str | None = None  # an exit was decided and is not finished yet
    flags: dict[str, str] = field(default_factory=dict)  # conditions that need attention

    def to_json(self) -> str:
        data = asdict(self)
        for key in ("day_start_equity", "entry_price", "stop_price"):
            if data[key] is not None:
                data[key] = str(data[key])
        return json.dumps(data, sort_keys=True)

    @classmethod
    def from_json(cls, text: str) -> BotState:
        data = json.loads(text)
        for key in ("day_start_equity", "entry_price", "stop_price"):
            if data.get(key) is not None:
                data[key] = dec(data[key])
        return cls(**data)


UNKNOWN_BASELINE = "start-of-day equity unknown"


def roll_day(state: BotState, day: str, baseline: Decimal | None) -> bool:
    """Start new daily counters. A missing baseline latches the day (fail closed)."""
    if state.day == day:
        return False
    state.day = day
    state.entries_today = 0
    state.day_start_equity = baseline
    if baseline is None:
        state.latched = True
        state.latch_reason = UNKNOWN_BASELINE
    else:
        state.latched = False
        state.latch_reason = ""
    return True


def check_daily_loss(state: BotState, cfg: RiskConfig, equity: Decimal) -> bool:
    """Latch entries if equity fell `daily_loss_pct` below the day's starting equity."""
    if state.latched or cfg.daily_loss_pct == 0 or not state.day_start_equity:
        return False
    change_pct = (equity - state.day_start_equity) / state.day_start_equity * 100
    if change_pct <= -cfg.daily_loss_pct:
        state.latched = True
        state.latch_reason = f"daily loss {change_pct:.2f}% hit the -{cfg.daily_loss_pct}% limit"
        return True
    return False


def entry_blocker(state: BotState, cfg: RiskConfig, bar_open: int, interval_ms: int) -> str | None:
    """Reason an entry signal for the bar opening at `bar_open` must be ignored, if any."""
    if state.latched:
        return f"entries paused for today: {state.latch_reason}"
    if state.entries_today >= cfg.max_entries_per_day:
        return f"entry cap reached ({cfg.max_entries_per_day} per UTC day)"
    if state.last_exit_bar is not None:
        allowed_from = state.last_exit_bar + cfg.cooldown_bars * interval_ms
        if bar_open < allowed_from:
            return "cooldown after the last exit"
    return None


def entry_quote(cfg: RiskConfig, equity: Decimal, cash: Decimal) -> Decimal:
    """Quote amount to spend on an entry: share of equity, capped and limited by cash."""
    amount = equity * cfg.position_pct / 100
    if cfg.max_position is not None:
        amount = min(amount, cfg.max_position)
    return max(Decimal(0), min(amount, cash))


def stop_for(entry_price: Decimal, cfg: RiskConfig) -> Decimal | None:
    if cfg.stop_loss_pct == 0:
        return None
    return entry_price * (1 - cfg.stop_loss_pct / 100)
