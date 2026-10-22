import io
from decimal import Decimal as D

import pytest

from spotbot import cli, runner
from spotbot.filters import SymbolRules
from spotbot.runner import RunConfig, RunError, StateLock

CLEAN_KEY = {
    "ipRestrict": True,
    "enableReading": True,
    "enableSpotAndMarginTrading": True,
    "enableWithdrawals": False,
    "enableInternalTransfer": False,
    "permitsUniversalTransfer": False,
    "enableMargin": False,
    "enableFutures": False,
    "enableVanillaOptions": False,
    "enablePortfolioMarginTrading": False,
}
RULES = SymbolRules(symbol="BTCUSDT", base_asset="BTC", quote_asset="USDT", min_notional=D(5))


def levels(checks):
    return [c.level for c in checks]


def test_clean_trade_only_key_passes():
    assert "fail" not in levels(runner.key_checks(CLEAN_KEY))


@pytest.mark.parametrize(
    "change",
    [
        {"enableWithdrawals": True},
        {"ipRestrict": False},
        {"enableSpotAndMarginTrading": False},
        {"enableReading": False},
        {"permitsUniversalTransfer": True},
        {"enableInternalTransfer": True},
        {"enableMargin": True},
        {"enableFutures": True},
        {"enableVanillaOptions": True},
        {"enablePortfolioMarginTrading": True},
    ],
)
def test_risky_or_useless_keys_are_refused(change):
    assert "fail" in levels(runner.key_checks({**CLEAN_KEY, **change}))


def test_account_checks():
    cfg = RunConfig(mode="live", symbol="BTCUSDT", interval="1h", budget=D(100))
    ok = {
        "canTrade": True,
        "permissions": ["SPOT"],
        "balances": [{"asset": "USDT", "free": "500", "locked": "0"}],
    }
    assert levels(runner.account_checks(ok, RULES, cfg)) == ["ok"]
    assert "fail" in levels(runner.account_checks({**ok, "canTrade": False}, RULES, cfg))
    held = {**ok, "balances": ok["balances"] + [{"asset": "BTC", "free": "0.5", "locked": "0"}]}
    assert "warn" in levels(runner.account_checks(held, RULES, cfg))
    poor = {**ok, "balances": [{"asset": "USDT", "free": "10", "locked": "0"}]}
    assert "warn" in levels(runner.account_checks(poor, RULES, cfg))


def test_position_cap_must_clear_the_exchange_minimum():
    from spotbot.risk import RiskConfig

    cfg = RunConfig(
        mode="live",
        symbol="BTCUSDT",
        interval="1h",
        budget=D(100),
        risk=RiskConfig(max_position=D(5)),
    )
    assert "fail" in levels(runner.rules_checks(RULES, cfg))


class Tty(io.StringIO):
    def isatty(self):
        return True


def test_live_confirmation_needs_a_terminal_and_the_exact_phrase():
    cfg = RunConfig(mode="live", symbol="BTCUSDT", interval="1h", budget=D(100))
    out = io.StringIO()
    assert not runner.confirm_live(cfg, {}, stdin=io.StringIO("LIVE BTCUSDT\n"), stdout=out)
    assert not runner.confirm_live(cfg, {}, stdin=Tty("yes\n"), stdout=out)
    assert not runner.confirm_live(cfg, {}, stdin=Tty("live btcusdt\n"), stdout=out)
    assert runner.confirm_live(cfg, {"uid": 1}, stdin=Tty("LIVE BTCUSDT\n"), stdout=out)


def test_paper_mode_never_loads_credentials(monkeypatch):
    def boom(*a, **k):
        raise AssertionError("credentials were loaded in paper mode")

    monkeypatch.setattr(runner, "load_credentials", boom)
    monkeypatch.setattr(runner.PublicClient, "sync_time", lambda self: 0)
    public, signed, creds = runner.build_clients("paper")
    assert signed is None and creds is None
    assert public.origin == "https://data-api.binance.vision"


def test_second_bot_on_the_same_state_refuses(tmp_path):
    a, b = StateLock(tmp_path / "paper.lock"), StateLock(tmp_path / "paper.lock")
    a.acquire()
    with pytest.raises(RunError, match="already running"):
        b.acquire()
    a.release()
    b.acquire()
    b.release()


def test_live_requires_explicit_amounts_before_any_network(monkeypatch, capsys):
    monkeypatch.setattr(runner, "build_clients", lambda mode: pytest.fail("network used"))
    assert cli.main(["run", "BTCUSDT", "--live"]) == 2
    assert cli.main(["run", "BTCUSDT", "--live", "--budget", "100"]) == 2
    assert "--max-position" in capsys.readouterr().err


def test_testnet_and_live_are_mutually_exclusive():
    with pytest.raises(SystemExit):
        cli.build_parser().parse_args(["run", "BTCUSDT", "--testnet", "--live"])


def test_missing_testnet_keys_give_a_clear_error(monkeypatch, capsys):
    monkeypatch.setattr(runner.SignedClient, "sync_time", lambda self: 0)
    assert cli.main(["run", "BTCUSDT", "--testnet", "--no-dashboard"]) == 1
    err = capsys.readouterr().err
    assert "BINANCE_TESTNET_API_KEY" in err and "docs/api-keys.md" in err


def test_unreported_permission_fields_fail_closed():
    perms = dict(CLEAN_KEY)
    del perms["enableFutures"]
    (check,) = runner.key_checks(perms)
    assert check.level == "fail" and "enableFutures" in check.text


def test_run_loop_survives_a_late_wakeup(monkeypatch, tmp_path):
    clock = iter([0.0, 0.5, 1.2, 1.2, 2.0, 2.6, 3.4, 3.4, 4.0, 4.0, 5.0, 5.0, 6.0, 6.0])

    def sleep(seconds):
        if seconds < 0:
            raise ValueError("sleep length must be non-negative")

    monkeypatch.setattr(runner.time, "monotonic", lambda: next(clock))
    monkeypatch.setattr(runner.time, "sleep", sleep)
    monkeypatch.setattr(runner.signal, "signal", lambda *a: None)
    bot = runner.Runner(RunConfig(mode="paper", symbol="BTCUSDT", interval="1m", budget=D(10), poll_seconds=1),
                        public=None)  # fmt: skip
    steps = []

    def step():
        steps.append(1)
        if len(steps) == 3:
            bot.stop()

    bot.step = step
    bot.run()
    assert len(steps) == 3
