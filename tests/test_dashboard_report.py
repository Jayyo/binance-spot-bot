from decimal import Decimal as D

from rich.console import Console

from spotbot import backtest, report
from spotbot.dashboard import render
from tests.fakes import T0, H, bar, make_engine, oversold_history, set_price
from tests.test_backtest import config, dataset, walk


def text_of(renderable, width=120):
    console = Console(record=True, width=width, force_terminal=False)
    console.print(renderable)
    return console.export_text()


def test_dashboard_renders_flat_and_long_states():
    engine, venue, store, quote = make_engine()
    engine.warmup(oversold_history())
    set_price(quote, 170, T0)
    engine.on_tick(T0)
    flat = text_of(render(engine.snapshot(T0)))
    assert "PAPER" not in flat and "TESTNET" in flat  # the scripted venue reports mode testnet
    assert "flat" in flat and "entries allowed" in flat

    set_price(quote, 170, T0 + H)
    engine.on_bar_close(bar(0, 170), T0 + H)
    long = text_of(render(engine.snapshot(T0 + H)))
    assert "BTC" in long and "stop" in long and "BUY" in long
    assert "-3.0%" in long


def test_report_files(tmp_path):
    res = backtest.run(
        config(fee_bps=D(10), slippage_bps=D(5)), dataset(walk(24 * 31 + 24 * 90, 5))
    )
    path = report.write(res, tmp_path / "r")
    html = path.read_text()
    assert "Exact conditions" in html and "Buy &amp; hold" in html
    for name in ("equity.png", "trades.csv", "equity.csv", "summary.json"):
        assert (tmp_path / "r" / name).stat().st_size > 0
    start, end = report.find_window(res)
    report.plot_zoom(res, start, end, tmp_path / "zoom.png")
    assert (tmp_path / "zoom.png").stat().st_size > 0
