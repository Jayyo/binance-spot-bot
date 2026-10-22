"""SQLite ledger: order intents, fills, bot state and events.

The book (cash, quantity, fees) is always recomputed from recorded fills, so a
fill that is recorded twice or lost on a crash is the only way to get it wrong,
and both are guarded by the (symbol, trade_id) primary key and reconciliation.
"""

from __future__ import annotations

import os
import sqlite3
import sys
from collections.abc import Iterable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from decimal import Decimal
from pathlib import Path

from .model import Fill, Side, Status, dec
from .risk import BotState

SCHEMA = """
CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS orders (
    client_id TEXT PRIMARY KEY,
    symbol TEXT NOT NULL,
    side TEXT NOT NULL,
    purpose TEXT NOT NULL,
    quote_qty TEXT,
    qty TEXT,
    decision_bar INTEGER,
    status TEXT NOT NULL,
    order_id INTEGER,
    executed_qty TEXT NOT NULL DEFAULT '0',
    cumulative_quote TEXT NOT NULL DEFAULT '0',
    created_at INTEGER NOT NULL,
    updated_at INTEGER NOT NULL,
    next_check_at INTEGER NOT NULL DEFAULT 0,
    checks INTEGER NOT NULL DEFAULT 0,
    error TEXT NOT NULL DEFAULT ''
);
CREATE TABLE IF NOT EXISTS fills (
    symbol TEXT NOT NULL,
    trade_id INTEGER NOT NULL,
    order_id INTEGER NOT NULL,
    client_id TEXT NOT NULL,
    side TEXT NOT NULL,
    price TEXT NOT NULL,
    qty TEXT NOT NULL,
    quote_qty TEXT NOT NULL,
    commission TEXT NOT NULL,
    commission_asset TEXT NOT NULL,
    commission_quote TEXT,
    time INTEGER NOT NULL,
    PRIMARY KEY (symbol, trade_id)
);
CREATE TABLE IF NOT EXISTS state (symbol TEXT PRIMARY KEY, json TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    time INTEGER NOT NULL,
    level TEXT NOT NULL,
    message TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS equity (
    time INTEGER PRIMARY KEY,
    equity TEXT NOT NULL,
    price TEXT NOT NULL,
    qty TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS sim_orders (client_id TEXT PRIMARY KEY, json TEXT NOT NULL);
CREATE INDEX IF NOT EXISTS orders_open ON orders(symbol, status);
"""

OPEN_STATUSES = tuple(s.value for s in Status if not s.terminal)


class LedgerConflict(Exception):
    pass


@dataclass(slots=True)
class OrderRow:
    client_id: str
    symbol: str
    side: Side
    purpose: str
    quote_qty: Decimal | None
    qty: Decimal | None
    decision_bar: int | None
    status: Status
    order_id: int | None
    executed_qty: Decimal
    cumulative_quote: Decimal
    created_at: int
    updated_at: int
    next_check_at: int
    checks: int
    error: str


@dataclass(slots=True)
class Book:
    budget: Decimal
    cash: Decimal = Decimal(0)
    qty: Decimal = Decimal(0)
    fees_quote: Decimal = Decimal(0)  # every fee we could value, in quote terms
    outside_fees: Decimal = Decimal(0)  # fees paid in a third asset (e.g. BNB), valued
    unvalued_fees: dict[str, Decimal] = field(default_factory=dict)

    def equity(self, price: Decimal) -> Decimal:
        return self.cash + self.qty * price - self.outside_fees


def book_from_fills(budget: Decimal, fills: Iterable[Fill], base: str, quote: str) -> Book:
    book = Book(budget=budget, cash=budget)
    for f in fills:
        if f.side is Side.BUY:
            book.cash -= f.quote_qty
            book.qty += f.qty
        else:
            book.cash += f.quote_qty
            book.qty -= f.qty
        if f.commission == 0:
            continue
        if f.commission_asset == base:
            book.qty -= f.commission
            book.fees_quote += f.commission * f.price
        elif f.commission_asset == quote:
            book.cash -= f.commission
            book.fees_quote += f.commission
        elif f.commission_quote is not None:
            book.outside_fees += f.commission_quote
            book.fees_quote += f.commission_quote
        else:
            book.unvalued_fees[f.commission_asset] = (
                book.unvalued_fees.get(f.commission_asset, Decimal(0)) + f.commission
            )
    return book


class Store:
    def __init__(self, path: str | Path = ":memory:") -> None:
        self.path = str(path)
        fresh = self.path == ":memory:" or not os.path.exists(self.path)
        self.conn = sqlite3.connect(self.path, isolation_level=None, check_same_thread=False)
        self.conn.execute(
            "PRAGMA journal_mode=WAL" if self.path != ":memory:" else "PRAGMA journal_mode=MEMORY"
        )
        self.conn.execute("PRAGMA synchronous=FULL")
        self.conn.executescript(SCHEMA)
        if fresh and self.path != ":memory:" and sys.platform != "win32":
            os.chmod(self.path, 0o600)
        self._depth = 0

    def close(self) -> None:
        self.conn.close()

    @contextmanager
    def tx(self) -> Iterator[None]:
        """Group writes; nested use joins the outer transaction."""
        if self._depth == 0:
            self.conn.execute("BEGIN IMMEDIATE")
        self._depth += 1
        try:
            yield
        except BaseException:
            self._depth -= 1
            if self._depth == 0:
                self.conn.execute("ROLLBACK")
            raise
        self._depth -= 1
        if self._depth == 0:
            self.conn.execute("COMMIT")

    # meta
    def get_meta(self, key: str) -> str | None:
        row = self.conn.execute("SELECT value FROM meta WHERE key=?", (key,)).fetchone()
        return row[0] if row else None

    def set_meta(self, key: str, value: str) -> None:
        self.conn.execute(
            "INSERT INTO meta(key, value) VALUES(?, ?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            (key, value),
        )

    # orders
    def insert_order(self, row: OrderRow) -> None:
        self.conn.execute(
            "INSERT INTO orders VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                row.client_id,
                row.symbol,
                row.side.value,
                row.purpose,
                None if row.quote_qty is None else str(row.quote_qty),
                None if row.qty is None else str(row.qty),
                row.decision_bar,
                row.status.value,
                row.order_id,
                str(row.executed_qty),
                str(row.cumulative_quote),
                row.created_at,
                row.updated_at,
                row.next_check_at,
                row.checks,
                row.error,
            ),
        )

    def update_order(self, client_id: str, **fields: object) -> None:
        allowed = {
            "status",
            "order_id",
            "executed_qty",
            "cumulative_quote",
            "updated_at",
            "next_check_at",
            "checks",
            "error",
        }
        sets, values = [], []
        for key, value in fields.items():
            if key not in allowed:
                raise KeyError(key)
            if isinstance(value, Status):
                value = value.value
            elif isinstance(value, Decimal):
                value = str(value)
            sets.append(f"{key}=?")
            values.append(value)
        values.append(client_id)
        self.conn.execute(f"UPDATE orders SET {', '.join(sets)} WHERE client_id=?", values)

    def _order(self, r: tuple) -> OrderRow:
        return OrderRow(
            client_id=r[0],
            symbol=r[1],
            side=Side(r[2]),
            purpose=r[3],
            quote_qty=None if r[4] is None else dec(r[4]),
            qty=None if r[5] is None else dec(r[5]),
            decision_bar=r[6],
            status=Status(r[7]),
            order_id=r[8],
            executed_qty=dec(r[9]),
            cumulative_quote=dec(r[10]),
            created_at=r[11],
            updated_at=r[12],
            next_check_at=r[13],
            checks=r[14],
            error=r[15],
        )

    def get_order(self, client_id: str) -> OrderRow | None:
        r = self.conn.execute("SELECT * FROM orders WHERE client_id=?", (client_id,)).fetchone()
        return self._order(r) if r else None

    def orders(self, symbol: str) -> list[OrderRow]:
        rows = self.conn.execute(
            "SELECT * FROM orders WHERE symbol=? ORDER BY created_at, rowid", (symbol,)
        ).fetchall()
        return [self._order(r) for r in rows]

    def open_orders(self, symbol: str) -> list[OrderRow]:
        marks = ",".join("?" * len(OPEN_STATUSES))
        rows = self.conn.execute(
            f"SELECT * FROM orders WHERE symbol=? AND status IN ({marks}) ORDER BY created_at, rowid",
            (symbol, *OPEN_STATUSES),
        ).fetchall()
        return [self._order(r) for r in rows]

    # fills
    def insert_fills(self, symbol: str, fills: Iterable[Fill]) -> list[Fill]:
        """Record fills; return the ones that were new.

        A trade id seen before is the same fill again, unless it belongs to a different
        order: then the exchange's ids no longer match this ledger (a Spot Testnet reset
        does that) and carrying on would mix two histories.
        """
        added = []
        for f in fills:
            known = self.conn.execute(
                "SELECT order_id, client_id FROM fills WHERE symbol=? AND trade_id=?",
                (symbol, f.trade_id),
            ).fetchone()
            if known is not None:
                if tuple(known) != (f.order_id, f.client_id):
                    raise LedgerConflict(
                        f"trade {f.trade_id} is recorded for order {known[1]}, now reported for "
                        f"{f.client_id}; the exchange history was reset or belongs to another account"
                    )
                continue
            self.conn.execute(
                "INSERT INTO fills VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                (
                    symbol,
                    f.trade_id,
                    f.order_id,
                    f.client_id,
                    f.side.value,
                    str(f.price),
                    str(f.qty),
                    str(f.quote_qty),
                    str(f.commission),
                    f.commission_asset,
                    None if f.commission_quote is None else str(f.commission_quote),
                    f.time,
                ),
            )
            added.append(f)
        return added

    def fills(self, symbol: str, client_id: str | None = None) -> list[Fill]:
        sql = "SELECT * FROM fills WHERE symbol=?"
        args: list[object] = [symbol]
        if client_id is not None:
            sql += " AND client_id=?"
            args.append(client_id)
        rows = self.conn.execute(sql + " ORDER BY time, trade_id", args).fetchall()
        return [
            Fill(
                trade_id=r[1],
                order_id=r[2],
                client_id=r[3],
                side=Side(r[4]),
                price=dec(r[5]),
                qty=dec(r[6]),
                quote_qty=dec(r[7]),
                commission=dec(r[8]),
                commission_asset=r[9],
                commission_quote=None if r[10] is None else dec(r[10]),
                time=r[11],
            )
            for r in rows
        ]

    def max_trade_id(self, symbol: str) -> int | None:
        row = self.conn.execute(
            "SELECT MAX(trade_id) FROM fills WHERE symbol=?", (symbol,)
        ).fetchone()
        return row[0]

    # bot state
    def load_state(self, symbol: str) -> BotState | None:
        row = self.conn.execute("SELECT json FROM state WHERE symbol=?", (symbol,)).fetchone()
        return BotState.from_json(row[0]) if row else None

    def save_state(self, symbol: str, state: BotState) -> None:
        self.conn.execute(
            "INSERT INTO state(symbol, json) VALUES(?, ?) ON CONFLICT(symbol) DO UPDATE SET json=excluded.json",
            (symbol, state.to_json()),
        )

    # events and equity
    def add_event(self, time: int, level: str, message: str) -> None:
        self.conn.execute(
            "INSERT INTO events(time, level, message) VALUES(?,?,?)", (time, level, message)
        )

    def recent_events(self, limit: int = 10) -> list[tuple[int, str, str]]:
        rows = self.conn.execute(
            "SELECT time, level, message FROM events ORDER BY id DESC LIMIT ?", (limit,)
        ).fetchall()
        return list(reversed(rows))

    def add_equity(self, time: int, equity: Decimal, price: Decimal, qty: Decimal) -> None:
        self.conn.execute(
            "INSERT OR REPLACE INTO equity VALUES(?,?,?,?)",
            (time, str(equity), str(price), str(qty)),
        )

    def equity_curve(self) -> list[tuple[int, Decimal, Decimal, Decimal]]:
        rows = self.conn.execute(
            "SELECT time, equity, price, qty FROM equity ORDER BY time"
        ).fetchall()
        return [(t, dec(e), dec(p), dec(q)) for t, e, p, q in rows]

    # simulated venues keep their own order records here
    def save_sim_order(self, client_id: str, payload: str) -> None:
        self.conn.execute("INSERT OR REPLACE INTO sim_orders VALUES(?, ?)", (client_id, payload))

    def load_sim_order(self, client_id: str) -> str | None:
        row = self.conn.execute(
            "SELECT json FROM sim_orders WHERE client_id=?", (client_id,)
        ).fetchone()
        return row[0] if row else None
