"""Terminal view of a running bot (rich)."""

from __future__ import annotations

from decimal import Decimal

from rich import box
from rich.console import Group, RenderableType
from rich.panel import Panel
from rich.table import Table
from rich.text import Text

from .engine import Snapshot
from .model import fmt_price, fmt_qty, fmt_time, interval_ms

MODE_STYLE = {
    "paper": "bold black on bright_cyan",
    "testnet": "bold black on yellow",
    "live": "bold white on red",
}


def money(x: Decimal | None, digits: int = 2) -> str:
    return "-" if x is None else f"{x:,.{digits}f}"


def signed_pct(x: Decimal | float | None) -> Text:
    if x is None:
        return Text("-")
    value = float(x)
    return Text(f"{value:+.2f}%", style="green" if value > 0 else "red" if value < 0 else "")


def kv(rows: list[tuple[str, RenderableType | str]]) -> Table:
    t = Table.grid(padding=(0, 2))
    t.add_column(style="dim", no_wrap=True)
    t.add_column(no_wrap=True)
    for k, v in rows:
        t.add_row(k, v)
    return t


def render(s: Snapshot, *, stop_file: str = "state/STOP") -> RenderableType:
    step = interval_ms(s.interval)
    next_close = (s.now - s.now % step) + step - s.now
    header = Text()
    header.append(" spotbot ", style="bold")
    header.append(f" {s.mode.upper()} ", style=MODE_STYLE.get(s.mode, "bold"))
    header.append(f"  {s.symbol} {s.interval}   {s.params}", style="bold")
    header.append(f"   {fmt_time(s.now)} UTC", style="dim")

    rsi = "-" if s.rsi is None else f"{s.rsi:.1f}"
    market_rows: list[tuple[str, RenderableType | str]] = [
        ("bid / ask", f"{fmt_price(s.bid)} / {fmt_price(s.ask)}"),
        ("RSI", rsi),
    ]
    if s.ema is not None:
        market_rows.append(("EMA", fmt_price(s.ema)))
    market_rows += [
        ("data age", f"{s.quote_age_ms / 1000:.1f} s"),
        ("last bar", f"{fmt_time(s.last_bar)} UTC" if s.last_bar else "-"),
        ("next close", f"in {next_close // 1000} s"),
    ]

    quote = s.quote_asset
    if s.entry_price is not None and s.qty > 0:
        value = s.qty * s.bid
        unreal = (s.bid / s.entry_price - 1) * 100 if s.entry_price else None
        stop = (
            f"{fmt_price(s.stop_price)} ({(s.stop_price / s.entry_price - 1) * 100:+.1f}%)"
            if s.stop_price
            else "off"
        )
        pos_rows: list[tuple[str, RenderableType | str]] = [
            ("size", f"{fmt_qty(s.qty)} {s.base_asset}"),
            ("value", f"{money(value)} {quote}"),
            ("entry", fmt_price(s.entry_price)),
            ("stop", stop),
            ("open P&L", signed_pct(unreal)),
        ]
    else:
        dust = f" (dust {fmt_qty(s.qty)})" if s.qty > 0 else ""
        pos_rows = [("size", f"flat{dust}")]
    pos_rows += [("cash", f"{money(s.cash)} {quote}"), ("equity", f"{money(s.equity)} {quote}")]

    if s.exit_reason and s.qty > 0:
        status = Text(f"selling ({s.exit_reason})", style="bold yellow")
    elif s.flags:
        status = Text("paused: " + "; ".join(s.flags.values()), style="bold red")
    elif s.latched:
        status = Text(f"paused today: {s.latch_reason}", style="bold yellow")
    elif s.open_orders:
        status = Text("waiting on " + ", ".join(s.open_orders), style="yellow")
    else:
        status = Text("entries allowed", style="green")
    risk_rows: list[tuple[str, RenderableType | str]] = [
        ("status", status),
        (
            "today",
            Text.assemble(signed_pct(s.day_change_pct), (f"  limit -{s.daily_loss_pct}%", "dim")),
        ),
        ("entries", f"{s.entries_today} / {s.max_entries} today"),
        ("budget", f"{money(s.budget)} {quote}"),
        ("fees paid", f"{money(s.fees_quote)} {quote}"),
    ]
    if s.unvalued_fees:
        risk_rows.append(
            ("unvalued fees", ", ".join(f"{v} {k}" for k, v in s.unvalued_fees.items()))
        )

    top = Table.grid(expand=True, padding=(0, 1))
    for _ in range(3):
        top.add_column(ratio=1)
    top.add_row(
        Panel(kv(market_rows), title="Market", title_align="left", box=box.SQUARE),
        Panel(kv(pos_rows), title="Position", title_align="left", box=box.SQUARE),
        Panel(kv(risk_rows), title="Risk", title_align="left", box=box.SQUARE),
    )

    events = Table.grid(padding=(0, 2))
    events.add_column(style="dim", no_wrap=True)
    events.add_column()
    for t, level, message in s.events[-8:]:
        style = "bold" if level == "trade" else "yellow" if level == "warn" else ""
        events.add_row(fmt_time(t)[11:], Text(message, style=style))
    if not s.events:
        events.add_row("", Text("no events yet", style="dim"))

    footer = Text(
        f" Ctrl-C stops the bot; an open position stays open and its stop is not watched while stopped. "
        f"Create {stop_file} to pause entries.",
        style="dim",
    )
    return Group(
        header, top, Panel(events, title="Events", title_align="left", box=box.SQUARE), footer
    )
