"""Replay historical bars through the same Engine used for paper and live trading.

Per bar the driver does, in this order:

1. open:   tick at the open price (day roll, gap through the stop), then hand the
           previous bar to the strategy; any order fills at this open.
2. inside: if the low reached the stop, tick once at min(open, stop).
3. close:  tick at the close (daily-loss latch) and record equity.

Fills pay `fee_bps` and lose `slippage_bps` on every side. Exchange filters
(lot size, minimum notional) are not applied: today's filters say nothing about
past ones, and the default sizes are far above them anyway.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime
from decimal import Decimal

from .data import Dataset
from .engine import Engine, EngineConfig
from .filters import SymbolRules
from .ledger import Store
from .metrics import Summary, Trade, summarize, trades_from_ledger
from .model import Bar, dec
from .risk import RiskConfig
from .strategy import RsiParams, RsiReversion
from .venues import Quote, SimVenue

QUOTE_ASSETS = (
    "USDT",
    "USDC",
    "FDUSD",
    "TUSD",
    "BUSD",
    "EUR",
    "TRY",
    "BRL",
    "JPY",
    "BTC",
    "ETH",
    "BNB",
)


def split_symbol(symbol: str) -> tuple[str, str]:
    for q in QUOTE_ASSETS:
        if symbol.endswith(q) and len(symbol) > len(q):
            return symbol[: -len(q)], q
    raise ValueError(f"cannot tell the quote asset of {symbol}")


def next_month(month: str) -> str:
    y, m = (int(x) for x in month.split("-"))
    return f"{y + m // 12:04d}-{m % 12 + 1:02d}"


def month_start_ms(month: str) -> int:
    y, m = (int(x) for x in month.split("-"))
    return int(datetime(y, m, 1, tzinfo=UTC).timestamp() * 1000)


@dataclass(frozen=True)
class BacktestConfig:
    symbol: str
    interval: str
    start: str  # first traded month, YYYY-MM
    end: str  # last traded month, YYYY-MM
    params: RsiParams = field(default_factory=RsiParams)
    risk: RiskConfig = field(default_factory=RiskConfig)
    budget: Decimal = Decimal(10_000)
    fee_bps: Decimal = Decimal(10)
    slippage_bps: Decimal = Decimal(5)


@dataclass
class BacktestResult:
    cfg: BacktestConfig
    data: Dataset
    bars: list[Bar]  # traded bars (warm-up excluded)
    warmup_bars: int
    points: list[tuple[int, float]]
    hold_points: list[tuple[int, float]]
    in_market: list[bool]
    trades: list[Trade]
    summary: Summary
    hold: Summary
    store: Store
    base: str
    quote: str


def run(cfg: BacktestConfig, data: Dataset) -> BacktestResult:
    base, quote_asset = split_symbol(cfg.symbol)
    start_ms = month_start_ms(cfg.start)
    end_ms = month_start_ms(next_month(cfg.end))
    warm = [b for b in data.bars if b.open_time < start_ms]
    bars = [b for b in data.bars if start_ms <= b.open_time < end_ms]
    if not bars:
        raise ValueError("no bars inside the test period")
    if len(warm) < cfg.params.warmup_bars + 1:
        raise ValueError(
            f"need {cfg.params.warmup_bars} bars before {cfg.start} to warm up the indicators, have {len(warm)}"
        )

    quote = Quote()
    store = Store(":memory:")
    rules = SymbolRules.unrestricted(cfg.symbol, base, quote_asset)
    fee = cfg.fee_bps / Decimal(10_000)
    slip = cfg.slippage_bps / Decimal(10_000)
    venue = SimVenue("backtest", rules, quote, store, fee_rate=fee, slippage=slip)
    engine = Engine(
        EngineConfig(
            symbol=cfg.symbol,
            interval=cfg.interval,
            budget=cfg.budget,
            risk=cfg.risk,
            account="backtest",
            namespace="bt",
            record_events=False,
        ),
        venue,
        store,
        RsiReversion(cfg.params),
        quote,
    )
    # The last warm-up bar is decided at the first open of the period, so trading can
    # start on the period's first bar.
    engine.warmup(warm[:-1])
    prev = warm[-1]

    points = [(bars[0].open_time, float(cfg.budget))]
    in_market: list[bool] = []
    for bar in bars:
        o = dec(bar.open)
        quote.set(o, o, bar.open_time)
        engine.on_tick(bar.open_time)
        engine.on_bar_close(prev, bar.open_time)
        held = engine.has_position()  # after any entry at this open
        stop = engine.state.stop_price
        if stop is not None and held and dec(bar.low) <= stop:
            p = min(o, stop)
            quote.set(p, p, bar.open_time + 1)
            engine.on_tick(bar.open_time + 1)
        c = dec(bar.close)
        quote.set(c, c, bar.close_time)
        engine.on_tick(bar.close_time)
        points.append((bar.close_time, float(engine.equity())))
        in_market.append(held or engine.has_position())
        prev = bar

    # Close out at the last close so the result is net of the exit costs too.
    engine.liquidate(bars[-1].close_time, "end of test")
    points[-1] = (bars[-1].close_time, float(engine.equity()))

    hold_points = buy_and_hold(bars, float(cfg.budget), float(fee), float(slip))
    fills = store.fills(cfg.symbol)
    trades = trades_from_ledger(store.orders(cfg.symbol), fills, base, quote_asset)
    return BacktestResult(
        cfg=cfg,
        data=data,
        bars=bars,
        warmup_bars=len(warm),
        points=points,
        hold_points=hold_points,
        in_market=in_market,
        trades=trades,
        summary=summarize(
            points, in_market=in_market, trades=trades, fees=float(engine.book.fees_quote)
        ),
        hold=summarize(hold_points),
        store=store,
        base=base,
        quote=quote_asset,
    )


def buy_and_hold(
    bars: list[Bar], budget: float, fee: float, slip: float
) -> list[tuple[int, float]]:
    """Buy at the first open, sell at the last close, same fee and slippage as the bot."""
    qty = budget / (bars[0].open * (1 + slip)) * (1 - fee)
    points = [(bars[0].open_time, budget)]
    points += [(b.close_time, qty * b.close) for b in bars]
    points[-1] = (bars[-1].close_time, qty * bars[-1].close * (1 - slip) * (1 - fee))
    return points
