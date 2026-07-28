"""Wiring: spec -> screen -> weights -> units -> level.

This is the only module allowed to know about all the others. Keeping the
orchestration in one place means the index definition can be read end to end
by someone auditing it, which is the entire premise of publishing an index.
"""

from __future__ import annotations

import json
import logging
import time
from dataclasses import dataclass
from dataclasses import field as dc_field
from pathlib import Path

from .config import NavParams, UniverseParams
from .index import (
    capped_equal_weights,
    compute_level,
    divisor_for_base,
    liquidity_caps,
    units_from_weights,
)
from .manipulation import estimate_attack, max_safe_aum_gp, weakest_link
from .membership import MembershipDecision, MembershipRules, rank_candidates
from .membership import review as review_membership
from .membership import seed as seed_membership
from .models import Constituent, IndexLevel, IndexSpec, PriceObservation
from .nav import gp_volume, median_spread, observe
from .storage import Store
from .universe import ScreenResult, basket_creation_capacity_gp, screen_item

log = logging.getLogger(__name__)

#: 24 hourly buckets is the NAV window; 24 daily buckets is the screening
#: window. Both are held here so the numbers in a report are traceable.
NAV_STEP = "1h"
SCREEN_STEP = "24h"
SCREEN_LOOKBACK_DAYS = 30


def load_spec(path: Path) -> IndexSpec:
    payload = json.loads(path.read_text())
    return IndexSpec(
        index_id=payload["index_id"],
        name=payload["name"],
        description=payload["description"],
        base_level=float(payload.get("base_level", 1000.0)),
        weighting=payload.get("weighting", "equal_weight_liquidity_capped"),
        rebalance=payload.get("rebalance", "quarterly"),
        review=payload.get("review", "semiannual"),
        max_weight_multiple=float(payload.get("max_weight_multiple", 3.0)),
        candidate_item_names=tuple(payload["candidate_item_names"]),
    )


def load_rules(path: Path) -> MembershipRules:
    payload = json.loads(path.read_text()).get("membership", {})
    return MembershipRules(**payload) if payload else MembershipRules()


def load_specs(spec_dir: Path) -> list[IndexSpec]:
    return [load_spec(p) for p in sorted(spec_dir.glob("*.json"))]


@dataclass
class BuildResult:
    spec: IndexSpec
    screened: list[ScreenResult]
    included: list[ScreenResult]
    excluded: list[ScreenResult]
    unresolved: list[str]
    constituents: list[Constituent]
    divisor: float
    level: IndexLevel | None
    creation_capacity_gp: float | None
    cheapest_attack: object | None
    max_safe_aum_gp: float | None
    cap_infeasible: bool
    membership: MembershipDecision | None = None
    #: "inception" | "revalue" | "review" -- see build() for why these differ.
    mode: str = "revalue"
    observations: dict[int, PriceObservation] = dc_field(default_factory=dict)


def build(
    store: Store,
    spec: IndexSpec,
    nav_params: NavParams | None = None,
    universe_params: UniverseParams | None = None,
    rules: MembershipRules | None = None,
    ts: int | None = None,
    review_now: bool = False,
    persist: bool = False,
    as_of: int | None = None,
    nav_step: str = NAV_STEP,
    previous_observations: dict[int, PriceObservation] | None = None,
) -> BuildResult:
    """Screen the candidate pool, pick the basket, weight it, and value it.

    Deliberately returns the rejects alongside the members. A methodology
    that only shows you what made the cut is unfalsifiable; the interesting
    question about any index is always what it left out and why.

    Membership is decided against whatever the store already holds, so a
    review is a function of (candidate pool, market data, previous basket).
    That last term is what makes the buffers work, and it is also why the
    published history has to record every composition change with a reason.
    """
    nav_params = nav_params or NavParams()
    universe_params = universe_params or UniverseParams()
    ts = ts or int(time.time())

    items = store.items()
    resolved, unresolved = spec.resolve_ids(items)
    if unresolved:
        log.warning("%s: unresolved seed names: %s", spec.index_id, ", ".join(unresolved))

    by_id = {item.id: item for item in items}

    screened: list[ScreenResult] = []
    observations: dict[int, PriceObservation] = {}

    for item_id in resolved.values():
        item = by_id[item_id]
        nav_bars = store.bars(
            item_id, nav_step, limit=nav_params.window_buckets, as_of=as_of
        )
        screen_bars = store.bars(
            item_id, SCREEN_STEP, limit=SCREEN_LOOKBACK_DAYS, as_of=as_of
        )

        prior = (previous_observations or {}).get(item_id)
        observation = observe(item_id, ts, nav_bars, nav_params, previous=prior)
        # Fall back to the MOST RECENT daily bar when hourly history is not
        # available (a fresh clone, or a historical replay).
        #
        # It must be one bar, not the whole lookback. Averaging the 30-day
        # window here silently turns the index into a 30-day moving average:
        # the levels still look plausible, but measured annualised volatility
        # collapses (5.6% against 16% for the same melee basket) because the
        # series is smoothed rather than priced. Caught by comparing a replay
        # against the direct timeseries calculation.
        reference = observation.value
        if reference is None and screen_bars:
            # Walk back to the most recent bar that carries a usable level.
            #
            # Crossed bars are accepted here, unlike in the main NAV path. A
            # crossed bucket's VWAP is still a volume-weighted average of real
            # transactions -- it is the *spread* that is meaningless, not the
            # level. Rejecting them at single-bar granularity drops ~10% of
            # constituents on any given day, and a dropped constituent is
            # valued at zero by compute_level, which craters the index. That
            # showed up as a -100% drawdown in replay.
            lenient = NavParams(
                window_buckets=1,
                min_valid_buckets=1,
                min_units_per_bucket=1,
                drop_crossed_buckets=False,
            )
            for bar in reversed(screen_bars):
                daily = observe(item_id, ts, [bar], lenient, previous=prior)
                if daily.value is not None:
                    reference = daily.value
                    observation = daily
                    break
        observations[item_id] = observation

        # Screening uses a single representative day so the volume threshold
        # keeps its documented meaning (gp per 24h), while spread and
        # two-sidedness are measured over the full lookback.
        recent = screen_bars[-1:] if screen_bars else []
        result = screen_item(
            item,
            recent,
            reference,
            universe_params,
            history_days=store.history_days(item_id, SCREEN_STEP, as_of=as_of),
        )
        # Recompute the distribution-based criteria over the whole lookback.
        result = ScreenResult(
            item_id=result.item_id,
            name=result.name,
            eligible=result.eligible,
            reasons=result.reasons,
            gp_volume_24h=gp_volume(recent),
            median_spread=median_spread(screen_bars),
            two_sided_ratio=result.two_sided_ratio,
            window_notional_gp=result.window_notional_gp,
            daily_creation_capacity_gp=result.daily_creation_capacity_gp,
        )
        screened.append(result)

    # ---------------------------------------------------------------- mode
    #
    # Three distinct operations, conflated at your peril:
    #
    #   inception  no prior basket. Seed units and solve the divisor so the
    #              index starts at base_level.
    #   revalue    the common case, and what the twice-daily job runs. Units
    #              and divisor are FIXED; only prices move. This is the only
    #              mode that produces an index level worth charting.
    #   review     scheduled: re-run membership and reset weights, preserving
    #              basket value so the level stays continuous.
    #
    # Re-seeding the divisor on every run is the bug that turns an index into
    # a flat line at base_level, because level = value/divisor and both are
    # recomputed from the same prices. It is easy to write and invisible
    # until you plot it.
    rules = rules or MembershipRules()
    stored = store.current_members(spec.index_id)
    previous_members = [row["item_id"] for row in stored]
    stored_units = {row["item_id"]: row["units"] for row in stored}
    stored_weights = {row["item_id"]: row["target_weight"] for row in stored}
    stored_capped = {row["item_id"]: bool(row["was_capped"]) for row in stored}
    last_levels = store.levels(spec.index_id, limit=1)
    stored_divisor = last_levels[0]["divisor"] if last_levels else None

    if not previous_members:
        mode = "inception"
        decision = seed_membership(screened, rules)
    elif review_now:
        mode = "review"
        decision = review_membership(screened, previous_members, rules)
    else:
        mode = "revalue"
        decision = MembershipDecision(
            ranked=rank_candidates(screened),
            members=previous_members,
            retained=previous_members,
        )

    chosen = set(decision.members)
    included = [r for r in screened if r.item_id in chosen]
    excluded = [r for r in screened if r.item_id not in chosen]

    if not included:
        return BuildResult(
            spec, screened, included, excluded, unresolved, [], 1.0, None, None, None, None,
            False, decision, mode, observations,
        )

    prices = {
        r.item_id: observations[r.item_id].value
        for r in included
        if observations[r.item_id].value
    }

    name_by_id = {r.item_id: r.name for r in screened}
    cap_infeasible = False

    if mode == "revalue":
        # Units and divisor carry over untouched. Weights drift with relative
        # performance, which is what an investable index actually does.
        units = {i: stored_units[i] for i in decision.members if i in stored_units}
        divisor = stored_divisor or divisor_for_base(units, prices, spec.base_level)
        weights = stored_weights
        capped_ids = {i for i, was in stored_capped.items() if was}
    else:
        volumes = {r.item_id: r.gp_volume_24h for r in included}
        caps = liquidity_caps(volumes, spec.max_weight_multiple)
        weight_result = capped_equal_weights(caps)
        cap_infeasible = weight_result.cap_infeasible
        if cap_infeasible:
            log.warning(
                "%s: liquidity caps could not all be honoured; weights normalised. "
                "The basket's turnover profile is too flat for the cap rule to bind.",
                spec.index_id,
            )
        weights = weight_result.weights
        capped_ids = set(weight_result.capped)

        if mode == "inception":
            units = units_from_weights(weights, prices, spec.base_level * 1000.0)
            divisor = divisor_for_base(units, prices, spec.base_level)
        else:
            # Preserve basket value across the review so the published level
            # is continuous. Members leaving the basket are valued at today's
            # price before they go, so the reweighting is not itself a return.
            all_prices = {
                r.item_id: observations[r.item_id].value
                for r in screened
                if observations[r.item_id].value
            }
            value_before = sum(
                units_held * all_prices[item_id]
                for item_id, units_held in stored_units.items()
                if item_id in all_prices
            )
            if value_before <= 0:
                raise ValueError(
                    f"{spec.index_id}: cannot review from a non-positive basket value; "
                    "prior constituents have no usable prices"
                )
            units = units_from_weights(weights, prices, value_before)
            divisor = stored_divisor or divisor_for_base(units, prices, spec.base_level)

    constituents = [
        Constituent(
            item_id=item_id,
            name=name_by_id.get(item_id, str(item_id)),
            target_weight=weights.get(item_id, 0.0),
            units=unit_count,
            capped=item_id in capped_ids,
        )
        for item_id, unit_count in units.items()
    ]

    level = compute_level(spec.index_id, ts, constituents, observations, divisor)

    capacity = basket_creation_capacity_gp(included)

    estimates = []
    for constituent in constituents:
        result = next(r for r in included if r.item_id == constituent.item_id)
        price = prices.get(constituent.item_id)
        bars = store.bars(constituent.item_id, SCREEN_STEP, limit=1, as_of=as_of)
        daily_units = bars[0].total_volume if bars else 0
        if not price or daily_units <= 0:
            continue
        estimates.append(
            estimate_attack(
                item_name=constituent.name,
                unit_price=price,
                daily_units=daily_units,
                relative_spread=result.median_spread or 0.0,
                buy_limit=by_id[constituent.item_id].buy_limit,
                n_members=len(constituents),
                member_weight=constituent.target_weight,
            )
        )
    cheapest = weakest_link(estimates)
    safe_aum = max_safe_aum_gp(cheapest) if cheapest else None

    if persist:
        store.upsert_index_def(
            spec.index_id,
            spec.name,
            spec.description,
            spec.weighting,
            spec.rebalance,
            spec.base_level,
            ts,
        )
        if mode != "revalue":
            store.set_members(
                spec.index_id,
                ts,
                [(c.item_id, c.target_weight, c.units, c.capped) for c in constituents],
            )
        store.insert_level(level)
        for item_id in decision.added:
            store.record_action(
                spec.index_id, ts, "add", decision.rationale.get(item_id, ""), item_id=item_id
            )
        for item_id in decision.removed:
            store.record_action(
                spec.index_id, ts, "remove", decision.rationale.get(item_id, ""), item_id=item_id
            )
        if not decision.changed:
            store.record_action(
                spec.index_id, ts, "review",
                f"no change; {len(constituents)} members held", divisor_after=divisor,
            )

    return BuildResult(
        spec=spec,
        screened=screened,
        included=included,
        excluded=excluded,
        unresolved=unresolved,
        constituents=constituents,
        divisor=divisor,
        level=level,
        creation_capacity_gp=capacity,
        cheapest_attack=cheapest,
        max_safe_aum_gp=safe_aum,
        cap_infeasible=cap_infeasible,
        membership=decision,
        mode=mode,
        observations=observations,
    )
