"""RSI mean-reversion rules for a single long-only spot position.

The rules are deliberately simple so every decision can be traced by hand:

- ENTER when RSI(period) of the last closed bar is below `oversold`
  (and, if `trend_ema` > 0, the close is above that EMA).
- EXIT when RSI is above `overbought`.

The entry is level-based, not a crossover: while RSI stays below the threshold
every bar is an entry signal. The risk layer (cooldown, daily loss latch, entry
cap) decides whether a signal becomes an order.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum

from .indicators import Ema, Rsi


class Signal(StrEnum):
    HOLD = "HOLD"
    ENTER = "ENTER"
    EXIT = "EXIT"


@dataclass(frozen=True, slots=True)
class RsiParams:
    rsi_period: int = 14
    oversold: float = 30.0
    overbought: float = 70.0
    trend_ema: int = 0  # 0 disables the trend filter

    def __post_init__(self) -> None:
        if not 0 < self.oversold < self.overbought < 100:
            raise ValueError("need 0 < oversold < overbought < 100")
        if self.trend_ema < 0:
            raise ValueError("trend_ema must be >= 0")

    @property
    def warmup_bars(self) -> int:
        """Bars needed before the first signal can be produced."""
        return max(self.rsi_period + 1, self.trend_ema)

    def describe(self) -> str:
        text = f"RSI({self.rsi_period}) < {self.oversold:g} enter, > {self.overbought:g} exit"
        if self.trend_ema:
            text += f", enter only above EMA({self.trend_ema})"
        return text


class RsiReversion:
    def __init__(self, params: RsiParams | None = None) -> None:
        self.params = params or RsiParams()
        self.rsi = Rsi(self.params.rsi_period)
        self.ema = Ema(self.params.trend_ema) if self.params.trend_ema else None
        self.last_close: float | None = None

    @property
    def ready(self) -> bool:
        return self.rsi.value is not None and (self.ema is None or self.ema.value is not None)

    def update(self, close: float) -> Signal:
        self.last_close = close
        rsi = self.rsi.update(close)
        ema = self.ema.update(close) if self.ema else None
        if not self.ready or rsi is None:
            return Signal.HOLD
        p = self.params
        if rsi > p.overbought:
            return Signal.EXIT
        if rsi < p.oversold and (self.ema is None or (ema is not None and close > ema)):
            return Signal.ENTER
        return Signal.HOLD
