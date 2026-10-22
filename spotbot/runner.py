"""Run the engine against live market data: paper, Spot Testnet or the real exchange."""

from __future__ import annotations

import logging
import signal
import sys
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from decimal import Decimal
from pathlib import Path
from typing import Any, TextIO

from .binance import LIVE, MARKET_DATA, TESTNET, BinanceError, PublicClient, SignedClient
from .engine import Engine, EngineConfig
from .filters import SymbolRules
from .keys import Credentials, RedactingFilter, load_credentials
from .ledger import Store
from .model import Bar, interval_ms
from .risk import RiskConfig
from .strategy import RsiParams, RsiReversion
from .venues import BinanceVenue, Quote, SimVenue

log = logging.getLogger(__name__)

PUBLIC_ORIGIN = {"paper": MARKET_DATA, "testnet": TESTNET, "live": LIVE}

# Permissions a trade-only key must not have. Binance reports them per key.
FORBIDDEN_PERMISSIONS = {
    "enableWithdrawals": "withdrawals",
    "enableInternalTransfer": "internal transfers",
    "permitsUniversalTransfer": "universal transfers",
    "enableMargin": "margin loans/transfers",
    "enableFutures": "futures",
    "enableVanillaOptions": "options",
    "enablePortfolioMarginTrading": "portfolio margin",
}


class RunError(Exception):
    pass


@dataclass(frozen=True)
class RunConfig:
    mode: str  # paper | testnet | live
    symbol: str
    interval: str
    budget: Decimal
    params: RsiParams = field(default_factory=RsiParams)
    risk: RiskConfig = field(default_factory=RiskConfig)
    state_dir: Path = Path("state")
    poll_seconds: float = 5.0
    paper_fee_bps: Decimal = Decimal(10)
    paper_slippage_bps: Decimal = Decimal(0)

    @property
    def db_path(self) -> Path:
        return self.state_dir / f"{self.mode}-{self.symbol}.db"

    @property
    def kill_switch(self) -> Path:
        return self.state_dir / "STOP"


@dataclass
class Check:
    level: str  # ok | warn | fail
    text: str


def to_bar(row: list[Any]) -> Bar:
    return Bar(
        int(row[0]),
        float(row[1]),
        float(row[2]),
        float(row[3]),
        float(row[4]),
        float(row[5]),
        int(row[6]),
    )


def closed_bars(
    client: PublicClient, symbol: str, interval: str, now: int, start: int | None = None
) -> list[Bar]:
    rows = client.klines(symbol, interval, start=start, limit=1000)
    return [to_bar(r) for r in rows if int(r[6]) < now]


REQUIRED_PERMISSIONS = (
    "ipRestrict",
    "enableReading",
    "enableSpotAndMarginTrading",
    *FORBIDDEN_PERMISSIONS,
)


def key_checks(perms: dict[str, Any]) -> list[Check]:
    """Judge a live API key from GET /sapi/v1/account/apiRestrictions. Unknown means no."""
    missing = [k for k in REQUIRED_PERMISSIONS if not isinstance(perms.get(k), bool)]
    if missing:
        return [
            Check(
                "fail",
                "Binance did not report " + ", ".join(missing) + "; cannot vouch for this key",
            )
        ]
    checks = []
    if perms["ipRestrict"]:
        checks.append(Check("ok", "key only works from trusted IPs"))
    else:
        checks.append(
            Check(
                "fail",
                "key is not restricted to trusted IPs; add your server's IP in API Management",
            )
        )
    if not perms["enableReading"]:
        checks.append(Check("fail", "key cannot read account data (enable Reading)"))
    if not perms["enableSpotAndMarginTrading"]:
        checks.append(Check("fail", "key cannot trade spot (enable Spot & Margin Trading)"))
    bad = [name for k, name in FORBIDDEN_PERMISSIONS.items() if perms[k]]
    if bad:
        checks.append(
            Check(
                "fail",
                "key has permissions this bot never needs: " + ", ".join(bad) + "; turn them off",
            )
        )
    else:
        checks.append(
            Check("ok", "no withdrawal, transfer, margin-loan, futures or options permission")
        )
    return checks


def account_checks(account: dict[str, Any], rules: SymbolRules, cfg: RunConfig) -> list[Check]:
    checks = []
    if not account.get("canTrade", False):
        checks.append(Check("fail", "account cannot trade right now (canTrade is false)"))
    if "SPOT" not in account.get("permissions", ["SPOT"]):
        checks.append(Check("fail", "account has no SPOT permission"))
    balances = {b["asset"]: Decimal(b["free"]) for b in account.get("balances", [])}
    free_quote = balances.get(rules.quote_asset, Decimal(0))
    need = min(cfg.budget, cfg.risk.max_position or cfg.budget)
    if free_quote < need:
        checks.append(
            Check("warn", f"free {rules.quote_asset} is {free_quote}, less than one entry ({need})")
        )
    else:
        checks.append(Check("ok", f"free {rules.quote_asset}: {free_quote}"))
    held = balances.get(rules.base_asset, Decimal(0))
    if held > 0:
        checks.append(
            Check(
                "warn",
                f"account already holds {held} {rules.base_asset}; the bot only sells what it bought itself, "
                "a dedicated sub-account keeps that separation clean",
            )
        )
    return checks


def rules_checks(rules: SymbolRules, cfg: RunConfig) -> list[Check]:
    checks = []
    problem = rules.tradable_problem()
    if problem:
        checks.append(Check("fail", problem))
    cap = cfg.risk.max_position or cfg.budget
    if rules.min_notional and cap < rules.min_notional * Decimal("1.05"):
        checks.append(
            Check(
                "fail",
                f"max position {cap} is too close to the exchange minimum order of {rules.min_notional}",
            )
        )
    return checks


def confirm_live(
    cfg: RunConfig, account: dict[str, Any], stdin: TextIO = sys.stdin, stdout: TextIO = sys.stdout
) -> bool:
    """Typed confirmation. There is deliberately no flag to skip it."""
    if not stdin.isatty():
        stdout.write("Live trading needs an interactive terminal to confirm. Refusing.\n")
        return False
    phrase = f"LIVE {cfg.symbol}"
    stdout.write(
        "\nAbout to trade REAL funds on Binance:\n"
        f"  account uid    {account.get('uid', '?')}\n"
        f"  symbol         {cfg.symbol} on {cfg.interval} bars\n"
        f"  strategy       {cfg.params.describe()}\n"
        f"  budget         {cfg.budget} (bot equity used for sizing)\n"
        f"  max position   {cfg.risk.max_position}\n"
        f"  stop / daily   {cfg.risk.stop_loss_pct}% / {cfg.risk.daily_loss_pct}% "
        "(triggers checked while the bot runs, not guaranteed loss limits)\n"
        f"\nType {phrase} to start, anything else to abort: "
    )
    stdout.flush()
    return stdin.readline().strip() == phrase


class StateLock:
    """One bot per mode and state directory; a second one refuses to start."""

    def __init__(self, path: Path) -> None:
        self.path = path
        self._fh: Any = None

    def acquire(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._fh = self.path.open("a+")
        try:
            if sys.platform == "win32":
                import msvcrt

                msvcrt.locking(self._fh.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl

                fcntl.flock(self._fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            self._fh.close()
            raise RunError(
                f"another bot is already running with {self.path.parent} in this mode"
            ) from None

    def release(self) -> None:
        if self._fh:
            self._fh.close()
            self._fh = None


class Runner:
    def __init__(
        self,
        cfg: RunConfig,
        *,
        public: PublicClient,
        signed: SignedClient | None = None,
        out: Callable[[Engine, int], None] | None = None,
        account: str | None = None,
    ) -> None:
        self.cfg = cfg
        self.public = public
        self.signed = signed
        self.out = out
        self.account = account
        self.quote = Quote()
        self.store: Store | None = None
        self.engine: Engine | None = None
        self.step_ms = interval_ms(cfg.interval)
        self._stop = False
        self._next_bar_check = 0
        self._next_sync = 0.0

    def clock(self) -> int:
        return (self.signed or self.public).now_ms()

    def setup(self) -> Engine:
        cfg = self.cfg
        cfg.state_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
        if sys.platform != "win32":
            cfg.state_dir.chmod(0o700)  # ledger, WAL files and logs: trade history is private
        rules = SymbolRules.from_exchange_info(self.public.exchange_info(cfg.symbol))
        problem = rules.tradable_problem()
        if problem:
            raise RunError(problem)
        self.store = Store(cfg.db_path)
        if cfg.mode == "paper":
            venue = SimVenue(
                "paper",
                rules,
                self.quote,
                self.store,
                fee_rate=cfg.paper_fee_bps / Decimal(10_000),
                slippage=cfg.paper_slippage_bps / Decimal(10_000),
                prices=self.public,
            )
        else:
            assert self.signed is not None
            venue = BinanceVenue(self.signed, rules)
        self.engine = Engine(
            EngineConfig(
                symbol=cfg.symbol,
                interval=cfg.interval,
                budget=cfg.budget,
                risk=cfg.risk,
                account=self.account or self._account_id(),
                kill_switch=cfg.kill_switch,
            ),
            venue,
            self.store,
            RsiReversion(cfg.params),
            self.quote,
            clock=self.clock,
        )
        now = self.clock()
        bars = closed_bars(self.public, cfg.symbol, cfg.interval, now)
        if len(bars) < cfg.params.warmup_bars:
            raise RunError(f"only {len(bars)} closed bars available; need {cfg.params.warmup_bars}")
        self.engine.warmup(bars)
        self._refresh_quote(now)
        self.engine.reconcile(now)
        self.engine.on_tick(now)  # also checks balances once no order is unresolved
        rsi = self.engine.strategy.rsi.value
        log.info(
            "%s %s %s: warmed up on %d bars (RSI %s), budget %s %s, state %s",
            cfg.mode,
            cfg.symbol,
            cfg.interval,
            len(bars),
            "-" if rsi is None else f"{rsi:.1f}",
            cfg.budget,
            rules.quote_asset,
            cfg.db_path,
        )
        return self.engine

    def _account_id(self) -> str:
        """The exchange account the ledger belongs to (Binance uid, or the key's fingerprint)."""
        if self.signed is None:
            return "paper"
        uid = self.signed.account().get("uid")
        return f"{self.cfg.mode}:{uid}" if uid else f"{self.cfg.mode}:key-{self.signed.fingerprint}"

    def _refresh_quote(self, now: int) -> bool:
        try:
            book = self.public.book_ticker(self.cfg.symbol)
        except BinanceError as exc:
            log.warning("book ticker failed: %s", exc)
            return False
        self.quote.set(Decimal(book["bidPrice"]), Decimal(book["askPrice"]), now)
        return True

    def step(self) -> None:
        assert self.engine is not None
        if time.monotonic() >= self._next_sync:
            try:
                (self.signed or self.public).sync_time()
            except BinanceError as exc:
                log.warning("time sync failed: %s", exc)
            self._next_sync = time.monotonic() + 600
        fresh = self._refresh_quote(self.clock())
        # STOP file, day roll and order checks run even without a quote.
        self.engine.on_tick(self.clock(), fresh=fresh)
        if self.clock() >= self._next_bar_check:
            self._process_new_bars(fresh)
        if self.out:
            self.out(self.engine, self.clock())

    def _process_new_bars(self, fresh: bool) -> None:
        assert self.engine is not None
        last = self.engine.state.last_bar
        start = None if last is None else last + self.step_ms
        now = self.clock()
        try:
            bars = closed_bars(self.public, self.cfg.symbol, self.cfg.interval, now, start=start)
        except BinanceError as exc:
            log.warning("klines failed: %s", exc)
            self._next_bar_check = now + 5_000
            return
        for bar in bars:
            # Decisions use the time they are made at, after the network calls above.
            self.engine.on_bar_close(bar, self.clock(), fresh=fresh)
            self.engine.record_equity(bar.close_time)
        now = self.clock()
        self._next_bar_check = now - now % self.step_ms + self.step_ms + 1_500

    def stop(self, *_: object) -> None:
        self._stop = True

    def run(self) -> None:
        signal.signal(signal.SIGINT, self.stop)
        signal.signal(signal.SIGTERM, self.stop)
        while not self._stop:
            started = time.monotonic()
            try:
                self.step()
            except BinanceError as exc:
                log.warning("step failed: %s", exc)
            except Exception:
                # Keep watching the position; the traceback goes to the log file.
                log.exception("unexpected error; continuing")
            deadline = started + self.cfg.poll_seconds
            while not self._stop:
                left = deadline - time.monotonic()
                if left <= 0:
                    break
                time.sleep(min(0.2, left))

    def close(self) -> None:
        if self.store:
            self.store.close()


def build_clients(mode: str) -> tuple[PublicClient, SignedClient | None, Credentials | None]:
    """Paper mode never touches credentials; testnet/live load them for their own mode only."""
    if mode == "paper":
        public = PublicClient(PUBLIC_ORIGIN["paper"])
        public.sync_time()
        return public, None, None
    creds = load_credentials(mode)
    for handler in logging.getLogger().handlers:
        handler.addFilter(RedactingFilter(creds.secrets()))
    signed = SignedClient(creds)
    signed.sync_time()
    return signed, signed, creds


def preflight(
    cfg: RunConfig, signed: SignedClient, rules: SymbolRules
) -> tuple[list[Check], dict[str, Any]]:
    checks = []
    if abs(signed.offset_ms) > 1000:
        checks.append(
            Check(
                "warn",
                f"local clock is {signed.offset_ms} ms off the exchange; requests use exchange time",
            )
        )
    if cfg.mode == "live":
        checks += key_checks(signed.api_restrictions())
    account = signed.account()
    checks += account_checks(account, rules, cfg)
    checks += rules_checks(rules, cfg)
    return checks, account
