"""Index construction: weights, units, divisor, continuity.

Weighting choice, and why the obvious alternatives are rejected:

  market cap    Impossible. Jagex publishes no float. Nobody knows how many
                Twisted bows exist. Any "implied market cap" built from
                price x volume or price x drop rate is a fabrication, and
                shipping one would be the single most dishonest thing this
                project could do.

  price-weighted  Mechanically absurd at OSRS price dispersion. In the melee
                basket, Scythe of vitur (1.235B gp) versus Dragon scimitar
                (59,526 gp) is a 20,700:1 ratio -- the index would be the
                Scythe with decoration. Measured over 361 days it delivered
                -7.9% at 0.92% daily vol versus -8.5% at 0.81% for equal
                weight: more volatility, no extra information.

  volume-weighted  Self-adapting to liquidity, which is genuinely appealing,
                but produces baskets ~50% concentrated in three names. That
                is a tracker wearing a basket costume.

  equal weight with a liquidity cap  What this module implements. Equal
                weight is the only defensible prior when you cannot observe
                float, and the cap stops the thinnest name from carrying a
                full 1/N of the index's manipulation surface.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass

from .models import Constituent, IndexLevel, PriceObservation, Quality

_WEIGHT_TOLERANCE = 1e-12
_MAX_WATERFALL_PASSES = 100


@dataclass(frozen=True)
class WeightResult:
    weights: dict[int, float]
    capped: frozenset[int]
    #: True when every member hit its cap, so the caps could not all be
    #: honoured and weights were simply normalised. Signals a basket whose
    #: liquidity profile is too flat for the cap rule to mean anything.
    cap_infeasible: bool


def capped_equal_weights(caps: Mapping[int, float]) -> WeightResult:
    """Equal weights, waterfall-capped, excess redistributed to uncapped names.

    Start every member at 1/N. Any member above its cap is clamped and the
    freed weight is spread equally over the members still below theirs.
    Repeat, because redistribution can push a previously-fine member over.

    If every member ends up capped the caps cannot sum to 1 and we normalise
    instead, returning cap_infeasible=True rather than silently pretending
    the constraint held.
    """
    ids = list(caps)
    if not ids:
        return WeightResult({}, frozenset(), False)

    n = len(ids)
    weights = {item_id: 1.0 / n for item_id in ids}
    free = set(ids)
    capped: set[int] = set()

    for _ in range(_MAX_WATERFALL_PASSES):
        excess = 0.0
        newly_capped = []
        for item_id in list(free):
            cap = caps[item_id]
            if weights[item_id] > cap:
                excess += weights[item_id] - cap
                weights[item_id] = cap
                newly_capped.append(item_id)
        for item_id in newly_capped:
            free.discard(item_id)
            capped.add(item_id)

        if excess <= _WEIGHT_TOLERANCE:
            break

        if not free:
            total = sum(weights.values())
            if total <= 0:  # pragma: no cover - caps are non-negative by construction
                return WeightResult({i: 1.0 / n for i in ids}, frozenset(ids), True)
            return WeightResult(
                {i: weights[i] / total for i in ids}, frozenset(capped), True
            )

        share = excess / len(free)
        for item_id in free:
            weights[item_id] += share

    total = sum(weights.values())
    if abs(total - 1.0) > 1e-9 and total > 0:
        weights = {i: w / total for i, w in weights.items()}
    return WeightResult(weights, frozenset(capped), False)


def liquidity_caps(
    gp_volumes: Mapping[int, float], max_weight_multiple: float
) -> dict[int, float]:
    """Cap each member at `k x its share of basket turnover`.

    With k = 3 an item carrying 5% of the basket's liquidity may hold at most
    15% of its weight. The rule bites exactly where it should: on the thin
    name in an otherwise deep basket, which is always the cheapest attack
    surface.
    """
    total = sum(gp_volumes.values())
    if total <= 0:
        return {item_id: 1.0 for item_id in gp_volumes}
    return {
        item_id: max_weight_multiple * (volume / total) for item_id, volume in gp_volumes.items()
    }


def units_from_weights(
    weights: Mapping[int, float], prices: Mapping[int, float], basket_value_gp: float
) -> dict[int, float]:
    """Convert target weights into held units at current prices.

    The index is carried as units, not weights. Between rebalances the units
    are constant and the weights drift with relative performance -- which is
    what an investable index actually does, and the reason a drifted weight
    is not a bug to be corrected daily.
    """
    units: dict[int, float] = {}
    for item_id, weight in weights.items():
        price = prices.get(item_id)
        if not price or price <= 0:
            continue
        units[item_id] = weight * basket_value_gp / price
    return units


def basket_value(units: Mapping[int, float], prices: Mapping[int, float]) -> float:
    return sum(u * prices[i] for i, u in units.items() if i in prices)


def level_from_units(
    units: Mapping[int, float], prices: Mapping[int, float], divisor: float
) -> float:
    if divisor <= 0:
        raise ValueError("divisor must be positive")
    return basket_value(units, prices) / divisor


def divisor_for_base(
    units: Mapping[int, float], prices: Mapping[int, float], base_level: float
) -> float:
    """Initial divisor so the index starts at its base level (conventionally 1000)."""
    value = basket_value(units, prices)
    if value <= 0:
        raise ValueError("cannot seed an index with non-positive basket value")
    return value / base_level


def rebalance(
    units: Mapping[int, float],
    prices: Mapping[int, float],
    target_weights: Mapping[int, float],
) -> dict[int, float]:
    """Reset units to target weights while preserving basket value.

    Because total value is preserved, the divisor does NOT change here. That
    is the whole point of separating rebalancing from membership changes: a
    reweighting is not an economic event for the index level, only a change
    in what it tracks going forward.
    """
    value = basket_value(units, prices)
    return units_from_weights(target_weights, prices, value)


def adjust_divisor_for_membership_change(
    divisor: float, value_before: float, value_after: float
) -> float:
    """Keep the level continuous across an add/remove.

    A member joining or leaving changes basket value for a reason that has
    nothing to do with market performance, so the divisor absorbs it:

        L = V/D must be unchanged  =>  D_new = D_old * V_after / V_before

    Every call must be recorded as a corporate action. An index whose divisor
    moves without an audit trail is not an index, it is a number someone
    types in.
    """
    if value_before <= 0:
        raise ValueError("cannot adjust divisor from a non-positive basket value")
    if value_after <= 0:
        raise ValueError("cannot adjust divisor to a non-positive basket value")
    return divisor * value_after / value_before


def compute_level(
    index_id: str,
    ts: int,
    constituents: Sequence[Constituent],
    observations: Mapping[int, PriceObservation],
    divisor: float,
) -> IndexLevel:
    """Value the basket, propagating data quality into the published level.

    Quality is not cosmetic. `n_stale_members` is persisted alongside every
    point so that anyone auditing the history can see which stretches were
    computed from carried-forward prices. An index that hides its own
    data-quality record cannot be trusted to settle anything.
    """
    value = 0.0
    stale = 0
    missing = 0

    for constituent in constituents:
        observation = observations.get(constituent.item_id)
        if observation is None or observation.value is None:
            # Skipping a member is arithmetically identical to marking it at
            # zero, so the level is NOT publishable -- it is reported with
            # quality MISSING and callers must refuse to publish it. Callers
            # that ignore the flag get an index that silently drops however
            # much weight failed to price that day.
            missing += 1
            continue
        if observation.quality is Quality.STALE:
            stale += 1
        value += constituent.units * observation.value

    if missing:
        quality = Quality.MISSING
    elif stale > len(constituents) / 4:
        quality = Quality.STALE
    elif stale:
        quality = Quality.DEGRADED
    else:
        quality = Quality.OK

    return IndexLevel(
        index_id=index_id,
        ts=ts,
        level=value / divisor if divisor > 0 else 0.0,
        divisor=divisor,
        basket_value_gp=value,
        n_members=len(constituents),
        n_stale_members=stale,
        quality=quality,
    )


def continuity_check(
    previous: IndexLevel | None, current: IndexLevel, tolerance: float = 0.05
) -> str | None:
    """Guardrail against a botched divisor adjustment.

    A rebalance or membership change must not produce a jump in the level.
    If it does, the divisor arithmetic is wrong and the historical series has
    silently broken -- the failure mode that makes backtests worthless. Run
    this on every rebalance and fail the job, loudly, rather than persisting
    a discontinuity.
    """
    if previous is None or previous.level <= 0:
        return None
    change = abs(current.level - previous.level) / previous.level
    if change > tolerance:
        return (
            f"index {current.index_id} jumped {change:.2%} between "
            f"ts={previous.ts} and ts={current.ts} (tolerance {tolerance:.0%}). "
            "Check the divisor adjustment before publishing."
        )
    return None
