"""Backtest output: a self-contained HTML report, a chart, CSVs and a JSON summary."""

from __future__ import annotations

import base64
import csv
import html
import json
from dataclasses import asdict
from datetime import UTC, datetime
from pathlib import Path

from . import __version__
from .backtest import BacktestResult
from .metrics import Summary, drawdown_series, yearly_returns
from .model import fmt_time

BLUE = "#0046FF"
INK = "#0F172A"
SLATE = "#334155"
MUTED = "#64748B"
GRAY = "#94A3B8"
LINE = "#E2E8F0"


def pct(x: float | None, digits: int = 1) -> str:
    return "n/a" if x is None else f"{x * 100:+.{digits}f}%"


def num(x: float | None, digits: int = 2) -> str:
    return "n/a" if x is None else f"{x:,.{digits}f}"


def title(res: BacktestResult) -> str:
    c = res.cfg
    return f"{c.symbol} {c.interval} · {c.start} to {c.end}"


def conditions(res: BacktestResult) -> list[tuple[str, str]]:
    c = res.cfg
    r = c.risk
    cap = f", capped at {r.max_position} {res.quote}" if r.max_position else ""
    return [
        (
            "Data",
            f"Binance spot monthly klines, {len(res.data.files)} archives, "
            + (
                "each SHA-256 matched Binance's checksum file"
                if res.data.verified
                else "checksums not verified"
            ),
        ),
        (
            "Period",
            f"{fmt_time(res.bars[0].open_time)} to {fmt_time(res.bars[-1].close_time)} UTC, {len(res.bars):,} bars",
        ),
        (
            "Warm-up",
            f"{res.warmup_bars:,} earlier bars feed the indicators; no trading, not in the metrics",
        ),
        ("Missing bars", "; ".join(res.data.gap_lines()) or "none"),
        ("Strategy", res.cfg.params.describe()),
        ("Sizing", f"{r.position_pct}% of bot equity per entry{cap}; long only, no leverage"),
        (
            "Stop",
            f"{r.stop_loss_pct}% below the entry fill, checked against each bar's low; fills at min(open, stop)"
            if r.stop_loss_pct
            else "none",
        ),
        (
            "Daily loss",
            f"entries pause for the UTC day once equity is {r.daily_loss_pct}% below its 00:00 value"
            if r.daily_loss_pct
            else "none",
        ),
        (
            "Entries",
            f"at most {r.max_entries_per_day} per UTC day, {r.cooldown_bars} bar cooldown after an exit",
        ),
        ("Execution", "decision at a bar's close, market fill at the next bar's open"),
        (
            "Costs",
            f"fee {c.fee_bps / 100:.2f}% and slippage {c.slippage_bps / 100:.2f}% on every fill, "
            "same for buy & hold",
        ),
        ("Start / end", f"{c.budget:,} {res.quote}; any open position is sold at the last close"),
        ("Not modelled", "exchange order filters, order book depth, fees paid in BNB"),
    ]


def metric_rows(s: Summary, h: Summary) -> list[tuple[str, str, str]]:
    return [
        ("Total return", pct(s.total_return), pct(h.total_return)),
        ("CAGR", pct(s.cagr), pct(h.cagr)),
        ("Max drawdown (bar closes)", pct(-s.max_drawdown), pct(-h.max_drawdown)),
        ("Sharpe (daily, rf=0)", num(s.sharpe), num(h.sharpe)),
        ("Final equity", num(s.end_value), num(h.end_value)),
        ("Bars in market", pct(s.exposure, 0).lstrip("+"), "100%"),
        ("Trades", str(s.trades), "1"),
        ("Win rate", pct(s.win_rate, 0).lstrip("+"), ""),
        ("Average win / loss", f"{pct(s.avg_win, 2)} / {pct(s.avg_loss, 2)}", ""),
        ("Profit factor", num(s.profit_factor), ""),
        ("Average holding time (fill to fill)", f"{num(s.avg_hours, 1)} h", ""),
        ("Fees paid", num(s.fees), ""),
    ]


def plot(
    res: BacktestResult, path: Path, *, width: float = 12.0, height: float = 7.2, dpi: int = 110
) -> None:
    import matplotlib

    matplotlib.use("Agg")
    import matplotlib.dates as mdates
    import matplotlib.pyplot as plt
    from matplotlib.ticker import FuncFormatter

    def dt(ms: int) -> datetime:
        return datetime.fromtimestamp(ms / 1000, UTC)

    plt.rcParams.update(
        {
            "font.size": 10,
            "axes.edgecolor": LINE,
            "axes.labelcolor": SLATE,
            "xtick.color": MUTED,
            "ytick.color": MUTED,
            "axes.titlecolor": INK,
            "axes.titlesize": 11,
            "axes.titleweight": "bold",
            "axes.titlelocation": "left",
        }
    )
    fig, (ax_p, ax_e, ax_d) = plt.subplots(
        3,
        1,
        figsize=(width, height),
        dpi=dpi,
        sharex=True,
        gridspec_kw={"height_ratios": [2.2, 2.2, 1.2]},
    )
    ax_p.plot(
        [dt(b.close_time) for b in res.bars], [b.close for b in res.bars], color=SLATE, lw=0.8
    )
    if len(res.trades) <= 60:  # beyond that the markers just paint over the line
        marks = (
            ("^", BLUE, "buy", [(t.entry_time, t.entry_price) for t in res.trades]),
            (
                "v",
                INK,
                "RSI exit",
                [(t.exit_time, t.exit_price) for t in res.trades if t.exit_reason != "stop"],
            ),
            (
                "x",
                GRAY,
                "stop",
                [(t.exit_time, t.exit_price) for t in res.trades if t.exit_reason == "stop"],
            ),
        )
        for marker, color, label, points in marks:
            if points:
                xs = [dt(t) for t, _ in points]
                ax_p.scatter(
                    xs,
                    [p for _, p in points],
                    marker=marker,
                    s=18,
                    color=color,
                    zorder=3,
                    label=label,
                )
        if res.trades:
            ax_p.legend(loc="upper left", frameon=False, ncols=3)
        ax_p.set_title(f"{res.cfg.symbol} close ({res.quote}) and trades")
    else:
        ax_p.set_title(
            f"{res.cfg.symbol} close ({res.quote}); {len(res.trades)} trades, listed in the report"
        )

    eq_t = [dt(t) for t, _ in res.points]
    bh_t = [dt(t) for t, _ in res.hold_points]
    ax_e.plot(bh_t, [v for _, v in res.hold_points], color=GRAY, lw=1.4, label="buy & hold")
    ax_e.plot(eq_t, [v for _, v in res.points], color=BLUE, lw=1.8, label=res.cfg.params.describe())
    ax_e.set_title(f"Equity ({res.quote}), net of fees and slippage")
    ax_e.legend(loc="upper left", frameon=False)

    dd = [d * 100 for d in drawdown_series([v for _, v in res.points])]
    ax_d.fill_between(eq_t, dd, 0, color=BLUE, alpha=0.18, lw=0)
    ax_d.plot(eq_t, dd, color=BLUE, lw=1.0)
    ax_d.plot(
        bh_t,
        [d * 100 for d in drawdown_series([v for _, v in res.hold_points])],
        color=GRAY,
        lw=1.0,
    )
    ax_d.set_title("Drawdown (%)")

    thousands = FuncFormatter(lambda v, _: f"{v:,.0f}")
    for ax in (ax_p, ax_e, ax_d):
        ax.grid(True, color=LINE, lw=0.8)
        for side in ("top", "right"):
            ax.spines[side].set_visible(False)
    ax_p.yaxis.set_major_formatter(thousands)
    ax_e.yaxis.set_major_formatter(thousands)
    ax_d.xaxis.set_major_formatter(mdates.DateFormatter("%Y-%m"))
    fig.suptitle(title(res), x=0.01, ha="left", fontsize=13, fontweight="bold", color=INK)
    fig.tight_layout()
    fig.savefig(path, facecolor="white")
    plt.close(fig)


def find_window(res: BacktestResult, days: int = 6) -> tuple[int, int]:
    """First window of `days` UTC days holding a stop-loss and an RSI exit (for illustrations)."""
    span = days * 86_400_000
    for t in res.trades:
        start = t.entry_time - t.entry_time % 86_400_000
        inside = [x for x in res.trades if start <= x.entry_time and x.exit_time < start + span]
        reasons = {x.exit_reason for x in inside}
        if len(inside) >= 2 and {"stop", "exit"} <= reasons:
            return start, start + span
    first = res.trades[0].entry_time if res.trades else res.bars[0].open_time
    start = first - first % 86_400_000
    return start, start + span


def plot_zoom(res: BacktestResult, start: int, end: int, path: Path) -> None:
    """Candles, trades and RSI for a short window: what the rules actually do."""
    import matplotlib

    matplotlib.use("Agg")
    import matplotlib.dates as mdates
    import matplotlib.pyplot as plt
    from matplotlib.patches import Rectangle
    from matplotlib.ticker import FuncFormatter

    from .indicators import Rsi

    rsi = Rsi(res.cfg.params.rsi_period)
    rsi_at = {b.open_time: rsi.update(b.close) for b in res.data.bars}
    bars = [b for b in res.bars if start <= b.open_time < end]
    step_days = (bars[1].open_time - bars[0].open_time) / 86_400_000 if len(bars) > 1 else 1 / 24

    def dt(ms: int) -> datetime:
        return datetime.fromtimestamp(ms / 1000, UTC)

    fig, (ax, ax_r) = plt.subplots(
        2, 1, figsize=(11, 5.6), dpi=100, sharex=True, gridspec_kw={"height_ratios": [3, 1.4]}
    )
    width = 0.7 * step_days
    for b in bars:
        x = mdates.date2num(dt(b.open_time)) + step_days / 2
        up = b.close >= b.open
        color = SLATE if up else GRAY
        ax.vlines(x, b.low, b.high, color=color, lw=0.8)
        body = Rectangle(
            (x - width / 2, min(b.open, b.close)),
            width,
            abs(b.close - b.open) or b.close * 1e-5,
            facecolor="white" if up else color,
            edgecolor=color,
            lw=0.8,
        )
        ax.add_patch(body)

    stop_pct = float(res.cfg.risk.stop_loss_pct) / 100
    for t in res.trades:
        if not start <= t.entry_time < end:
            continue
        stopped = t.exit_reason == "stop"
        ax.scatter(dt(t.entry_time), t.entry_price, marker="^", s=70, color=BLUE, zorder=4)
        ax.scatter(
            dt(t.exit_time), t.exit_price, marker="x" if stopped else "v", s=70, color=INK, zorder=4
        )
        if stop_pct:
            level = t.entry_price * (1 - stop_pct)
            ax.hlines(level, dt(t.entry_time), dt(t.exit_time), colors=BLUE, linestyles="--", lw=1)
        ax.annotate(
            f"{'stop' if stopped else 'RSI exit'} {t.ret * 100:+.1f}%",
            (dt(t.exit_time), t.exit_price),
            xytext=(6, 8),
            textcoords="offset points",
            fontsize=9,
            color=INK,
        )
    ax.set_title(
        f"{res.cfg.symbol} {res.cfg.interval}, {fmt_time(start)[:10]} to {fmt_time(end - 1)[:10]} UTC"
        "    ▲ buy   ▼ RSI exit   × stop   -- stop level",
        loc="left",
        fontsize=11,
        fontweight="bold",
        color=INK,
    )

    p = res.cfg.params
    times = [dt(b.close_time) for b in bars]
    ax_r.plot(times, [rsi_at.get(b.open_time) for b in bars], color=BLUE, lw=1.4)
    ax_r.axhline(p.oversold, color=INK, lw=1, ls="--")
    ax_r.axhline(p.overbought, color=INK, lw=1, ls="--")
    ax_r.fill_between(times, 0, p.oversold, color=BLUE, alpha=0.08)
    ax_r.set_ylim(0, 100)
    ax_r.set_title(
        f"RSI({p.rsi_period}): buy below {p.oversold:g}, sell above {p.overbought:g}",
        loc="left",
        fontsize=10,
        fontweight="bold",
        color=INK,
    )
    for a in (ax, ax_r):
        a.grid(True, color=LINE, lw=0.8)
        for side in ("top", "right"):
            a.spines[side].set_visible(False)
        a.tick_params(colors=MUTED)
    ax.yaxis.set_major_formatter(FuncFormatter(lambda v, _: f"{v:,.0f}"))
    ax_r.xaxis.set_major_formatter(mdates.DateFormatter("%b %d"))
    fig.tight_layout()
    fig.savefig(path, facecolor="white")
    plt.close(fig)


def write(res: BacktestResult, out_dir: Path) -> Path:
    out_dir.mkdir(parents=True, exist_ok=True)
    chart = out_dir / "equity.png"
    plot(res, chart)

    with (out_dir / "trades.csv").open("w", newline="") as fh:
        w = csv.writer(fh)
        w.writerow(
            [
                "entry_utc",
                "exit_utc",
                "entry_price",
                "exit_price",
                "qty",
                "cost",
                "proceeds",
                "pnl",
                "return_pct",
                "exit_reason",
            ]
        )
        for t in res.trades:
            w.writerow(
                [
                    fmt_time(t.entry_time),
                    fmt_time(t.exit_time),
                    f"{t.entry_price:.8g}",
                    f"{t.exit_price:.8g}",
                    f"{t.qty:.8g}",
                    f"{t.cost:.2f}",
                    f"{t.proceeds:.2f}",
                    f"{t.pnl:.2f}",
                    f"{t.ret * 100:.3f}",
                    t.exit_reason,
                ]
            )
    with (out_dir / "equity.csv").open("w", newline="") as fh:
        w = csv.writer(fh)
        w.writerow(["time_utc", "strategy", "buy_and_hold"])
        for (t, v), (_, h) in zip(res.points, res.hold_points, strict=True):
            w.writerow([fmt_time(t), f"{v:.4f}", f"{h:.4f}"])

    summary = {
        "symbol": res.cfg.symbol,
        "interval": res.cfg.interval,
        "start": res.cfg.start,
        "end": res.cfg.end,
        "strategy": res.cfg.params.describe(),
        "fee_bps": str(res.cfg.fee_bps),
        "slippage_bps": str(res.cfg.slippage_bps),
        "budget": str(res.cfg.budget),
        "risk": {k: str(v) for k, v in asdict(res.cfg.risk).items()},
        "result": asdict(res.summary),
        "buy_and_hold": asdict(res.hold),
        "yearly": {
            str(y): {"strategy": v, "buy_and_hold": yearly_returns(res.hold_points).get(y)}
            for y, v in yearly_returns(res.points).items()
        },
        "missing_bars": res.data.gap_lines(),
        "files": {f.name: f.sha256 for f in res.data.files},
        "spotbot": __version__,
    }
    (out_dir / "summary.json").write_text(json.dumps(summary, indent=1) + "\n")

    report = out_dir / "report.html"
    report.write_text(_html(res, chart.read_bytes()))
    return report


def _html(res: BacktestResult, png: bytes) -> str:
    e = html.escape
    s, h = res.summary, res.hold
    verdict = (
        f"The strategy returned {pct(s.total_return)} with a {pct(-s.max_drawdown)} worst drawdown; "
        f"buying and holding returned {pct(h.total_return)} with {pct(-h.max_drawdown)}."
    )
    cond = "".join(f"<tr><th>{e(k)}</th><td>{e(v)}</td></tr>" for k, v in conditions(res))
    metrics = "".join(
        f"<tr><th>{e(a)}</th><td>{e(b)}</td><td>{e(c)}</td></tr>" for a, b, c in metric_rows(s, h)
    )
    hold_years = yearly_returns(res.hold_points)
    years = "".join(
        f"<tr><th>{y}</th><td>{pct(v)}</td><td>{pct(hold_years.get(y))}</td></tr>"
        for y, v in yearly_returns(res.points).items()
    )
    trades = "".join(
        f"<tr><td>{fmt_time(t.entry_time)}</td><td>{fmt_time(t.exit_time)}</td><td>{t.entry_price:,.4g}</td>"
        f"<td>{t.exit_price:,.4g}</td><td>{t.ret * 100:+.2f}%</td><td>{t.pnl:+,.2f}</td><td>{e(t.exit_reason)}</td></tr>"
        for t in res.trades
    )
    files = "".join(
        f"<tr><td>{e(f.name)}</td><td><code>{f.sha256}</code></td></tr>" for f in res.data.files
    )
    image = base64.b64encode(png).decode()
    generated = datetime.now(UTC).strftime("%Y-%m-%d %H:%M UTC")
    return f"""<!doctype html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<title>Backtest {e(title(res))}</title>
<style>
:root {{ --ink:{INK}; --slate:{SLATE}; --muted:{MUTED}; --line:{LINE}; --blue:{BLUE}; }}
body {{ margin:0; font:15px/1.5 system-ui,-apple-system,"Segoe UI",sans-serif; color:var(--ink); background:#fff; }}
main {{ max-width:1100px; margin:0 auto; padding:28px 16px 60px; }}
h1 {{ font-size:24px; margin:0 0 6px; }} h2 {{ font-size:17px; margin:32px 0 8px; border-bottom:2px solid var(--ink); padding-bottom:4px; }}
p.lead {{ font-size:17px; color:var(--slate); margin:0 0 18px; }}
table {{ border-collapse:collapse; width:100%; font-size:14px; }}
th, td {{ text-align:left; padding:6px 10px; border-bottom:1px solid var(--line); vertical-align:top; }}
thead th {{ background:#F8FAFC; border-bottom:2px solid var(--line); }}
tbody th {{ font-weight:600; color:var(--slate); width:28%; }}
td:nth-child(n+2) {{ font-variant-numeric:tabular-nums; }}
img {{ width:100%; height:auto; border:1px solid var(--line); }}
code {{ font-size:12px; color:var(--muted); }} .note {{ color:var(--muted); font-size:13px; }}
.scroll {{ max-height:520px; overflow:auto; border:1px solid var(--line); }}
</style></head><body><main>
<h1>Backtest: {e(title(res))}</h1>
<p class="lead">{e(verdict)}</p>
<img src="data:image/png;base64,{image}" alt="price with trades, equity against buy and hold, drawdown">
<h2>Results</h2>
<table><thead><tr><th></th><th>Strategy</th><th>Buy &amp; hold</th></tr></thead><tbody>{metrics}</tbody></table>
<h2>By calendar year</h2>
<table><thead><tr><th></th><th>Strategy</th><th>Buy &amp; hold</th></tr></thead><tbody>{years}</tbody></table>
<h2>Exact conditions</h2>
<table><tbody>{cond}</tbody></table>
<h2>Trades ({len(res.trades)})</h2>
<div class="scroll"><table><thead><tr><th>Entry (UTC)</th><th>Exit (UTC)</th><th>Entry</th><th>Exit</th><th>Return</th><th>P&amp;L</th><th>Exit reason</th></tr></thead><tbody>{trades}</tbody></table></div>
<h2>Input files</h2>
<div class="scroll"><table><tbody>{files}</tbody></table></div>
<p class="note">Generated {generated} by spotbot {__version__}. Past results on one symbol and period say little about any other.</p>
</main></body></html>
"""
