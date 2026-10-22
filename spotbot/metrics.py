"""Performance numbers, each with one fixed definition.

- Equity is sampled at every bar close (plus the starting value).
- Max drawdown uses those samples, so moves inside a bar are not seen.
- Sharpe uses daily returns (last sample of each UTC day), risk-free rate 0,
  annualised with sqrt(365).
- A trade is one entry until the position is flat again; its return is net of
  all fees. Holding time runs from the first to the last fill; a stop hit inside
  a bar is stamped at that bar's open, since OHLC data does not say when.
- Exposure ("bars in market") is the share of bars in which a position was held
  at any point, including a bar entered at its open and stopped out inside it.
"""

from __future__ import annotations

import math
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal

from .ledger import OrderRow
from .model import Fill, Side, utc_day


@dataclass(slots=True)
class Trade:
    entry_time: int
    exit_time: int
    entry_price: float
    exit_price: float
    qty: float
    cost: float  # quote spent, fees included
    proceeds: float  # quote received, fees deducted
    exit_reason: str

    @property
    def pnl(self) -> float:
        return self.proceeds - self.cost

    @property
    def ret(self) -> float:
        return self.pnl / self.cost if self.cost else 0.0

    @property
    def hours(self) -> float:
        return (self.exit_time - self.entry_time) / 3_600_000


def trades_from_ledger(
    orders: list[OrderRow],
    fills: list[Fill],
    base: str,
    quote: str,
    is_dust: Callable[[Decimal], bool] = lambda q: q <= Decimal("1e-12"),
) -> list[Trade]:
    by_order: dict[str, list[Fill]] = {}
    for f in fills:
        by_order.setdefault(f.client_id, []).append(f)
    trades: list[Trade] = []
    open_qty = Decimal(0)
    cost = proceeds = Decimal(0)
    bought = sold = sold_quote = Decimal(0)
    entry_time = 0
    for order in sorted(orders, key=lambda o: o.created_at):
        legs = by_order.get(order.client_id, [])
        if not legs:
            continue
        for f in legs:
            fee_quote = f.commission_quote or Decimal(0)
            if f.commission_asset == quote:
                fee_quote = f.commission
            if f.side is Side.BUY:
                if bought == 0:
                    entry_time = f.time
                received = f.qty - (f.commission if f.commission_asset == base else 0)
                open_qty += received
                bought += f.qty
                cost += f.quote_qty + fee_quote
            else:
                open_qty -= f.qty + (f.commission if f.commission_asset == base else 0)
                sold += f.qty
                sold_quote += f.quote_qty
                proceeds += f.quote_qty - fee_quote
        if order.side is Side.SELL and bought > 0 and is_dust(open_qty):
            trades.append(
                Trade(
                    entry_time=entry_time,
                    exit_time=legs[-1].time,
                    entry_price=float(cost / bought) if bought else 0.0,
                    exit_price=float(sold_quote / sold) if sold else 0.0,
                    qty=float(bought),
                    cost=float(cost),
                    proceeds=float(proceeds),
                    exit_reason=order.purpose,
                )
            )
            cost = proceeds = bought = sold = sold_quote = Decimal(0)
    return trades


def max_drawdown(values: list[float]) -> float:
    peak = -math.inf
    worst = 0.0
    for v in values:
        peak = max(peak, v)
        if peak > 0:
            worst = max(worst, (peak - v) / peak)
    return worst


def drawdown_series(values: list[float]) -> list[float]:
    peak = -math.inf
    out = []
    for v in values:
        peak = max(peak, v)
        out.append((v - peak) / peak if peak > 0 else 0.0)
    return out


def daily_closes(points: list[tuple[int, float]]) -> list[tuple[str, float]]:
    days: dict[str, float] = {}
    for t, v in points:
        days[utc_day(t)] = v
    return list(days.items())


def sharpe(points: list[tuple[int, float]]) -> float | None:
    closes = [v for _, v in daily_closes(points)]
    rets = [b / a - 1 for a, b in zip(closes, closes[1:], strict=False) if a > 0]
    if len(rets) < 2:
        return None
    mean = sum(rets) / len(rets)
    var = sum((r - mean) ** 2 for r in rets) / (len(rets) - 1)
    if var <= 0:
        return None
    return mean / math.sqrt(var) * math.sqrt(365)


def cagr(start_value: float, end_value: float, start_ms: int, end_ms: int) -> float | None:
    years = (end_ms - start_ms) / (365.25 * 86_400_000)
    if years <= 0 or start_value <= 0 or end_value <= 0:
        return None
    return (end_value / start_value) ** (1 / years) - 1


def yearly_returns(points: list[tuple[int, float]]) -> dict[int, float]:
    out: dict[int, float] = {}
    prev_value = points[0][1]
    year_end: dict[int, float] = {}
    for t, v in points:
        year_end[datetime.fromtimestamp(t / 1000, UTC).year] = v
    for year in sorted(year_end):
        out[year] = year_end[year] / prev_value - 1
        prev_value = year_end[year]
    return out


@dataclass(slots=True)
class Summary:
    start_value: float
    end_value: float
    total_return: float
    cagr: float | None
    max_drawdown: float
    sharpe: float | None
    exposure: float | None = None
    trades: int | None = None
    win_rate: float | None = None
    avg_win: float | None = None
    avg_loss: float | None = None
    profit_factor: float | None = None
    fees: float | None = None
    avg_hours: float | None = None


def summarize(
    points: list[tuple[int, float]],
    *,
    in_market: list[bool] | None = None,
    trades: list[Trade] | None = None,
    fees: float | None = None,
) -> Summary:
    values = [v for _, v in points]
    s = Summary(
        start_value=values[0],
        end_value=values[-1],
        total_return=values[-1] / values[0] - 1,
        cagr=cagr(values[0], values[-1], points[0][0], points[-1][0]),
        max_drawdown=max_drawdown(values),
        sharpe=sharpe(points),
        fees=fees,
    )
    if in_market:
        s.exposure = sum(in_market) / len(in_market)
    if trades is not None:
        s.trades = len(trades)
        wins = [t.ret for t in trades if t.pnl > 0]
        losses = [t.ret for t in trades if t.pnl <= 0]
        if trades:
            s.win_rate = len(wins) / len(trades)
            s.avg_hours = sum(t.hours for t in trades) / len(trades)
        s.avg_win = sum(wins) / len(wins) if wins else None
        s.avg_loss = sum(losses) / len(losses) if losses else None
        gross_loss = -sum(t.pnl for t in trades if t.pnl <= 0)
        gross_win = sum(t.pnl for t in trades if t.pnl > 0)
        s.profit_factor = gross_win / gross_loss if gross_loss > 0 else None
    return s
