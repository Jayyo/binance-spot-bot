"""Command line: spotbot fetch | backtest | run | check-key | status."""

from __future__ import annotations

import argparse
import logging
import sys
import time
from decimal import Decimal, InvalidOperation
from pathlib import Path

from . import __version__

log = logging.getLogger("spotbot")


def decimal_arg(text: str) -> Decimal:
    try:
        value = Decimal(text)
    except InvalidOperation:
        raise argparse.ArgumentTypeError(f"not a number: {text}") from None
    if not value.is_finite():
        raise argparse.ArgumentTypeError(f"not a number: {text}")
    return value


def positive_decimal(text: str) -> Decimal:
    value = decimal_arg(text)
    if value <= 0:
        raise argparse.ArgumentTypeError(f"must be positive: {text}")
    return value


def bps_arg(text: str) -> Decimal:
    value = decimal_arg(text)
    if not 0 <= value <= 1000:
        raise argparse.ArgumentTypeError(f"basis points between 0 and 1000 expected: {text}")
    return value


def poll_arg(text: str) -> float:
    try:
        value = float(text)
    except ValueError:
        raise argparse.ArgumentTypeError(f"not a number: {text}") from None
    if not 1 <= value <= 3600:
        raise argparse.ArgumentTypeError("--poll must be between 1 and 3600 seconds")
    return value


def add_strategy_args(p: argparse.ArgumentParser) -> None:
    g = p.add_argument_group("strategy")
    g.add_argument("--rsi-period", type=int, default=14)
    g.add_argument("--oversold", type=float, default=30.0, help="enter below this RSI (default 30)")
    g.add_argument(
        "--overbought", type=float, default=70.0, help="exit above this RSI (default 70)"
    )
    g.add_argument(
        "--trend-ema", type=int, default=0, help="only enter above this EMA; 0 = off (default)"
    )


def add_risk_args(p: argparse.ArgumentParser, *, position_default: str = "100") -> None:
    g = p.add_argument_group("risk")
    g.add_argument(
        "--position-pct",
        type=decimal_arg,
        default=Decimal(position_default),
        help="share of bot equity per entry (default %(default)s)",
    )
    g.add_argument(
        "--max-position",
        type=positive_decimal,
        default=None,
        help="absolute cap per entry in the quote asset (required for --live)",
    )
    g.add_argument(
        "--stop-loss",
        type=decimal_arg,
        default=Decimal(3),
        help="percent below entry; 0 = off (default 3)",
    )
    g.add_argument(
        "--daily-loss",
        type=decimal_arg,
        default=Decimal(2),
        help="pause entries for the UTC day after this percent loss; 0 = off (default 2)",
    )
    g.add_argument("--max-entries", type=int, default=10, help="entries per UTC day (default 10)")
    g.add_argument("--cooldown", type=int, default=1, help="bars to wait after an exit (default 1)")


def strategy_from(args: argparse.Namespace):
    from .strategy import RsiParams

    return RsiParams(args.rsi_period, args.oversold, args.overbought, args.trend_ema)


def risk_from(args: argparse.Namespace):
    from .risk import RiskConfig

    return RiskConfig(
        position_pct=args.position_pct,
        max_position=args.max_position,
        stop_loss_pct=args.stop_loss,
        daily_loss_pct=args.daily_loss,
        max_entries_per_day=args.max_entries,
        cooldown_bars=args.cooldown,
    )


def cmd_fetch(args: argparse.Namespace) -> int:
    from .data import fetch_month, month_range

    for month in month_range(args.start, args.end):
        path = fetch_month(args.symbol, args.interval, month, args.data_dir)
        print(f"ok  {path}")
    return 0


def cmd_backtest(args: argparse.Namespace) -> int:
    from . import backtest, data, report
    from .model import interval_ms

    params = strategy_from(args)
    # Enough whole months before --start to warm the indicators up (at least one).
    bars_per_month = 28 * 86_400_000 // interval_ms(args.interval)
    warm_months = max(1, -(-3 * params.warmup_bars // max(1, bars_per_month)))
    first = data.previous_month(args.start, warm_months)
    ds = data.load(
        args.symbol, args.interval, first, args.end, args.data_dir, download=not args.offline
    )
    if args.manifest:
        data.verify_manifest([ds], args.manifest)
    for line in ds.gap_lines():
        print(f"note: missing bars {line}", file=sys.stderr)
    cfg = backtest.BacktestConfig(
        symbol=args.symbol,
        interval=args.interval,
        start=args.start,
        end=args.end,
        params=params,
        risk=risk_from(args),
        budget=args.budget,
        fee_bps=args.fee_bps,
        slippage_bps=args.slippage_bps,
    )
    res = backtest.run(cfg, ds)
    name = f"{args.symbol}-{args.interval}-{args.start}-{args.end}"
    if args.trend_ema:
        name += f"-ema{args.trend_ema}"
    path = report.write(res, args.out or Path("reports") / name)
    s, h = res.summary, res.hold
    print(f"{args.symbol} {args.interval} {args.start}..{args.end}  {params.describe()}")
    print(
        f"  strategy     return {s.total_return:+.1%}  max drawdown {-s.max_drawdown:.1%}  trades {s.trades}  "
        f"bars in market {s.exposure:.0%}  fees {s.fees:,.2f}"
    )
    print(f"  buy & hold   return {h.total_return:+.1%}  max drawdown {-h.max_drawdown:.1%}")
    print(f"  report       {path}")
    return 0


def setup_logging(log_file: Path | None, console: bool) -> None:
    root = logging.getLogger()
    root.setLevel(logging.INFO)
    fmt = logging.Formatter("%(asctime)sZ %(levelname)s %(name)s: %(message)s")
    fmt.converter = time.gmtime  # UTC, like bar times and the daily limit
    if log_file:
        log_file.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        if sys.platform != "win32":
            log_file.parent.chmod(0o700)
        fh = logging.FileHandler(log_file)
        fh.setFormatter(fmt)
        root.addHandler(fh)
    if console:
        sh = logging.StreamHandler(sys.stdout)
        short = logging.Formatter("%(asctime)sZ %(message)s", "%H:%M:%S")
        short.converter = time.gmtime
        sh.setFormatter(short)
        root.addHandler(sh)


def cmd_run(args: argparse.Namespace) -> int:
    from . import runner
    from .filters import SymbolRules

    mode = "live" if args.live else "testnet" if args.testnet else "paper"
    if mode == "live" and (args.budget is None or args.max_position is None):
        print(
            "--live needs an explicit --budget and --max-position (amounts you are prepared to lose).",
            file=sys.stderr,
        )
        return 2
    budget = args.budget if args.budget is not None else Decimal(1000)
    cfg = runner.RunConfig(
        mode=mode,
        symbol=args.symbol,
        interval=args.interval,
        budget=budget,
        params=strategy_from(args),
        risk=risk_from(args),
        state_dir=args.state_dir,
        poll_seconds=args.poll,
        paper_fee_bps=args.paper_fee_bps,
    )
    setup_logging(cfg.state_dir / f"{mode}-{cfg.symbol}.log", console=args.no_dashboard)
    lock = runner.StateLock(cfg.state_dir / f"{mode}.lock")
    lock.acquire()
    try:
        public, signed, creds = runner.build_clients(mode)
        if signed is not None:
            rules = SymbolRules.from_exchange_info(public.exchange_info(cfg.symbol))
            checks, account = runner.preflight(cfg, signed, rules)
            print(f"{mode} key {creds.fingerprint} ({creds.signer.kind}, from {creds.source})")
            for c in checks:
                print(f"  [{c.level:>4}] {c.text}")
            if any(c.level == "fail" for c in checks):
                print("Refusing to start; fix the items marked fail.", file=sys.stderr)
                return 2
            if mode == "live" and not runner.confirm_live(cfg, account):
                print("Not confirmed. Nothing was sent.", file=sys.stderr)
                return 2
        bot = runner.Runner(cfg, public=public, signed=signed)
        bot.setup()
        try:
            if args.no_dashboard:
                bot.run()
            else:
                from rich.live import Live

                from .dashboard import render

                with Live(
                    render(bot.engine.snapshot(bot.clock()), stop_file=str(cfg.kill_switch)),
                    refresh_per_second=2,
                    transient=False,
                ) as live:
                    bot.out = lambda engine, now: live.update(
                        render(engine.snapshot(now), stop_file=str(cfg.kill_switch))
                    )
                    bot.run()
        finally:
            bot.close()
        print(
            "Stopped. An open position stays open; its stop is not watched until the bot runs again."
        )
        return 0
    finally:
        lock.release()


def cmd_check_key(args: argparse.Namespace) -> int:
    from . import runner
    from .filters import SymbolRules

    mode = "live" if args.live else "testnet"
    setup_logging(None, console=True)
    public, signed, creds = runner.build_clients(mode)
    assert signed is not None and creds is not None
    rules = SymbolRules.from_exchange_info(public.exchange_info(args.symbol))
    cfg = runner.RunConfig(mode=mode, symbol=args.symbol, interval="1h", budget=Decimal(1000))
    checks, _ = runner.preflight(cfg, signed, rules)
    print(
        f"{mode} key {creds.fingerprint} ({creds.signer.kind}, from {creds.source}), clock offset {signed.offset_ms} ms"
    )
    for c in checks:
        print(f"  [{c.level:>4}] {c.text}")
    return 2 if any(c.level == "fail" for c in checks) else 0


def cmd_status(args: argparse.Namespace) -> int:
    from .ledger import Store, book_from_fills
    from .model import fmt_time

    mode = "live" if args.live else "testnet" if args.testnet else "paper"
    path = args.state_dir / f"{mode}-{args.symbol}.db"
    if not path.exists():
        print(f"no state at {path}")
        return 1
    store = Store(path)
    budget = Decimal(store.get_meta("budget") or "0")
    base, quote = store.get_meta("base_asset") or "", store.get_meta("quote_asset") or ""
    fills = store.fills(args.symbol)
    state = store.load_state(args.symbol)
    book = book_from_fills(budget, fills, base, quote)
    print(
        f"{mode} {args.symbol}: budget {budget}, {len(fills)} fills, {len(store.orders(args.symbol))} orders"
    )
    print(
        f"  tracked {book.qty} {base}, cash {book.cash} {quote}, fees {book.fees_quote:.4f} {quote}"
    )
    if state:
        print(
            f"  entry {state.entry_price}  stop {state.stop_price}  day {state.day}  latched {state.latched}"
        )
        for k, v in state.flags.items():
            print(f"  needs attention ({k}): {v}")
    for o in store.open_orders(args.symbol):
        print(
            f"  unresolved order {o.client_id}: {o.side.value} {o.purpose} {o.status.value} {o.error}"
        )
    for t, _level, message in store.recent_events(10):
        print(f"  {fmt_time(t)}  {message}")
    return 0


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="spotbot", description="Binance spot RSI bot: backtest, paper, testnet, live."
    )
    p.add_argument("--version", action="version", version=f"spotbot {__version__}")
    sub = p.add_subparsers(dest="command", required=True)

    f = sub.add_parser("fetch", help="download monthly klines from data.binance.vision")
    f.add_argument("symbol", type=str.upper)
    f.add_argument("--interval", default="1h")
    f.add_argument("--start", required=True, help="first month, YYYY-MM")
    f.add_argument("--end", required=True, help="last month, YYYY-MM")
    f.add_argument("--data-dir", type=Path, default=Path("data"))
    f.set_defaults(func=cmd_fetch)

    b = sub.add_parser("backtest", help="replay public klines and write a report")
    b.add_argument("symbol", type=str.upper)
    b.add_argument("--interval", default="1h")
    b.add_argument("--start", required=True, help="first traded month, YYYY-MM")
    b.add_argument("--end", required=True, help="last traded month, YYYY-MM")
    b.add_argument("--budget", type=positive_decimal, default=Decimal(10_000))
    b.add_argument(
        "--fee-bps", type=bps_arg, default=Decimal(10), help="per fill, 10 = 0.10%% (default)"
    )
    b.add_argument("--slippage-bps", type=bps_arg, default=Decimal(5), help="per fill (default 5)")
    b.add_argument("--data-dir", type=Path, default=Path("data"))
    b.add_argument("--offline", action="store_true", help="use cached archives only")
    b.add_argument(
        "--manifest", type=Path, help="fail unless the archives match these SHA-256 values"
    )
    b.add_argument("--out", type=Path, help="report folder (default reports/<run>)")
    add_strategy_args(b)
    add_risk_args(b)
    b.set_defaults(func=cmd_backtest)

    r = sub.add_parser("run", help="trade on live prices: paper (default), --testnet or --live")
    r.add_argument("symbol", type=str.upper)
    r.add_argument("--interval", default="1h")
    target = r.add_mutually_exclusive_group()
    target.add_argument(
        "--testnet", action="store_true", help="place orders on Binance Spot Testnet"
    )
    target.add_argument(
        "--live", action="store_true", help="place REAL orders (asks for confirmation)"
    )
    r.add_argument(
        "--budget",
        type=positive_decimal,
        default=None,
        help="bot equity in the quote asset (paper/testnet default 1000)",
    )
    r.add_argument("--state-dir", type=Path, default=Path("state"))
    r.add_argument(
        "--poll", type=poll_arg, default=5.0, help="seconds between price checks (default 5)"
    )
    r.add_argument("--paper-fee-bps", type=bps_arg, default=Decimal(10))
    r.add_argument(
        "--no-dashboard", action="store_true", help="plain log lines instead of the terminal view"
    )
    add_strategy_args(r)
    add_risk_args(r)
    r.set_defaults(func=cmd_run)

    k = sub.add_parser("check-key", help="check API key permissions and account without trading")
    which = k.add_mutually_exclusive_group(required=True)
    which.add_argument("--testnet", action="store_true")
    which.add_argument("--live", action="store_true")
    k.add_argument("--symbol", type=str.upper, default="BTCUSDT")
    k.set_defaults(func=cmd_check_key)

    s = sub.add_parser("status", help="print what the ledger knows, without network access")
    s.add_argument("symbol", type=str.upper)
    which = s.add_mutually_exclusive_group()
    which.add_argument("--testnet", action="store_true")
    which.add_argument("--live", action="store_true")
    s.add_argument("--state-dir", type=Path, default=Path("state"))
    s.set_defaults(func=cmd_status)
    return p


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    from .binance import BinanceError
    from .data import DataError
    from .engine import EngineError
    from .keys import CredentialError
    from .runner import RunError

    try:
        return args.func(args)
    except (BinanceError, DataError, EngineError, CredentialError, RunError, ValueError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        return 130


if __name__ == "__main__":
    sys.exit(main())
