"""End-to-end pipeline tests over a temporary store.

The property under test is the one that is invisible until you plot it: an
index must actually move. `build()` re-solving the divisor on every run
yields level = value/divisor with both recomputed from the same prices, so
the series pins to base_level forever and looks perfectly plausible in a
table.
"""

from __future__ import annotations

import pytest

from osrs_index.config import NavParams, UniverseParams
from osrs_index.membership import MembershipRules
from osrs_index.models import Bar, IndexSpec, Item, Quality
from osrs_index.pipeline import NAV_STEP, SCREEN_STEP, build
from osrs_index.storage import Store

HOUR = 3600
DAY = 86400

NAV = NavParams(min_valid_buckets=12, window_buckets=24)
UNIVERSE = UniverseParams(min_gp_volume_24h=1_000_000_000, min_history_days=90)
RULES = MembershipRules(target_size=4, entry_rank=3, exit_rank=6, max_additions=None)


def spec_for(names: list[str]) -> IndexSpec:
    return IndexSpec(
        index_id="TEST",
        name="Test Index",
        description="",
        base_level=1000.0,
        weighting="equal_weight_liquidity_capped",
        rebalance="quarterly",
        review="semiannual",
        max_weight_multiple=100.0,  # effectively uncapped, so weights stay 1/N
        candidate_item_names=tuple(names),
    )


@pytest.fixture
def store(tmp_path):
    with Store(tmp_path / "test.sqlite3") as s:
        yield s


def seed_item(store: Store, item_id: int, name: str, price: int, units_per_day: int) -> None:
    store.upsert_items(
        [Item(id=item_id, name=name, members=True, buy_limit=1000, ge_value=price)], 0
    )
    write_prices(store, item_id, price, units_per_day)


def write_prices(
    store: Store, item_id: int, price: int, units_per_day: int, days: int = 120
) -> None:
    """Enough history to clear the screen, at a flat price."""
    high, low = int(price * 1.01), int(price * 0.99)
    per_side = max(1, units_per_day // 2)
    store.insert_bars(
        [
            Bar(item_id, d * DAY, SCREEN_STEP, high, low, per_side, per_side)
            for d in range(days)
        ]
        + [
            Bar(item_id, days * DAY + h * HOUR, NAV_STEP, high, low, per_side, per_side)
            for h in range(24)
        ]
    )


def basic_universe(store: Store) -> IndexSpec:
    """Six candidates, strictly decreasing liquidity, all screen-eligible."""
    for i in range(1, 7):
        seed_item(store, i, f"item{i}", price=1_000_000, units_per_day=10_000 // i)
    return spec_for([f"item{i}" for i in range(1, 7)])


# ------------------------------------------------------------------ inception


def test_inception_starts_at_base_level(store):
    spec = basic_universe(store)
    result = build(store, spec, NAV, UNIVERSE, RULES, ts=DAY * 200, review_now=True, persist=True)
    assert result.mode == "inception"
    assert result.level is not None
    assert result.level.level == pytest.approx(1000.0)
    assert len(result.constituents) == RULES.target_size


def test_inception_picks_the_most_liquid_candidates(store):
    spec = basic_universe(store)
    result = build(store, spec, NAV, UNIVERSE, RULES, ts=DAY * 200, review_now=True, persist=True)
    assert sorted(c.name for c in result.constituents) == ["item1", "item2", "item3", "item4"]


# ------------------------------------------------------------------- revalue


def test_index_level_moves_when_a_constituent_moves(store):
    """The headline property. A flat series here means the divisor is being
    re-solved every run and the index measures nothing."""
    spec = basic_universe(store)
    first = build(store, spec, NAV, UNIVERSE, RULES, ts=DAY * 200, review_now=True, persist=True)
    weight = next(c.target_weight for c in first.constituents if c.name == "item1")

    write_prices(store, 1, price=1_100_000, units_per_day=10_000)  # +10%
    second = build(store, spec, NAV, UNIVERSE, RULES, ts=DAY * 201, persist=True)

    assert second.mode == "revalue"
    move = second.level.level / first.level.level - 1
    assert move == pytest.approx(weight * 0.10, rel=0.02)


def test_revalue_keeps_units_and_divisor_fixed(store):
    spec = basic_universe(store)
    first = build(store, spec, NAV, UNIVERSE, RULES, ts=DAY * 200, review_now=True, persist=True)
    write_prices(store, 1, price=1_500_000, units_per_day=10_000)
    second = build(store, spec, NAV, UNIVERSE, RULES, ts=DAY * 201, persist=True)

    assert second.divisor == pytest.approx(first.divisor)
    before = {c.item_id: c.units for c in first.constituents}
    after = {c.item_id: c.units for c in second.constituents}
    assert after == pytest.approx(before)


def test_revalue_does_not_change_membership(store):
    """A twice-daily job must not silently reshuffle the basket."""
    spec = basic_universe(store)
    build(store, spec, NAV, UNIVERSE, RULES, ts=DAY * 200, review_now=True, persist=True)
    # item5 becomes the most liquid name in the pool by a wide margin.
    write_prices(store, 5, price=1_000_000, units_per_day=500_000)
    result = build(store, spec, NAV, UNIVERSE, RULES, ts=DAY * 201, persist=True)

    assert result.mode == "revalue"
    assert 5 not in {c.item_id for c in result.constituents}


def test_a_flat_market_produces_a_flat_level(store):
    spec = basic_universe(store)
    first = build(store, spec, NAV, UNIVERSE, RULES, ts=DAY * 200, review_now=True, persist=True)
    second = build(store, spec, NAV, UNIVERSE, RULES, ts=DAY * 201, persist=True)
    assert second.level.level == pytest.approx(first.level.level)


# -------------------------------------------------------------------- review


def test_review_is_continuous_in_level(store):
    """A composition change is not a return. The level must not jump."""
    spec = basic_universe(store)
    build(store, spec, NAV, UNIVERSE, RULES, ts=DAY * 200, review_now=True, persist=True)

    write_prices(store, 5, price=1_000_000, units_per_day=500_000)  # item5 surges
    before = build(store, spec, NAV, UNIVERSE, RULES, ts=DAY * 201, persist=True)
    after = build(store, spec, NAV, UNIVERSE, RULES, ts=DAY * 202, review_now=True, persist=True)

    assert after.mode == "review"
    assert 5 in {c.item_id for c in after.constituents}
    assert after.level.level == pytest.approx(before.level.level, rel=1e-6)


def test_review_resets_weights_to_target(store):
    spec = basic_universe(store)
    build(store, spec, NAV, UNIVERSE, RULES, ts=DAY * 200, review_now=True, persist=True)
    write_prices(store, 1, price=3_000_000, units_per_day=10_000)  # heavy drift
    build(store, spec, NAV, UNIVERSE, RULES, ts=DAY * 201, persist=True)
    after = build(store, spec, NAV, UNIVERSE, RULES, ts=DAY * 202, review_now=True, persist=True)

    for constituent in after.constituents:
        assert constituent.target_weight == pytest.approx(1 / RULES.target_size)


def test_review_records_composition_changes_as_corporate_actions(store):
    spec = basic_universe(store)
    build(store, spec, NAV, UNIVERSE, RULES, ts=DAY * 200, review_now=True, persist=True)
    write_prices(store, 5, price=1_000_000, units_per_day=500_000)
    build(store, spec, NAV, UNIVERSE, RULES, ts=DAY * 202, review_now=True, persist=True)

    kinds = [
        row["kind"]
        for row in store.conn.execute("SELECT kind FROM corporate_action WHERE index_id='TEST'")
    ]
    assert "add" in kinds
    assert "remove" in kinds


def test_ineligible_constituent_is_forced_out_at_review(store):
    spec = basic_universe(store)
    build(store, spec, NAV, UNIVERSE, RULES, ts=DAY * 200, review_now=True, persist=True)
    # item1's volume collapses below the screen floor.
    store.conn.execute("DELETE FROM price_bar WHERE item_id = 1")
    store.conn.commit()
    write_prices(store, 1, price=1_000_000, units_per_day=2)

    result = build(store, spec, NAV, UNIVERSE, RULES, ts=DAY * 202, review_now=True, persist=True)
    assert 1 not in {c.item_id for c in result.constituents}
    assert 1 in result.membership.forced_out


# ------------------------------------------------------------------ reporting


def test_unresolved_candidate_names_are_reported_not_swallowed(store):
    basic_universe(store)
    spec = spec_for(["item1", "item2", "item3", "item4", "Nonexistent item"])
    result = build(store, spec, NAV, UNIVERSE, RULES, ts=DAY * 200, review_now=True)
    assert result.unresolved == ["Nonexistent item"]


def test_screen_rejections_are_returned_alongside_members(store):
    spec = basic_universe(store)
    result = build(store, spec, NAV, UNIVERSE, RULES, ts=DAY * 200, review_now=True)
    assert len(result.included) == RULES.target_size
    assert len(result.excluded) == 2
    assert len(result.screened) == 6


# ------------------------------------------------- the zero-valuation trap


def test_missing_constituent_price_never_publishes_a_level(store):
    """Skipping an unpriced member is arithmetically marking it at zero.

    In replay this showed up as a -100% drawdown: on any day ~10% of items
    print a crossed daily bar, those were dropped, and the basket lost that
    weight outright. The level must be flagged MISSING so callers refuse it.
    """
    spec = basic_universe(store)
    build(store, spec, NAV, UNIVERSE, RULES, ts=DAY * 200, review_now=True, persist=True)
    store.conn.execute("DELETE FROM price_bar WHERE item_id = 1")
    store.conn.commit()

    result = build(store, spec, NAV, UNIVERSE, RULES, ts=DAY * 201)
    assert result.level.quality is Quality.MISSING


def test_carried_observation_prices_an_item_that_did_not_trade(store):
    """A quiet day is stale pricing, not a 100% loss on that constituent."""
    spec = basic_universe(store)
    first = build(store, spec, NAV, UNIVERSE, RULES, ts=DAY * 200, review_now=True, persist=True)
    carried = {i: o for i, o in first.observations.items() if o.value is not None}

    store.conn.execute("DELETE FROM price_bar WHERE item_id = 1")
    store.conn.commit()

    result = build(
        store, spec, NAV, UNIVERSE, RULES, ts=DAY * 201, previous_observations=carried
    )
    assert result.level.quality is not Quality.MISSING
    assert result.level.n_stale_members >= 1
    # The level must barely move -- a carried price is not a repricing.
    assert result.level.level == pytest.approx(first.level.level, rel=0.01)


def test_crossed_daily_bar_still_yields_a_level(store):
    """A crossed bar's VWAP is real transactions; only its spread is noise."""
    spec = basic_universe(store)
    build(store, spec, NAV, UNIVERSE, RULES, ts=DAY * 200, review_now=True, persist=True)
    store.conn.execute("DELETE FROM price_bar WHERE item_id = 1")
    store.conn.commit()
    # avg_high below avg_low: instant-buy printed under an earlier instant-sell.
    store.insert_bars(
        [Bar(1, d * DAY, SCREEN_STEP, 990_000, 1_010_000, 5000, 5000) for d in range(120)]
    )

    result = build(store, spec, NAV, UNIVERSE, RULES, ts=DAY * 201, as_of=DAY * 201)
    assert result.level.quality is not Quality.MISSING


def test_as_of_does_not_leak_future_bars(store):
    """A replay that reads tomorrow's bars is not a backtest."""
    basic_universe(store)
    store.insert_bars([Bar(1, DAY * 500, SCREEN_STEP, 9_000_000, 9_000_000, 5000, 5000)])
    bars = store.bars(1, SCREEN_STEP, limit=1, as_of=DAY * 200)
    assert bars[0].ts <= DAY * 200
