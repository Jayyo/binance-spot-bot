import hashlib
import io
import zipfile

import pytest

from spotbot import data
from spotbot.data import DataError
from spotbot.model import Bar

H = 3_600_000
T = 1_735_689_600_000  # 2025-01-01 00:00 UTC


def row(open_ms, o=100, h=101, lo=99, c=100.5, close_ms=None, micro=False):
    close_ms = open_ms + H - 1 if close_ms is None else close_ms
    scale = 1000 if micro else 1
    return f"{open_ms * scale},{o},{h},{lo},{c},12.5,{close_ms * scale + (999 if micro else 0)},1250,10,6,600,0"


def make_zip(lines, name="X-1h-2025-01.csv", header=False):
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        body = (
            "open_time,open,high,low,close,volume,close_time,q,n,tb,tq,ignore\n" if header else ""
        )
        zf.writestr(name, body + "\n".join(lines) + "\n")
    return buf.getvalue()


def test_reads_millisecond_and_microsecond_archives(tmp_path):
    ms = tmp_path / "ms.zip"
    us = tmp_path / "us.zip"
    ms.write_bytes(make_zip([row(T - H)], header=True))
    us.write_bytes(make_zip([row(T, micro=True)]))
    (a,) = data.parse_archive(ms)
    (b,) = data.parse_archive(us)
    assert a.open_time == T - H and a.close_time == T - 1
    assert b.open_time == T and b.close_time == T + H - 1
    assert (b.open, b.high, b.low, b.close) == (100, 101, 99, 100.5)


def bars(*opens, **kw):
    return [Bar(t, 100, 101, 99, 100, 1, t + H - 1) for t in opens]


def test_gaps_are_reported_not_filled():
    gaps = data.check_bars(bars(T, T + H, T + 3 * H), "1h")
    assert gaps == [(T + H, T + 3 * H)]


@pytest.mark.parametrize(
    "broken",
    [
        bars(T, T),  # duplicate
        bars(T + H, T),  # out of order
        bars(T + 1),  # not aligned to the interval
        [Bar(T, 100, 99, 101, 100, 1, T + H - 1)],  # high below low
        [Bar(T, 100, 101, 99, 100, 1, T + 2 * H)],  # closes after the next bar opened
    ],
)
def test_broken_data_is_rejected(broken):
    with pytest.raises(DataError):
        data.check_bars(broken, "1h")


def test_bar_cut_short_by_a_halt_is_accepted_and_normalised(tmp_path):
    folder = tmp_path / "X" / "1h"
    folder.mkdir(parents=True)
    archive = folder / "X-1h-2025-01.zip"
    archive.write_bytes(make_zip([row(T, close_ms=T + 20 * 60_000), row(T + H)]))
    ds = data.load("X", "1h", "2025-01", "2025-01", tmp_path, download=False)
    assert ds.bars[0].close_time == T + H - 1
    assert ds.files[0].sha256 == hashlib.sha256(archive.read_bytes()).hexdigest()


class FakeResponse:
    def __init__(self, status, content=b""):
        self.status_code = status
        self.content = content
        self.text = content.decode(errors="replace")

    def raise_for_status(self):
        if self.status_code >= 400:
            raise RuntimeError(self.status_code)


class FakeSession:
    def __init__(self, files):
        self.files = files
        self.calls = []

    def get(self, url, timeout):
        self.calls.append(url)
        name = url.rsplit("/", 1)[1]
        if name not in self.files:
            return FakeResponse(404)
        return FakeResponse(200, self.files[name])


def archive_files(content, checksum=None):
    digest = checksum or hashlib.sha256(content).hexdigest()
    name = "BTCUSDT-1h-2025-01.zip"
    return {name: content, name + ".CHECKSUM": f"{digest}  {name}\n".encode()}


def test_fetch_verifies_checksum_and_caches(tmp_path):
    content = make_zip([row(T)])
    session = FakeSession(archive_files(content))
    path = data.fetch_month("BTCUSDT", "1h", "2025-01", tmp_path, session)
    assert path.read_bytes() == content
    calls = len(session.calls)
    data.fetch_month("BTCUSDT", "1h", "2025-01", tmp_path, session)
    assert len(session.calls) == calls  # verified copy on disk: no download


def test_fetch_rejects_a_checksum_mismatch(tmp_path):
    session = FakeSession(archive_files(make_zip([row(T)]), checksum="0" * 64))
    with pytest.raises(DataError, match="checksum mismatch"):
        data.fetch_month("BTCUSDT", "1h", "2025-01", tmp_path, session)
    assert not list(tmp_path.rglob("*.zip"))


def test_missing_month_has_a_clear_message(tmp_path):
    with pytest.raises(DataError, match="not in the public archive"):
        data.fetch_month("BTCUSDT", "1h", "2025-01", tmp_path, FakeSession({}))


def test_manifest_pins_exact_files(tmp_path):
    ds = data.Dataset("X", "1h", [], [data.ArchiveFile("a.zip", "1" * 64, 1)])
    manifest = tmp_path / "manifest.json"
    data.write_manifest([ds], manifest)
    data.verify_manifest([ds], manifest)
    ds.files[0] = data.ArchiveFile("a.zip", "2" * 64, 1)
    with pytest.raises(DataError, match="differs"):
        data.verify_manifest([ds], manifest)


def test_month_helpers():
    assert data.month_range("2024-11", "2025-02") == ["2024-11", "2024-12", "2025-01", "2025-02"]
    assert data.previous_month("2025-01", 2) == "2024-11"
    with pytest.raises(DataError):
        data.month_range("2025-03", "2025-01")


@pytest.mark.parametrize("month", ["2022-13", "2022-00", "22-01", "2022/01"])
def test_impossible_months_are_rejected(month):
    with pytest.raises(DataError):
        data.month_range(month, "2023-01")


def test_offline_load_checks_cached_checksums(tmp_path):
    folder = tmp_path / "X" / "1h"
    folder.mkdir(parents=True)
    archive = folder / "X-1h-2025-01.zip"
    archive.write_bytes(make_zip([row(T)]))
    assert data.load("X", "1h", "2025-01", "2025-01", tmp_path, download=False).verified is False
    checksum = archive.with_name(archive.name + ".CHECKSUM")
    checksum.write_text(hashlib.sha256(archive.read_bytes()).hexdigest() + "  X-1h-2025-01.zip\n")
    assert data.load("X", "1h", "2025-01", "2025-01", tmp_path, download=False).verified is True
    checksum.write_text("0" * 64 + "  X-1h-2025-01.zip\n")
    with pytest.raises(DataError, match="does not match"):
        data.load("X", "1h", "2025-01", "2025-01", tmp_path, download=False)
