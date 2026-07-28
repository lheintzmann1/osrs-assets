"""Eligibility screening.

The screen is where the honest limits of this project become arithmetic
rather than opinion. Measured on a live snapshot (2026-07-27), the OSRS
tradeable universe thins out fast:

    24h gp volume > 1M      2489 items    median spread 11.6% at the bottom
    24h gp volume > 10M     1623 items
    24h gp volume > 100M     934 items    median spread  6.1%
    24h gp volume > 1B       392 items    median spread  1.9%
    24h gp volume > 10B      108 items    median spread  1.6%

Total 24h turnover was ~7.16T gp with the top 20 items accounting for 47.6%
of it. That concentration is the whole story: OSRS is a market of roughly a
hundred deep items and a very long unusable tail.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass

from .config import BUY_LIMIT_WINDOWS_PER_DAY, UniverseParams
from .models import Bar, Item
from .nav import gp_volume, median_spread, two_sided_ratio


@dataclass(frozen=True)
class ScreenResult:
    item_id: int
    name: str
    eligible: bool
    reasons: tuple[str, ...]
    gp_volume_24h: float
    median_spread: float | None
    two_sided_ratio: float
    window_notional_gp: float | None
    daily_creation_capacity_gp: float | None

    @property
    def rejected_for(self) -> str:
        return ", ".join(self.reasons) if self.reasons else ""


def screen_item(
    item: Item,
    bars: Sequence[Bar],
    reference_price: float | None,
    params: UniverseParams | None = None,
    history_days: float | None = None,
) -> ScreenResult:
    """Apply every inclusion criterion and report all failures, not the first.

    Reporting every reason matters for the published methodology: "rejected
    for thin volume" and "rejected for thin volume, wide spread and an
    unworkable buy limit" are different facts about an item, and collapsing
    them hides how close a borderline name is to re-entering.
    """
    params = params or UniverseParams()
    reasons: list[str] = []

    volume = gp_volume(bars)
    spread = median_spread(bars)
    two_sided = two_sided_ratio(bars)

    window_notional: float | None = None
    daily_capacity: float | None = None
    if item.buy_limit is not None and reference_price is not None:
        window_notional = item.buy_limit * reference_price
        daily_capacity = window_notional * BUY_LIMIT_WINDOWS_PER_DAY

    if volume < params.min_gp_volume_24h:
        reasons.append(f"gp_volume_24h={volume/1e9:.2f}B < {params.min_gp_volume_24h/1e9:.2f}B")

    if reference_price is None:
        reasons.append("no usable reference price")
    elif reference_price < params.min_price_gp:
        # Below ~50 gp integer rounding dominates the price signal: Pure
        # essence at 2 gp shows a -66.7% spread purely from rounding. This is
        # also the GE tax exemption threshold, so the microstructure genuinely
        # differs from the rest of the market.
        reasons.append(f"price={reference_price:.0f}gp < {params.min_price_gp}gp floor")

    if spread is None:
        reasons.append("no two-sided buckets to measure spread")
    elif spread > params.max_median_spread:
        reasons.append(f"median_spread={spread:.2%} > {params.max_median_spread:.2%}")

    if two_sided < params.min_two_sided_bucket_ratio:
        reasons.append(
            f"two_sided_ratio={two_sided:.0%} < {params.min_two_sided_bucket_ratio:.0%}"
        )

    if params.require_physical_replicability:
        # Only meaningful when screening for a fund that must actually buy the
        # basket. Rune platebody turns over 4.2B gp/day yet caps at 2.7M gp per
        # account per 4h window: liquid to trade, impossible to replicate.
        # For a read-only index that distinction is irrelevant, which is why
        # this block is opt-in. See UniverseParams.
        if item.buy_limit is None:
            reasons.append("no published buy limit")
        elif window_notional is not None and window_notional < params.min_window_notional_gp:
            reasons.append(
                f"window_notional={window_notional/1e6:.1f}M < "
                f"{params.min_window_notional_gp/1e6:.0f}M (buy limit binds)"
            )

    if history_days is not None and history_days < params.min_history_days:
        reasons.append(f"history={history_days:.0f}d < {params.min_history_days}d")

    if params.members_only is not None and item.members != params.members_only:
        reasons.append("members flag mismatch")

    if item.id in params.excluded_item_ids:
        reasons.append("explicitly excluded")

    return ScreenResult(
        item_id=item.id,
        name=item.name,
        eligible=not reasons,
        reasons=tuple(reasons),
        gp_volume_24h=volume,
        median_spread=spread,
        two_sided_ratio=two_sided,
        window_notional_gp=window_notional,
        daily_creation_capacity_gp=daily_capacity,
    )


def basket_creation_capacity_gp(results: Sequence[ScreenResult]) -> float | None:
    """Max gp of an equal-weighted basket one account can assemble per day.

    To build X gp of an N-name equal-weighted basket you need X/N of each
    name, and each name is capped at `limit x price x 6` per account per day.
    The binding constraint is therefore the *cheapest* leg, not the average:

        X <= N * min_i(daily_capacity_i)

    This is the number that kills physical replication. For the melee basket
    it works out to ~252M gp per account per day, so a 10B gp fund needs
    roughly 40 account-days of buying -- and every additional account is a
    fresh ToS surface. See docs/feasibility.md section 5.
    """
    capacities = [r.daily_creation_capacity_gp for r in results if r.daily_creation_capacity_gp]
    if not capacities or len(capacities) != len(results):
        return None
    return len(results) * min(capacities)
