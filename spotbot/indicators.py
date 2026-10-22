"""Incremental indicators. Each update() takes the next closed bar's close."""

from __future__ import annotations


def rsi_from_averages(avg_gain: float, avg_loss: float) -> float:
    if avg_gain == 0 and avg_loss == 0:
        return 50.0  # flat series: neither overbought nor oversold
    if avg_loss == 0:
        return 100.0
    return 100.0 - 100.0 / (1.0 + avg_gain / avg_loss)


class Rsi:
    """Wilder's RSI: simple average of the first `period` changes, then smoothing."""

    def __init__(self, period: int = 14) -> None:
        if period < 2:
            raise ValueError("RSI period must be at least 2")
        self.period = period
        self.value: float | None = None
        self._prev: float | None = None
        self._seed_gain = 0.0
        self._seed_loss = 0.0
        self._seed_n = 0
        self._avg_gain: float | None = None
        self._avg_loss: float | None = None

    def update(self, close: float) -> float | None:
        prev, self._prev = self._prev, close
        if prev is None:
            return None
        change = close - prev
        gain = change if change > 0 else 0.0
        loss = -change if change < 0 else 0.0
        if self._avg_gain is None or self._avg_loss is None:
            self._seed_gain += gain
            self._seed_loss += loss
            self._seed_n += 1
            if self._seed_n < self.period:
                return None
            self._avg_gain = self._seed_gain / self.period
            self._avg_loss = self._seed_loss / self.period
        else:
            n = self.period
            self._avg_gain = (self._avg_gain * (n - 1) + gain) / n
            self._avg_loss = (self._avg_loss * (n - 1) + loss) / n
        self.value = rsi_from_averages(self._avg_gain, self._avg_loss)
        return self.value


class Ema:
    """Exponential moving average seeded with the simple average of the first `period` closes."""

    def __init__(self, period: int) -> None:
        if period < 1:
            raise ValueError("EMA period must be positive")
        self.period = period
        self.value: float | None = None
        self._alpha = 2.0 / (period + 1)
        self._seed: list[float] = []

    def update(self, close: float) -> float | None:
        if self.value is None:
            self._seed.append(close)
            if len(self._seed) < self.period:
                return None
            self.value = sum(self._seed) / self.period
            self._seed = []
        else:
            self.value += self._alpha * (close - self.value)
        return self.value
