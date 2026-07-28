"""Tests for the NAV estimator.

These are the tests that matter most in the repo: every one of them encodes
a property that, if it broke silently, would produce an index that looks
fine and settles wrong.
"""

from __future__ import annotations

import pytest

from osrs_index.config import NavParams
from osrs_index.models import Bar, PriceObservation, Quality
from osrs_index.nav import median_spread, observe, two_sided_ratio


def bar(ts: int, high: int | None, low: int | None, vh: int = 50, vl: int = 50) -> Bar:
    return Bar(item_id=1, ts=ts, step="1h", avg_high=high, avg_low=low, vol_high=vh, vol_low=vl)


def steady(n: int = 24, price: int = 1000) -> list[Bar]:
    return [bar(i * 3600, price + 10, price - 10) for i in range(n)]


def test_observe_returns_volume_weighted_price():
    result = observe(1, 0, steady())
    assert result.quality is Quality.OK
    assert result.value == pytest.approx(1000.0)
    assert result.buckets_used == 24


def test_thin_buckets_are_rejected():
    """A bucket built from one trade is one player's opinion, not a price."""
    bars = steady(23) + [bar(23 * 3600, 5000, 5000, vh=1, vl=0)]
    result = observe(1, 0, bars, NavParams(min_units_per_bucket=5))
    assert result.buckets_rejected == 1
    assert result.value == pytest.approx(1000.0)


def test_crossed_buckets_are_dropped():
    """Crossed prints carry no level information, only sampling skew."""
    bars = steady(20) + [bar(i * 3600, 900, 1100) for i in range(20, 24)]
    result = observe(1, 0, bars)
    assert result.buckets_rejected == 4
    assert result.quality is Quality.DEGRADED


def test_outlier_is_winsorised_not_dropped():
    """Clamping keeps the sample size fixed.

    Dropping outliers would let an attacker shrink the denominator and
    amplify the influence of whatever they left behind.
    """
    bars = steady(23) + [bar(23 * 3600, 10_000, 9_800)]
    result = observe(1, 0, bars)
    assert result.buckets_winsorised == 1
    assert result.buckets_used == 24
    # The clamped value still pulls the mean, but nowhere near the raw 9900.
    assert 1000 < result.value < 1400


def test_single_huge_print_cannot_dominate_the_window():
    """The headline property: one trade must not set the NAV.

    Against /latest this exact input would move the reported price by 900%.
    """
    bars = steady(23) + [bar(23 * 3600, 100_000, 100_000, vh=10_000, vl=0)]
    result = observe(1, 0, bars)
    assert result.value < 2000, "a single high-volume print dominated the NAV"


def test_insufficient_buckets_carries_previous_and_flags_stale():
    previous = PriceObservation(1, 0, 1234.0, Quality.OK, 24, 0, 0)
    result = observe(1, 3600, steady(3), NavParams(min_valid_buckets=12), previous=previous)
    assert result.quality is Quality.STALE
    assert result.value == 1234.0


def test_missing_when_no_data_and_no_history():
    result = observe(1, 0, [], NavParams())
    assert result.quality is Quality.MISSING
    assert result.value is None


def test_never_reports_ok_when_data_was_touched():
    """Quality must never overstate confidence."""
    bars = steady(23) + [bar(23 * 3600, 1010, 990, vh=1, vl=0)]
    result = observe(1, 0, bars)
    assert result.quality is not Quality.OK


def test_median_spread_excludes_crossed_buckets():
    """Averaging in negative spreads would make thin items look tight."""
    bars = [bar(0, 1100, 900), bar(3600, 900, 1100), bar(7200, 1100, 900)]
    assert median_spread(bars) == pytest.approx(0.2)


def test_two_sided_ratio():
    bars = [bar(0, 1010, 990), bar(3600, 1010, None), bar(7200, None, 990), bar(10800, 1010, 990)]
    assert two_sided_ratio(bars) == pytest.approx(0.5)


def test_one_sided_bucket_still_contributes_its_side():
    """A bucket where only instant-buys printed is information, not noise."""
    result = observe(1, 0, [bar(i * 3600, 1000, None, vh=50, vl=0) for i in range(24)])
    assert result.value == pytest.approx(1000.0)


def test_mad_resists_a_stdev_inflating_outlier():
    """An outlier large enough to inflate stdev must still be caught.

    Using stdev here would let the outlier widen the acceptance band far
    enough to admit itself -- the filter would protect exactly what it is
    supposed to reject.
    """
    bars = steady(23) + [bar(23 * 3600, 1_000_000, 1_000_000)]
    result = observe(1, 0, bars)
    assert result.buckets_winsorised == 1
    assert result.value < 5000
