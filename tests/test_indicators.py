import pytest
from hypothesis import given
from hypothesis import strategies as st

from spotbot.indicators import Ema, Rsi
from tests.reference import ema_series, rsi_series

# Wilder's RSI example as tabulated in the RSI spreadsheet of StockCharts' ChartSchool.
CLOSES = [
    44.3389, 44.0902, 44.1497, 43.6124, 44.3278, 44.8264, 45.0955, 45.4245, 45.8433, 46.0826,
    45.8931, 46.0328, 45.6140, 46.2820, 46.2820, 46.0028, 46.0328, 46.4116, 46.2222, 45.6439,
    46.2122, 46.2521, 45.7137, 46.4515, 45.7835, 45.3548, 44.0288, 44.1783, 44.2181, 44.5672,
    43.4205, 42.6628, 43.1314,
]  # fmt: skip
EXPECTED = [
    70.53, 66.32, 66.55, 69.41, 66.36, 57.97, 62.93, 63.26, 56.06, 62.38,
    54.71, 50.42, 39.99, 41.46, 41.87, 45.46, 37.30, 33.08, 37.77,
]  # fmt: skip


def test_rsi_matches_published_wilder_example():
    rsi = Rsi(14)
    values = [rsi.update(c) for c in CLOSES]
    assert values[:14] == [None] * 14
    assert [round(v, 2) for v in values[14:]] == EXPECTED


@pytest.mark.parametrize(
    ("closes", "expected"),
    [
        ([10.0] * 20, 50.0),  # flat: neither signal
        ([float(i) for i in range(20)], 100.0),  # only gains
        ([float(20 - i) for i in range(20)], 0.0),  # only losses
    ],
)
def test_rsi_edge_cases(closes, expected):
    rsi = Rsi(14)
    for c in closes:
        rsi.update(c)
    assert rsi.value == expected


def test_ema_is_seeded_with_simple_average():
    ema = Ema(3)
    assert ema.update(1.0) is None
    assert ema.update(2.0) is None
    assert ema.update(3.0) == pytest.approx(2.0)
    assert ema.update(5.0) == pytest.approx(2.0 + 0.5 * (5.0 - 2.0))


prices = st.lists(st.floats(min_value=1, max_value=1e6, allow_nan=False), min_size=1, max_size=200)


@given(prices, st.integers(min_value=2, max_value=30))
def test_incremental_rsi_matches_batch_reference(closes, period):
    rsi = Rsi(period)
    got = [rsi.update(c) for c in closes]
    want = rsi_series(closes, period)
    assert got == pytest.approx(want, rel=1e-9, abs=1e-9)


@given(prices, st.integers(min_value=1, max_value=50))
def test_incremental_ema_matches_batch_reference(closes, period):
    ema = Ema(period)
    got = [ema.update(c) for c in closes]
    assert got == pytest.approx(ema_series(closes, period), rel=1e-9)
