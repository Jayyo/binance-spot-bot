"""The trading engine: strategy signals in, orders out, ledger and risk in between.

Order of operations is the same in every mode:

- on_tick(now, fresh): reconcile unfinished orders, roll the UTC day, check the
  STOP file; with a fresh quote also check the stop, send a pending exit and
  update the daily-loss latch.
- on_bar_close(bar, now, fresh): feed the closed bar to the strategy, then act
  on the signal at the current price (in a backtest: the next bar's open).

Invariants the tests lean on:

- At most one order is unfinished at any time. While an order's outcome is
  unknown, nothing new is sent; the engine keeps asking the exchange.
- Sell orders are capped at the quantity attributed to the bot's own fills.
- An exit, once decided (stop, RSI exit, end of test), is recorded first and sent
  when nothing else is in flight; it is retried across ticks and restarts until
  the bot is flat. A position that cannot be sold right now keeps its stop.
- A state directory belongs to one account, symbol and budget; strategy and
  risk settings can only change while the bot is flat.
"""

from __future__ import annotations

import json
import logging
import secrets
from collections.abc import Callable
from dataclasses import asdict, dataclass, field
from decimal import Decimal
from pathlib import Path

from . import risk
from .binance import Ambiguous, BinanceError, NotSent, RateLimited, Rejected
from .filters import ceil_to_step, floor_to_step
from .ledger import Book, LedgerConflict, OrderRow, Store, book_from_fills
from .model import (
    DAY_MS,
    Bar,
    Fill,
    OrderUpdate,
    Side,
    Status,
    day_start_ms,
    dec,
    fmt_price,
    fmt_qty,
    fmt_time,
    interval_ms,
    utc_day,
)
from .risk import BotState, RiskConfig
from .strategy import RsiReversion, Signal
from .venues import Quote, Venue

log = logging.getLogger(__name__)

EXIT_RETRY_MS = 30_000


@dataclass(frozen=True)
class EngineConfig:
    symbol: str
    interval: str
    budget: Decimal
    risk: RiskConfig
    account: str = "local"  # which exchange account this state belongs to
    namespace: str | None = None  # client order id prefix; random per state if None
    unknown_grace_ms: int = 120_000  # how long "order not found" may still mean "in flight"
    attention_ms: int = 600_000  # unresolved order this old needs a human
    stale_ms: int = 30_000  # no new orders on market data older than this
    kill_switch: Path | None = None  # entries pause while this file exists
    record_events: bool = True


@dataclass(slots=True)
class Snapshot:
    mode: str
    symbol: str
    base_asset: str
    quote_asset: str
    interval: str
    now: int
    bid: Decimal
    ask: Decimal
    quote_age_ms: int
    rsi: float | None
    ema: float | None
    params: str
    budget: Decimal
    cash: Decimal
    qty: Decimal
    equity: Decimal
    entry_price: Decimal | None
    stop_price: Decimal | None
    exit_reason: str | None
    day_start_equity: Decimal | None
    day_change_pct: Decimal | None
    entries_today: int
    max_entries: int
    daily_loss_pct: Decimal
    latched: bool
    latch_reason: str
    flags: dict[str, str]
    open_orders: list[str]
    unvalued_fees: dict[str, Decimal]
    fees_quote: Decimal
    last_bar: int | None
    events: list[tuple[int, str, str]] = field(default_factory=list)


class EngineError(Exception):
    pass


def _norm(value: object) -> object:
    return format(value.normalize(), "f") if isinstance(value, Decimal) else value


class Engine:
    def __init__(
        self,
        cfg: EngineConfig,
        venue: Venue,
        store: Store,
        strategy: RsiReversion,
        quote: Quote,
        on_event: Callable[[int, str, str], None] | None = None,
        clock: Callable[[], int] | None = None,
    ) -> None:
        self.cfg = cfg
        self._clock = clock  # real time for checks made after network calls
        self.venue = venue
        self.rules = venue.rules
        self.store = store
        self.strategy = strategy
        self.quote = quote
        self.on_event = on_event
        self.interval_ms = interval_ms(cfg.interval)
        self.base, self.quote_asset = self.rules.base_asset, self.rules.quote_asset
        self.state = store.load_state(cfg.symbol) or BotState()
        self.book = self._rebuild_book()
        self._pending_events: list[str] = []
        self._bind_state()
        self._last_block: str | None = None
        self._last_exit_problem: str | None = None
        self._dirty = False
        self._day_index = -1
        self._retry_baseline_at = 0
        self._retry_exit_at = 0
        # Simulated venues report no balances, so there is nothing to compare.
        self._balance_check_at = 0 if venue.mode in ("testnet", "live") else 2**62

    # ----- setup -------------------------------------------------------------

    def _settings(self) -> str:
        return json.dumps(
            {
                "interval": self.cfg.interval,
                "strategy": asdict(self.strategy.params),
                "risk": {k: _norm(v) for k, v in asdict(self.cfg.risk).items()},
            },
            sort_keys=True,
        )

    def _bind_state(self) -> None:
        """Tie the state to one account, symbol and budget; settings change only when flat."""
        meta = self.store.get_meta
        if meta("budget") is None:
            for key, value in (
                ("budget", str(self.cfg.budget)),
                ("symbol", self.cfg.symbol),
                ("base_asset", self.base),
                ("quote_asset", self.quote_asset),
                ("account", self.cfg.account),
                ("settings", self._settings()),
                ("namespace", self.cfg.namespace or secrets.token_hex(3)),
            ):
                self.store.set_meta(key, value)
        if meta("symbol") != self.cfg.symbol:
            raise EngineError(
                f"this state belongs to {meta('symbol')}; use one --state-dir per symbol"
            )
        if meta("account") != self.cfg.account:
            raise EngineError(
                f"this state belongs to account {meta('account')}, not {self.cfg.account}; "
                "use a separate --state-dir for each account"
            )
        if dec(meta("budget")) != self.cfg.budget:
            raise EngineError(
                f"this state was started with a budget of {meta('budget')} {self.quote_asset}; "
                "use the same --budget or a new --state-dir"
            )
        if meta("settings") != self._settings():
            if self.has_position() or self.open_orders():
                raise EngineError(
                    "strategy or risk settings differ from the ones this position was opened with; "
                    f"restart with the same settings until the bot is flat (stored: {meta('settings')})"
                )
            self.store.set_meta("settings", self._settings())
            self.state.last_bar = None  # the interval may have changed; warm-up sets it again
            self._pending_events.append("settings changed while flat; the new ones apply from now")
        self.namespace = meta("namespace") or "x"

    def _rebuild_book(self) -> Book:
        fills = self.store.fills(self.cfg.symbol)
        self._last_fill_time = fills[-1].time if fills else -1
        return book_from_fills(self.cfg.budget, fills, self.base, self.quote_asset)

    def warmup(self, bars: list[Bar]) -> None:
        """Feed history to the strategy without trading on it."""
        for bar in bars:
            self.strategy.update(bar.close)
        if bars:
            last = bars[-1].open_time
            if self.state.last_bar is None or last > self.state.last_bar:
                self.state.last_bar = last
                self._save()

    # ----- helpers -----------------------------------------------------------

    def _event(self, now: int, level: str, message: str) -> None:
        log.log(logging.WARNING if level == "warn" else logging.INFO, message)
        if self.cfg.record_events:
            self.store.add_event(now, level, message)
        if self.on_event:
            self.on_event(now, level, message)

    def _save(self) -> None:
        self.store.save_state(self.cfg.symbol, self.state)
        self._dirty = False

    def position_qty(self) -> Decimal:
        """The bot's inventory rounded to the lot size; below the minimum lot it is dust."""
        return self.rules.lots(self.book.qty)

    def has_position(self) -> bool:
        return self.position_qty() > 0

    def equity(self) -> Decimal:
        price = self.quote.bid if self.quote.bid > 0 else Decimal(0)
        return self.book.equity(price)

    def open_orders(self) -> list[OrderRow]:
        return self.store.open_orders(self.cfg.symbol)

    def _client_id(self, purpose: str, now: int) -> str:
        seq = int(self.store.get_meta("order_seq") or "0") + 1
        self.store.set_meta("order_seq", str(seq))
        return f"sb{self.venue.mode[0]}{purpose[0]}{self.namespace}-{now}-{seq}"[:36]

    def _bar_of(self, now: int) -> int:
        return now - now % self.interval_ms

    def _is_fresh(self, now: int) -> bool:
        return self.quote.bid > 0 and now - self.quote.time <= self.cfg.stale_ms

    def _now(self, fallback: int) -> int:
        return self._clock() if self._clock else fallback

    # ----- inputs ------------------------------------------------------------

    def on_tick(self, now: int, fresh: bool = True) -> None:
        """`fresh` is False when the latest quote could not be fetched: control checks still
        run, anything priced off the quote waits."""
        while self._pending_events:
            self._event(now, "info", self._pending_events.pop(0))
        self.reconcile(now)
        self._roll_day(now)
        self._check_kill_switch(now)
        self._check_balances_due(now)
        if fresh and self._is_fresh(now):
            self._check_exits(now)
            self._check_daily_loss(now)
        if self._dirty:
            self._save()

    def _check_balances_due(self, now: int) -> None:
        """Compare the ledger with the account once nothing is in flight, then every few minutes."""
        if now < self._balance_check_at or self.open_orders():
            return
        self.check_balances(now)
        self._balance_check_at = now + (60_000 if "balance" in self.state.flags else 300_000)

    def _check_daily_loss(self, now: int) -> None:
        if risk.check_daily_loss(self.state, self.cfg.risk, self.equity()):
            self._event(
                now,
                "warn",
                f"entries paused for the rest of the UTC day: {self.state.latch_reason}",
            )
            self._dirty = True

    def _check_exits(self, now: int) -> None:
        stop = self.state.stop_price
        busy = bool(self.open_orders())
        if not self.has_position():
            if not busy and (stop is not None or self.state.exit_reason):
                self._clear_position(now)
            return
        if self.state.exit_reason is None and stop is not None and self.quote.bid <= stop:
            self._decide_exit(
                now, "stop", f"stop {fmt_price(stop)} hit, bid {fmt_price(self.quote.bid)}"
            )
        if self.state.exit_reason and not busy and now >= self._retry_exit_at:
            self._exit(now)

    def on_bar_close(self, bar: Bar, now: int, fresh: bool = True) -> Signal | None:
        if self.state.last_bar is not None and bar.open_time <= self.state.last_bar:
            return None
        signal = self.strategy.update(bar.close)
        self.state.last_bar = bar.open_time
        self._save()
        action = self._act(signal, bar, now, fresh)
        if self._dirty:
            self._save()
        if log.isEnabledFor(logging.INFO):
            rsi = self.strategy.rsi.value
            log.info(
                "bar %s close %s RSI %s: %s",
                fmt_time(bar.open_time),
                fmt_price(bar.close),
                "-" if rsi is None else f"{rsi:.1f}",
                action,
            )
        return signal

    def _act(self, signal: Signal, bar: Bar, now: int, fresh: bool) -> str:
        if now - bar.close_time > self.interval_ms:
            self._event(
                now, "info", f"bar {fmt_time(bar.open_time)} closed too long ago; not acting on it"
            )
            return "too old to act on"
        self.reconcile(now)
        self._roll_day(now)  # the bar may have been fetched across midnight
        fresh = fresh and self._is_fresh(now)
        if fresh:
            self._check_daily_loss(now)
        long = self.has_position()
        if signal is Signal.EXIT and long:
            if self.state.exit_reason:
                return "exit already in progress"
            rsi = self.strategy.rsi.value
            self._decide_exit(now, "exit", f"RSI {rsi:.1f} > {self.strategy.params.overbought:g}")
            if fresh and not self.open_orders():
                self._exit(now)
                return "exit"
            return "exit pending (unresolved order or no fresh price)"
        if signal is Signal.ENTER and not long:
            reason = self._entry_blocker(bar, now) if fresh else "market data is stale"
            if reason:
                if reason != self._last_block:
                    self._event(now, "info", f"entry signal ignored: {reason}")
                self._last_block = reason
                return f"entry signal ignored ({reason})"
            self._last_block = None
            self._enter(now, bar)
            return "entry"
        return "holding" if long else "flat"

    def record_equity(self, now: int) -> None:
        self.store.add_equity(now, self.equity(), self.quote.bid, self.book.qty)

    # ----- risk gates --------------------------------------------------------

    def _entry_blocker(self, bar: Bar, now: int) -> str | None:
        if self.open_orders():
            return "an order is still unresolved"
        self._check_kill_switch(now)
        if self.state.flags:
            return "; ".join(self.state.flags.values())
        return risk.entry_blocker(self.state, self.cfg.risk, bar.open_time, self.interval_ms)

    def _check_kill_switch(self, now: int) -> None:
        path = self.cfg.kill_switch
        if path is None:
            return
        present = path.exists()
        if present and "stop_file" not in self.state.flags:
            self.state.flags["stop_file"] = f"{path} exists"
            self._event(now, "warn", f"{path} found: entries paused (exits and stops still run)")
            self._dirty = True
        elif not present and "stop_file" in self.state.flags:
            del self.state.flags["stop_file"]
            self._event(now, "info", f"{path} removed: entries allowed again")
            self._dirty = True

    def _roll_day(self, now: int) -> None:
        index = now // DAY_MS
        if index == self._day_index:
            return
        day = utc_day(now)
        if self.state.day != day:
            baseline = self._equity_at(day_start_ms(now))
            risk.roll_day(self.state, day, baseline)
            self._dirty = True
            if baseline is None:
                self._retry_baseline_at = now + 60_000
                self._event(
                    now, "warn", "could not value the bot at 00:00 UTC; entries paused until it can"
                )
        elif self.state.day_start_equity is None:
            if now < self._retry_baseline_at:
                return
            baseline = self._equity_at(day_start_ms(now))
            if baseline is None:
                self._retry_baseline_at = now + 60_000
                return
            self._set_baseline(now, baseline)
        if self.state.day_start_equity is not None:
            self._day_index = index

    def _set_baseline(self, now: int, baseline: Decimal) -> None:
        self.state.day_start_equity = baseline
        if self.state.latch_reason == risk.UNKNOWN_BASELINE:
            self.state.latched, self.state.latch_reason = False, ""
            self._event(now, "info", "start-of-day equity recovered; entries allowed")
        self._dirty = True

    def _equity_at(self, t: int) -> Decimal | None:
        """Bot equity at time t, rebuilt from fills before t and the price at t."""
        if self._last_fill_time < t:
            book = self.book
        else:
            fills = [f for f in self.store.fills(self.cfg.symbol) if f.time < t]
            book = book_from_fills(self.cfg.budget, fills, self.base, self.quote_asset)
        if self.rules.lots(book.qty) <= 0:
            # Flat (or only dust): the price at t barely matters.
            return book.equity(self.quote.bid)
        price = self.venue.price_at(t)
        return None if price is None else book.equity(price)

    def _fills_before_midnight(self, now: int, new: list[Fill]) -> None:
        """A fill from before 00:00 that is only recorded now changes today's baseline."""
        if self.state.day != utc_day(now) or not any(f.time < day_start_ms(now) for f in new):
            return
        baseline = self._equity_at(day_start_ms(now))
        if baseline is None:
            self.state.day_start_equity = None
            if not self.state.latched:  # a daily-loss latch stays as it is
                self.state.latched, self.state.latch_reason = True, risk.UNKNOWN_BASELINE
            self._retry_baseline_at = now + 60_000
            self._day_index = -1
        else:
            self._set_baseline(now, baseline)
            self._event(
                now, "info", "start-of-day equity recomputed after a late fill from yesterday"
            )

    # ----- orders --------------------------------------------------------------

    def _enter(self, now: int, bar: Bar) -> None:
        equity = self.equity()
        amount = risk.entry_quote(self.cfg.risk, equity, self.book.cash)
        free = self.venue.free_balance(self.quote_asset)
        if free is not None:
            amount = min(amount, free)
            later = self._now(now)  # the balance request took time; check again
            if not self._is_fresh(later) or utc_day(later) != self.state.day:
                self._event(
                    now, "info", "entry skipped: the price or the UTC day changed while deciding"
                )
                return
        amount = self.rules.buy_quote(amount)
        problem = self.rules.notional_problem(amount)
        if amount <= 0 or problem:
            self._event(now, "warn", f"entry skipped: {problem or 'no cash available'}")
            return
        if self.rules.quote_market_allowed or self.venue.mode == "backtest":
            self._submit(now, Side.BUY, "entry", bar.open_time, quote_qty=amount)
        else:
            qty = self.rules.sell_qty(amount / self.quote.ask) if self.quote.ask > 0 else Decimal(0)
            if qty <= 0:
                self._event(now, "warn", "entry skipped: quantity rounds to zero")
                return
            self._submit(now, Side.BUY, "entry", bar.open_time, qty=qty)

    def _decide_exit(self, now: int, purpose: str, why: str) -> None:
        """Record that the position must go; the sell follows when nothing is in flight."""
        self.state.exit_reason = purpose
        self._event(now, "info", f"{purpose}: {why}")
        self._save()

    def _exit(self, now: int) -> None:
        """Send a sell for the pending exit: the whole position, or what can be sold now."""
        qty = self.position_qty()
        free = self.venue.free_balance(self.base)
        if free is not None and free < qty:
            self.state.flags["balance"] = (
                f"exchange holds {free} {self.base}, less than the bot's {qty}; check the account"
            )
            self._dirty = True
            qty = self.rules.lots(free)
        price = self.quote.bid
        problem = None
        if qty <= 0:
            problem = "nothing sellable on the exchange"
        elif price > 0:
            qty = self._chunk(qty, price)
        if qty > 0 and price > 0:
            problem = self.rules.notional_problem(qty * price)
        if problem:
            if problem != self._last_exit_problem:
                self._event(now, "warn", f"cannot sell yet ({problem}); keeping the stop, retrying")
            self._last_exit_problem = problem
            self._retry_exit_at = now + EXIT_RETRY_MS
            return
        self._last_exit_problem = None
        self._submit(now, Side.SELL, self.state.exit_reason or "exit", None, qty=qty)

    def _chunk(self, qty: Decimal, price: Decimal) -> Decimal:
        """Sell size under the exchange's maximums that leaves a remainder it will still accept."""
        r = self.rules
        caps = [floor_to_step(r.max_qty, r.step)] if r.max_qty > 0 else []
        if r.max_notional_market and r.max_notional > 0:
            caps.append(floor_to_step(r.max_notional / price, r.step))
        largest = min(caps, default=qty)
        if qty <= largest:
            return qty
        keep = r.min_qty  # the rest must still be a valid order on its own
        if r.min_notional_market and r.min_notional > 0:
            keep = max(keep, ceil_to_step(r.min_notional / price, r.step))
        if qty - largest >= keep:
            return largest
        smaller = floor_to_step(qty - keep, r.step)
        return smaller if smaller > 0 and smaller >= r.min_qty else largest

    def _submit(
        self,
        now: int,
        side: Side,
        purpose: str,
        decision_bar: int | None,
        *,
        quote_qty: Decimal | None = None,
        qty: Decimal | None = None,
    ) -> None:
        cid = self._client_id(purpose, now)
        row = OrderRow(
            client_id=cid,
            symbol=self.cfg.symbol,
            side=side,
            purpose=purpose,
            quote_qty=quote_qty,
            qty=qty,
            decision_bar=decision_bar,
            status=Status.PENDING_SEND,
            order_id=None,
            executed_qty=Decimal(0),
            cumulative_quote=Decimal(0),
            created_at=now,
            updated_at=now,
            next_check_at=now,
            checks=0,
            error="",
        )
        with self.store.tx():
            self.store.insert_order(row)
            if purpose == "entry":
                self.state.entries_today += 1
            self._save()
        size = (
            f"{quote_qty} {self.quote_asset}"
            if quote_qty is not None
            else f"{fmt_qty(qty or Decimal(0))} {self.base}"
        )
        try:
            update = self.venue.place_market(side, cid, quote_qty=quote_qty, qty=qty)
        except (NotSent, RateLimited, Rejected) as exc:
            self.store.update_order(cid, status=Status.REJECTED, error=str(exc), updated_at=now)
            self._event(now, "warn", f"{side.value} {size} not placed: {exc}")
            if side is Side.SELL:
                self._retry_exit_at = now + EXIT_RETRY_MS  # the exit stays pending
            return
        except Ambiguous as exc:
            self.store.update_order(
                cid, status=Status.UNKNOWN, error=str(exc), updated_at=now, next_check_at=now + 1000
            )
            self._event(
                now, "warn", f"{side.value} {size}: outcome unknown ({exc}); asking the exchange"
            )
            return
        self._apply(cid, update, now)

    def reconcile(self, now: int) -> None:
        """Ask the venue about every unfinished order whose next check is due."""
        for order in self.open_orders():
            if now < order.next_check_at:
                continue
            try:
                update = self.venue.query(order.client_id)
            except BinanceError as exc:
                self._backoff(order, now, str(exc))
                continue
            if update is not None:
                self._apply(order.client_id, update, now)
                continue
            if now - order.created_at < self.cfg.unknown_grace_ms:
                self._backoff(order, now, "not found yet")
                continue
            seen = self.venue.seen_since(order.client_id, order.created_at - 60_000)
            if seen is False:
                self.store.update_order(order.client_id, status=Status.NOT_FOUND, updated_at=now)
                self._event(
                    now,
                    "info",
                    f"order {order.client_id} never reached the exchange; nothing was executed",
                )
            else:
                self._backoff(order, now, "not found, history inconclusive")
        self._flag_stuck(now)

    def _backoff(self, order: OrderRow, now: int, why: str) -> None:
        delay = min(30_000, 1000 * 2 ** min(order.checks, 5))
        self.store.update_order(
            order.client_id,
            checks=order.checks + 1,
            next_check_at=now + delay,
            error=why,
            updated_at=now,
        )

    def _flag_stuck(self, now: int) -> None:
        stuck = [o for o in self.open_orders() if now - o.created_at > self.cfg.attention_ms]
        if stuck and "stuck_order" not in self.state.flags:
            ids = ", ".join(o.client_id for o in stuck)
            self.state.flags["stuck_order"] = (
                f"order {ids} unresolved for over {self.cfg.attention_ms // 60000} min"
            )
            self._event(now, "warn", f"needs attention: {self.state.flags['stuck_order']}")
            self._save()
        elif not stuck and "stuck_order" in self.state.flags:
            del self.state.flags["stuck_order"]
            self._save()

    def _apply(self, cid: str, update: OrderUpdate, now: int) -> None:
        order = self.store.get_order(cid)
        if order is None:
            raise EngineError(f"unknown client order id {cid}")
        try:
            with self.store.tx():
                new = self.store.insert_fills(self.cfg.symbol, update.fills)
                recorded = sum((f.qty for f in self.store.fills(self.cfg.symbol, cid)), Decimal(0))
                if update.executed_qty > recorded:
                    try:
                        new += self.store.insert_fills(
                            self.cfg.symbol, self.venue.order_fills(update)
                        )
                    except BinanceError as exc:
                        log.warning("could not fetch fills for %s: %s", cid, exc)
                    recorded = sum(
                        (f.qty for f in self.store.fills(self.cfg.symbol, cid)), Decimal(0)
                    )
                complete = update.status.terminal and recorded >= update.executed_qty
                self.store.update_order(
                    cid,
                    status=update.status
                    if complete or not update.status.terminal
                    else Status.PARTIALLY_FILLED,
                    order_id=update.order_id,
                    executed_qty=update.executed_qty,
                    cumulative_quote=update.cumulative_quote,
                    updated_at=now,
                    next_check_at=now + 1000,
                    error="" if complete else order.error,
                )
                self.book = self._rebuild_book()
                if new:
                    self._fills_before_midnight(now, new)
                    if order.side is Side.BUY:
                        self._protect(cid)  # known fills get a stop, even before the order finishes
                if complete:
                    self._after_fill(order, update, now)
                self._save()
        except LedgerConflict as exc:
            self.state.flags["ledger"] = f"{exc}; start a new --state-dir"
            self.store.update_order(cid, next_check_at=now + 60_000, updated_at=now)
            self._event(now, "warn", f"needs attention: {self.state.flags['ledger']}")
            self._save()

    def _protect(self, client_id: str) -> None:
        """Entry price and stop from the fills of an entry order recorded so far."""
        fills = self.store.fills(self.cfg.symbol, client_id)
        qty = sum((f.qty for f in fills), Decimal(0))
        if qty > 0:
            avg = sum((f.quote_qty for f in fills), Decimal(0)) / qty
            self.state.entry_price = avg
            self.state.stop_price = risk.stop_for(avg, self.cfg.risk)

    def _after_fill(self, order: OrderRow, update: OrderUpdate, now: int) -> None:
        fills = self.store.fills(self.cfg.symbol, order.client_id)
        qty = sum((f.qty for f in fills), Decimal(0))
        if qty == 0:
            self._event(
                now,
                "info",
                f"{order.side.value} {order.client_id} finished with nothing executed ({update.status.value})",
            )
            return
        avg = sum((f.quote_qty for f in fills), Decimal(0)) / qty
        if order.side is Side.BUY:
            self._protect(order.client_id)
            stop = f", stop {fmt_price(self.state.stop_price)}" if self.state.stop_price else ""
            self._event(now, "trade", f"BUY {fmt_qty(qty)} {self.base} @ {fmt_price(avg)}{stop}")
        else:
            self._event(
                now,
                "trade",
                f"SELL {fmt_qty(qty)} {self.base} @ {fmt_price(avg)} ({order.purpose})",
            )
            self.state.last_exit_bar = self._bar_of(update.time or now)
            if not self.has_position():
                self._clear_position(now)

    def _clear_position(self, now: int) -> None:
        self.state.entry_price = None
        self.state.stop_price = None
        self.state.exit_reason = None
        self._last_exit_problem = None
        self._dirty = True

    def liquidate(self, now: int, why: str) -> None:
        """Sell the bot's inventory now (end of a backtest)."""
        if self.has_position() and not self.open_orders():
            self._decide_exit(now, "end", why)
            self._exit(now)

    # ----- startup checks ---------------------------------------------------------

    def check_balances(self, now: int) -> None:
        """Flag (and stop entries) if the exchange holds less than the ledger says."""
        free = self.venue.free_balance(self.base)
        if free is None:
            return
        if free < self.book.qty and self.rules.lots(self.book.qty - free) > 0:
            self.state.flags["balance"] = (
                f"exchange holds {free} {self.base}, ledger says {self.book.qty}; "
                "coins were moved or sold outside the bot"
            )
            self._event(now, "warn", f"needs attention: {self.state.flags['balance']}")
        elif "balance" in self.state.flags:
            del self.state.flags["balance"]
        self._save()

    # ----- dashboard ----------------------------------------------------------------

    def snapshot(self, now: int) -> Snapshot:
        equity = self.equity()
        start = self.state.day_start_equity
        change = None if not start else (equity - start) / start * 100
        return Snapshot(
            mode=self.venue.mode,
            symbol=self.cfg.symbol,
            base_asset=self.base,
            quote_asset=self.quote_asset,
            interval=self.cfg.interval,
            now=now,
            bid=self.quote.bid,
            ask=self.quote.ask,
            quote_age_ms=max(0, now - self.quote.time),
            rsi=self.strategy.rsi.value,
            ema=self.strategy.ema.value if self.strategy.ema else None,
            params=self.strategy.params.describe(),
            budget=self.cfg.budget,
            cash=self.book.cash,
            qty=self.book.qty,
            equity=equity,
            entry_price=self.state.entry_price,
            stop_price=self.state.stop_price,
            exit_reason=self.state.exit_reason,
            day_start_equity=start,
            day_change_pct=change,
            entries_today=self.state.entries_today,
            max_entries=self.cfg.risk.max_entries_per_day,
            daily_loss_pct=self.cfg.risk.daily_loss_pct,
            latched=self.state.latched,
            latch_reason=self.state.latch_reason,
            flags=dict(self.state.flags),
            open_orders=[
                f"{o.side.value} {o.purpose} {o.status.value}" for o in self.open_orders()
            ],
            unvalued_fees=dict(self.book.unvalued_fees),
            fees_quote=self.book.fees_quote,
            last_bar=self.state.last_bar,
            events=self.store.recent_events(8),
        )


__all__ = ["DAY_MS", "Engine", "EngineConfig", "EngineError", "Snapshot"]
