"""Domain types.

Deliberately dumb dataclasses. The interesting logic is in nav.py, index.py
and manipulation.py; keeping the types inert makes those testable without a
database.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from enum import StrEnum


@dataclass(frozen=True)
class Item:
    id: int
    name: str
    members: bool
    buy_limit: int | None
    ge_value: int | None = None
    highalch: int | None = None
    lowalch: int | None = None

    @property
    def window_notional_gp(self) -> int | None:
        """Max gp of this item one account can buy per 4h window, at `value`.

        Returns None when the item has no published buy limit. Callers must
        treat None as "unknown", not "unlimited".
        """
        if self.buy_limit is None or self.ge_value is None:
            return None
        return self.buy_limit * self.ge_value


@dataclass(frozen=True)
class Bar:
    """One aggregate bucket for one item.

    `avg_high` is the volume-weighted mean price of instant-BUY transactions
    in the bucket (players paying the offer). `avg_low` is the same for
    instant-SELL. Either may be None when that side did not trade.
    """

    item_id: int
    ts: int
    step: str
    avg_high: int | None
    avg_low: int | None
    vol_high: int
    vol_low: int

    @property
    def total_volume(self) -> int:
        return self.vol_high + self.vol_low

    @property
    def gp_volume(self) -> float:
        """Notional traded in the bucket. Uses each side's own price."""
        total = 0.0
        if self.avg_high is not None:
            total += self.avg_high * self.vol_high
        if self.avg_low is not None:
            total += self.avg_low * self.vol_low
        return total

    @property
    def is_two_sided(self) -> bool:
        return self.avg_high is not None and self.avg_low is not None

    @property
    def is_crossed(self) -> bool:
        """True when instant-buy printed below instant-sell.

        Not an error: the two legs are asynchronous event streams, so a falling
        market routinely produces a 'negative spread'. Observed on 15.6% of
        items in /latest, 10.1% over 24h, 5.3% over 5m.
        """
        return self.is_two_sided and self.avg_high < self.avg_low  # type: ignore[operator]

    @property
    def mid(self) -> float | None:
        if not self.is_two_sided:
            return None
        return (self.avg_high + self.avg_low) / 2  # type: ignore[operator]

    @property
    def relative_spread(self) -> float | None:
        mid = self.mid
        if mid is None or mid <= 0:
            return None
        return (self.avg_high - self.avg_low) / mid  # type: ignore[operator]

    @property
    def vwap(self) -> float | None:
        """Volume-weighted mean across both sides of the bucket.

        This is the honest point estimate of "where the item traded" -- it
        weights each side by its own participation rather than assuming the
        two legs are symmetric quotes around a mid.
        """
        if self.total_volume == 0:
            return None
        numerator = 0.0
        denominator = 0
        if self.avg_high is not None:
            numerator += self.avg_high * self.vol_high
            denominator += self.vol_high
        if self.avg_low is not None:
            numerator += self.avg_low * self.vol_low
            denominator += self.vol_low
        if denominator == 0:
            return None
        return numerator / denominator


class Quality(StrEnum):
    """How much the NAV observation should be trusted."""

    OK = "ok"
    #: Enough buckets survived, but some were winsorised or dropped.
    DEGRADED = "degraded"
    #: Too few valid buckets. Previous value carried forward.
    STALE = "stale"
    #: No usable data at all.
    MISSING = "missing"


@dataclass(frozen=True)
class PriceObservation:
    item_id: int
    ts: int
    value: float | None
    quality: Quality
    buckets_used: int
    buckets_rejected: int
    buckets_winsorised: int

    @property
    def usable(self) -> bool:
        return self.value is not None and self.quality in (Quality.OK, Quality.DEGRADED)


@dataclass(frozen=True)
class Constituent:
    item_id: int
    name: str
    target_weight: float
    units: float
    capped: bool = False


@dataclass(frozen=True)
class IndexLevel:
    index_id: str
    ts: int
    level: float
    divisor: float
    basket_value_gp: float
    n_members: int
    n_stale_members: int
    quality: Quality


@dataclass(frozen=True)
class IndexSpec:
    index_id: str
    name: str
    description: str
    base_level: float
    weighting: str
    rebalance: str
    review: str
    max_weight_multiple: float
    #: Curated candidate pool, deliberately larger than the target basket
    #: size. Defines what the basket is ABOUT; membership.py decides what it
    #: HOLDS. See membership.py for why curation cannot be derived from data.
    candidate_item_names: tuple[str, ...]

    def resolve_ids(self, items: Iterable[Item]) -> tuple[dict[str, int], list[str]]:
        """Map seed names to item ids, reporting anything that did not match.

        Name-based specs are intentional: item ids are opaque and reviewers
        cannot sanity-check them, whereas a wrong name is obvious on sight.
        The cost is that a Jagex rename breaks resolution loudly, which is the
        correct failure mode.
        """
        by_name = {item.name.lower(): item.id for item in items}
        resolved: dict[str, int] = {}
        missing: list[str] = []
        for name in self.candidate_item_names:
            found = by_name.get(name.lower())
            if found is None:
                missing.append(name)
            else:
                resolved[name] = found
        return resolved, missing
