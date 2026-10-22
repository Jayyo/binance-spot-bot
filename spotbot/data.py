"""Historical klines from Binance's public archive (data.binance.vision).

Monthly zip files are downloaded once, checked against the SHA-256 that Binance
publishes next to each file, and cached. Spot archives switched from
millisecond to microsecond timestamps in January 2025; both are read.
"""

from __future__ import annotations

import csv
import hashlib
import io
import json
import zipfile
from dataclasses import dataclass, field, replace
from pathlib import Path

import requests

from .model import Bar, fmt_time, interval_ms

ARCHIVE = "https://data.binance.vision/data/spot/monthly/klines/{s}/{i}/{s}-{i}-{m}.zip"


class DataError(Exception):
    pass


@dataclass(frozen=True, slots=True)
class ArchiveFile:
    name: str
    sha256: str
    rows: int


@dataclass(slots=True)
class Dataset:
    symbol: str
    interval: str
    bars: list[Bar]
    files: list[ArchiveFile]
    gaps: list[tuple[int, int]] = field(default_factory=list)  # (last bar before, first bar after)
    verified: bool = False  # every file matched the SHA-256 Binance publishes for it

    def gap_lines(self) -> list[str]:
        step = interval_ms(self.interval)
        lines = []
        for a, b in self.gaps:
            missing = (b - a) // step - 1
            span = (
                fmt_time(a + step)
                if missing == 1
                else f"{fmt_time(a + step)} to {fmt_time(b - step)}"
            )
            lines.append(f"{span} UTC ({missing} bar{'s' if missing > 1 else ''} missing)")
        return lines


def parse_month(text: str) -> tuple[int, int]:
    try:
        y, m = (int(x) for x in text.split("-"))
    except ValueError:
        raise DataError(f"{text!r}: months are written YYYY-MM") from None
    if not (2017 <= y <= 2100 and 1 <= m <= 12):
        raise DataError(f"{text!r} is not a month Binance has data for")
    return y, m


def month_range(start: str, end: str) -> list[str]:
    y, m = parse_month(start)
    ey, em = parse_month(end)
    if (y, m) > (ey, em):
        raise DataError(f"start {start} is after end {end}")
    out = []
    while (y, m) <= (ey, em):
        out.append(f"{y:04d}-{m:02d}")
        y, m = (y + 1, 1) if m == 12 else (y, m + 1)
    return out


def previous_month(month: str, count: int = 1) -> str:
    y, m = parse_month(month)
    for _ in range(count):
        y, m = (y - 1, 12) if m == 1 else (y, m - 1)
    return f"{y:04d}-{m:02d}"


def sha256_of(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def fetch_month(
    symbol: str, interval: str, month: str, data_dir: Path, session: requests.Session | None = None
) -> Path:
    """Download one monthly archive unless a verified copy is already cached."""
    folder = data_dir / symbol / interval
    target = folder / f"{symbol}-{interval}-{month}.zip"
    checksum_file = target.with_name(target.name + ".CHECKSUM")
    if target.exists() and checksum_file.exists() and sha256_of(target) == _expected(checksum_file):
        return target
    folder.mkdir(parents=True, exist_ok=True)
    http = session or requests.Session()
    url = ARCHIVE.format(s=symbol, i=interval, m=month)
    checksum = http.get(url + ".CHECKSUM", timeout=30)
    if checksum.status_code == 404:
        raise DataError(
            f"{symbol} {interval} {month} is not in the public archive "
            "(wrong symbol, or the month is not finished yet)"
        )
    checksum.raise_for_status()
    body = http.get(url, timeout=60)
    body.raise_for_status()
    expected = checksum.text.split()[0].strip().lower()
    actual = hashlib.sha256(body.content).hexdigest()
    if actual != expected:
        raise DataError(
            f"checksum mismatch for {target.name}: got {actual}, Binance lists {expected}"
        )
    tmp = target.with_suffix(".part")
    tmp.write_bytes(body.content)
    tmp.replace(target)
    checksum_file.write_text(checksum.text)
    return target


def _expected(checksum_file: Path) -> str:
    return checksum_file.read_text().split()[0].strip().lower()


def _to_ms(raw: str) -> int:
    value = int(raw)
    # 16-digit values are microseconds (spot archives from 2025-01 on).
    return value // 1000 if value >= 10**15 else value


def parse_archive(path: Path) -> list[Bar]:
    with zipfile.ZipFile(path) as zf:
        names = [n for n in zf.namelist() if n.endswith(".csv")]
        if len(names) != 1:
            raise DataError(f"{path.name}: expected one CSV inside, found {len(names)}")
        text = io.TextIOWrapper(zf.open(names[0]), encoding="ascii")
        bars = []
        for row in csv.reader(text):
            if not row or not row[0].strip().isdigit():
                continue  # header line in some archives
            bars.append(
                Bar(
                    open_time=_to_ms(row[0]),
                    open=float(row[1]),
                    high=float(row[2]),
                    low=float(row[3]),
                    close=float(row[4]),
                    volume=float(row[5]),
                    close_time=_to_ms(row[6]),
                )
            )
    return bars


def check_bars(bars: list[Bar], interval: str) -> list[tuple[int, int]]:
    """Raise on broken data; return gaps (missing bars are reported, never filled)."""
    step = interval_ms(interval)
    gaps = []
    for i, b in enumerate(bars):
        if not (b.low <= min(b.open, b.close) <= max(b.open, b.close) <= b.high) or b.low <= 0:
            raise DataError(f"bad OHLC at {fmt_time(b.open_time)}: {b}")
        if b.open_time % step:
            raise DataError(f"bar at {fmt_time(b.open_time)} is not aligned to {interval}")
        # Bars cut short by an exchange halt close early (2023-03-24 12:00 closes at
        # 12:39), and a few archived close times are off by a second or so.
        if not b.open_time < b.close_time <= b.open_time + step + 60_000:
            raise DataError(f"bar at {fmt_time(b.open_time)} has close time {b.close_time}")
        if i:
            prev = bars[i - 1].open_time
            if b.open_time <= prev:
                raise DataError(f"duplicate or out-of-order bar at {fmt_time(b.open_time)}")
            if b.open_time - prev != step:
                gaps.append((prev, b.open_time))
    return gaps


def load(
    symbol: str,
    interval: str,
    start: str,
    end: str,
    data_dir: Path,
    *,
    download: bool = True,
    session: requests.Session | None = None,
) -> Dataset:
    interval_ms(interval)  # validates
    bars: list[Bar] = []
    files: list[ArchiveFile] = []
    verified = True
    for month in month_range(start, end):
        path = data_dir / symbol / interval / f"{symbol}-{interval}-{month}.zip"
        if download:
            path = fetch_month(symbol, interval, month, data_dir, session)
        elif not path.exists():
            raise DataError(f"{path} is missing; run `spotbot fetch` first")
        else:
            checksum = path.with_name(path.name + ".CHECKSUM")
            if not checksum.exists():
                verified = False
            elif sha256_of(path) != _expected(checksum):
                raise DataError(
                    f"{path.name} does not match its .CHECKSUM file; delete it and fetch again"
                )
        month_bars = parse_archive(path)
        bars.extend(month_bars)
        files.append(ArchiveFile(path.name, sha256_of(path), len(month_bars)))
    gaps = check_bars(bars, interval)
    step = interval_ms(interval)
    bars = [
        b
        if b.close_time == b.open_time + step - 1
        else replace(b, close_time=b.open_time + step - 1)
        for b in bars
    ]
    return Dataset(symbol, interval, bars, files, gaps, verified)


def write_manifest(datasets: list[Dataset], path: Path) -> None:
    payload = {
        "source": "https://data.binance.vision (spot monthly klines)",
        "files": {f.name: f.sha256 for ds in datasets for f in ds.files},
    }
    path.write_text(json.dumps(payload, indent=1, sort_keys=True) + "\n")


def verify_manifest(datasets: list[Dataset], path: Path) -> None:
    pinned = json.loads(path.read_text())["files"]
    for ds in datasets:
        for f in ds.files:
            want = pinned.get(f.name)
            if want is None:
                raise DataError(f"{f.name} is not pinned in {path}")
            if want != f.sha256:
                raise DataError(f"{f.name} differs from the pinned copy ({f.sha256} != {want})")
