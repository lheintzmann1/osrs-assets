"""Replay an index over stored daily history.

Why this exists: a freshly deployed index has one observation and a chart
with one dot on it. `/timeseries` gives 365 days of daily bars per item, so
the index can be reconstructed over that window and launch with a real
series instead of a year of waiting.

What it is not
--------------
**This is a backtest, and backtests flatter.** Every level produced here is
labelled `simulated` in the published history and rendered distinctly on the
site. Three specific reasons not to read it as a track record:

1. The candidate pool is *today's* pool. An item that only became liquid in
   May is a candidate for the whole replay, which is survivorship bias in its
   purest form -- items that died before today are simply absent from the
   list a human curated.
2. Daily bars are one VWAP per item per day. The live index values off 24
   hourly buckets with outlier handling, so the replay is smoother than the
   real thing and understates volatility.
3. Buy limits, spread and the 2% GE tax are not charged. A physically
   replicated basket would have paid ~3.5-4% per rebalance.

The replay is honest about direction and rough magnitude. It is not a
performance claim, and the code labels it so that nobody can quote it as one
by accident.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass

from .config import NavParams, UniverseParams
from .membership import MembershipRules
from .models import IndexSpec, PriceObservation, Quality
from .pipeline import SCREEN_STEP, build
from .storage import Store

log = logging.getLogger(__name__)

DAY = 86400

#: Days between membership reviews during a replay. 91 approximates the
#: quarterly schedule in the published methodology.
REVIEW_INTERVAL_DAYS = 91

#: A replay values off single daily bars, so the live NAV window and its
#: 12-bucket minimum do not apply. `nav_step` is switched to the daily step
#: as well -- leaving it on the hourly step makes every observation fall
#: through to the daily fallback, which is correct but easy to get subtly
#: wrong (see the note in pipeline.build).
REPLAY_NAV = NavParams(window_buckets=1, min_valid_buckets=1, min_units_per_bucket=1)


@dataclass
class ReplayResult:
    index_id: str
    levels: list
    reviews: int
    skipped: int
    first_ts: int | None
    last_ts: int | None


def replay(
    store: Store,
    spec: IndexSpec,
    rules: MembershipRules | None = None,
    universe_params: UniverseParams | None = None,
    warmup_days: int = 30,
    review_interval_days: int = REVIEW_INTERVAL_DAYS,
) -> ReplayResult:
    """Reconstruct the index day by day over stored daily bars.

    Wipes any existing state for this index first: a replay that appended to
    a live series would splice simulated and observed levels into one
    indistinguishable line, which is exactly the thing this module's docstring
    warns about.

    `warmup_days` skips the start of the window so the screen has enough
    history to mean anything -- without it the first reviews run against a
    one-day lookback and pick essentially at random.
    """
    rules = rules or MembershipRules()
    universe_params = universe_params or UniverseParams()

    items = store.items()
    resolved, _ = spec.resolve_ids(items)
    item_ids = list(resolved.values())
    dates = store.bar_dates(SCREEN_STEP, item_ids)
    if len(dates) <= warmup_days:
        return ReplayResult(spec.index_id, [], 0, 0, None, None)

    store.conn.execute("DELETE FROM index_member WHERE index_id = ?", (spec.index_id,))
    store.conn.execute("DELETE FROM index_value WHERE index_id = ?", (spec.index_id,))
    store.conn.execute("DELETE FROM corporate_action WHERE index_id = ?", (spec.index_id,))
    store.conn.commit()

    levels = []
    reviews = 0
    skipped = 0
    days_since_review = 0
    started = False
    #: Last usable observation per item, carried across days so an item that
    #: simply did not trade is priced stale rather than dropped.
    carried: dict[int, PriceObservation] = {}

    for as_of in dates[warmup_days:]:
        # Inception on the first usable day, then quarterly reviews.
        review_now = not started or days_since_review >= review_interval_days

        result = build(
            store,
            spec,
            nav_params=REPLAY_NAV,
            universe_params=universe_params,
            rules=rules,
            ts=as_of,
            review_now=review_now,
            persist=True,
            as_of=as_of,
            nav_step=SCREEN_STEP,
            previous_observations=carried,
        )
        carried.update(
            {i: o for i, o in result.observations.items() if o.value is not None}
        )
        if result.level is None or result.level.quality is Quality.MISSING:
            # A level with an unpriced member understates the basket by that
            # member's weight. Never publish it.
            skipped += 1
            continue

        if review_now:
            reviews += 1
            days_since_review = 0
        else:
            days_since_review += 1
        started = True
        levels.append(result.level)

    return ReplayResult(
        index_id=spec.index_id,
        levels=levels,
        reviews=reviews,
        skipped=skipped,
        first_ts=levels[0].ts if levels else None,
        last_ts=levels[-1].ts if levels else None,
    )


def summarise(result: ReplayResult) -> dict:
    """Headline statistics, for the CLI and the published payload."""
    if len(result.levels) < 2:
        return {"observations": len(result.levels)}

    values = [level.level for level in result.levels]
    returns = [values[i] / values[i - 1] - 1 for i in range(1, len(values)) if values[i - 1]]
    if not returns:
        return {"observations": len(values)}

    mean = sum(returns) / len(returns)
    variance = sum((r - mean) ** 2 for r in returns) / len(returns)
    daily_vol = variance**0.5

    peak = values[0]
    max_drawdown = 0.0
    for value in values:
        peak = max(peak, value)
        if peak:
            max_drawdown = min(max_drawdown, value / peak - 1)

    return {
        "observations": len(values),
        "total_return": values[-1] / values[0] - 1,
        "daily_vol": daily_vol,
        "annualised_vol": daily_vol * (365**0.5),
        "max_drawdown": max_drawdown,
        "reviews": result.reviews,
        "skipped_days": result.skipped,
    }
