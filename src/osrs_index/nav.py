"""Turning a noisy trade feed into a number you can settle on.

This module exists because of one fact: the OSRS real-time prices API does
not publish a price. It publishes two independent streams of transaction
prints -- the last instant-buy and the last instant-sell -- with independent
timestamps, arbitrary staleness, and no guarantee of ordering between them.

Anything that treats `(high + low) / 2` from /latest as a valuation is
wrong, and measurably so:

  * median staleness of the older leg: 37 minutes
  * p90: 14.8 hours, p99: 38.2 hours
  * high < low on 15.6% of items
  * only 718 of 4591 items had both sides trade in the last 5m bucket

The estimator below is built to be as expensive to move as this data allows.
It is still not very expensive -- see docs/feasibility.md section 4 for the
attack costs it does not defend against.
"""

from __future__ import annotations

import statistics
from collections.abc import Sequence

from .config import NavParams
from .models import Bar, PriceObservation, Quality

#: Scale factor converting median-absolute-deviation to a standard-deviation
#: equivalent for a normal distribution. We use MAD rather than stdev because
#: a single manipulated bucket inflates stdev enough to protect itself from
#: being flagged as an outlier -- the exact failure the filter is meant to
#: prevent.
MAD_TO_SIGMA = 1.4826

#: Minimum sigma, as a fraction of the median, used when MAD collapses to
#: zero. That happens whenever more than half the buckets share one price --
#: common for cheap items whose price is pinned by integer rounding.
#:
#: There must be no stdev fallback here. A single 100x print pushes stdev high
#: enough that a 3-sigma band admits the print itself, so the filter would
#: protect exactly what it exists to reject. A relative floor cannot be
#: widened by the outlier, which is the whole point.
#:
#: At 0.5% the 3-sigma acceptance band is +/-1.5% around the median. It only
#: binds on a genuinely flat market, where a large deviation really is an
#: outlier.
RELATIVE_SIGMA_FLOOR = 0.005


def _robust_sigma(values: Sequence[float]) -> float:
    if len(values) < 2:
        return 0.0
    median = statistics.median(values)
    mad = statistics.median([abs(v - median) for v in values])
    sigma = MAD_TO_SIGMA * mad
    return max(sigma, RELATIVE_SIGMA_FLOOR * abs(median))


def observe(
    item_id: int,
    ts: int,
    bars: Sequence[Bar],
    params: NavParams | None = None,
    previous: PriceObservation | None = None,
) -> PriceObservation:
    """Estimate one item's price from a window of aggregate buckets.

    The pipeline, in order:

    1. Keep only buckets with at least `min_units_per_bucket` units traded.
       A bucket built from one trade is one player's opinion, and for an
       illiquid item that player can be the attacker.
    2. Drop crossed buckets when configured. A crossed bucket carries no
       reliable level information, only the fact that the two legs were
       sampled at different moments.
    3. Winsorise, do not trim. Clamping outliers to the +/- N-sigma boundary
       keeps the sample size fixed; dropping them would let an attacker
       shrink the denominator and amplify their remaining influence.
    4. Volume-weight the survivors. Time-weighting would treat a dead 3am
       bucket as equal to a busy one.

    Returns a PriceObservation whose `quality` says how much to trust it.
    Callers must branch on `quality` -- an estimator that silently returns a
    number for an item that has not traded in two days is how indices lie.
    """
    params = params or NavParams()

    kept: list[Bar] = []
    rejected = 0
    for bar in bars[-params.window_buckets :]:
        if bar.total_volume < params.min_units_per_bucket:
            rejected += 1
            continue
        if params.drop_crossed_buckets and bar.is_crossed:
            rejected += 1
            continue
        if bar.vwap is None or bar.vwap <= 0:
            rejected += 1
            continue
        kept.append(bar)

    if len(kept) < params.min_valid_buckets:
        # Not enough signal to publish a fresh number. Carry the previous
        # value if we have one, but never launder it as fresh.
        if previous is not None and previous.value is not None:
            return PriceObservation(
                item_id=item_id,
                ts=ts,
                value=previous.value,
                quality=Quality.STALE,
                buckets_used=len(kept),
                buckets_rejected=rejected,
                buckets_winsorised=0,
            )
        return PriceObservation(
            item_id=item_id,
            ts=ts,
            value=None,
            quality=Quality.MISSING,
            buckets_used=len(kept),
            buckets_rejected=rejected,
            buckets_winsorised=0,
        )

    prices = [bar.vwap for bar in kept]  # type: ignore[misc]
    median = statistics.median(prices)
    sigma = _robust_sigma(prices)

    winsorised = 0
    numerator = 0.0
    denominator = 0
    if sigma > 0:
        lower = median - params.winsor_sigma * sigma
        upper = median + params.winsor_sigma * sigma
    else:
        lower = upper = None

    for bar in kept:
        price = bar.vwap
        assert price is not None
        if lower is not None and upper is not None:
            if price < lower:
                price, winsorised = lower, winsorised + 1
            elif price > upper:
                price, winsorised = upper, winsorised + 1
        weight = bar.total_volume
        numerator += price * weight
        denominator += weight

    if denominator == 0:  # pragma: no cover - guarded by min_units filter
        return PriceObservation(item_id, ts, None, Quality.MISSING, 0, rejected, 0)

    quality = Quality.OK if (rejected == 0 and winsorised == 0) else Quality.DEGRADED
    return PriceObservation(
        item_id=item_id,
        ts=ts,
        value=numerator / denominator,
        quality=quality,
        buckets_used=len(kept),
        buckets_rejected=rejected,
        buckets_winsorised=winsorised,
    )


def median_spread(bars: Sequence[Bar]) -> float | None:
    """Median relative spread across two-sided, uncrossed buckets.

    Used by the eligibility screen. Crossed buckets are excluded rather than
    counted as negative spread, because a negative spread is a sampling
    artefact and averaging it in would make illiquid items look tighter than
    liquid ones.
    """
    spreads = [
        bar.relative_spread
        for bar in bars
        if bar.is_two_sided and not bar.is_crossed and bar.relative_spread is not None
    ]
    if not spreads:
        return None
    return statistics.median(spreads)


def two_sided_ratio(bars: Sequence[Bar]) -> float:
    """Share of buckets in which both sides of the market printed."""
    if not bars:
        return 0.0
    return sum(1 for bar in bars if bar.is_two_sided) / len(bars)


def gp_volume(bars: Sequence[Bar]) -> float:
    return sum(bar.gp_volume for bar in bars)
