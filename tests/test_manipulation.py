"""Tests for the attack cost model.

The model is a set of stated assumptions, not a measurement. These tests pin
the assumptions so that changing one is a deliberate, visible act.
"""

from __future__ import annotations

import pytest

from osrs_index.config import GE_TAX_CAP_GP
from osrs_index.manipulation import (
    estimate_attack,
    ge_tax_gp,
    max_safe_aum_gp,
    weakest_link,
)

# Abyssal dagger, measured 2026-07-27: 1.229M gp, 353 units/24h, 3.70% spread,
# buy limit 8 per 4h window.
DAGGER = dict(
    item_name="Abyssal dagger",
    unit_price=1_228_612,
    daily_units=353,
    relative_spread=0.037,
    buy_limit=8,
    n_members=10,
)


def test_equal_weight_leverage_on_the_index():
    """1% on a 10-name equal-weighted index needs 10% on one constituent."""
    est = estimate_attack(**DAGGER, index_move=0.01, nav_window="24h")
    assert est.required_item_move == pytest.approx(0.10)


def test_wider_nav_window_costs_strictly_more():
    costs = [
        estimate_attack(**DAGGER, nav_window=window).total_cost_gp
        for window in ("latest", "5m", "1h", "24h")
    ]
    assert costs == sorted(costs)
    # /latest is cheap enough to be a rounding error; 24h is ~3 orders more.
    assert costs[-1] / costs[0] > 100


def test_latest_window_needs_a_single_print():
    """This is why nav.py refuses to read /latest."""
    est = estimate_attack(**DAGGER, nav_window="latest")
    assert est.units_required == 1.0
    assert est.total_cost_gp < 1_000_000


def test_24h_attack_is_still_cheap_in_absolute_terms():
    """The uncomfortable headline result.

    Even the most robust NAV window this data source supports leaves a
    ten-name index movable for tens of dollars.
    """
    est = estimate_attack(**DAGGER, nav_window="24h", index_move=0.01)
    assert 20e6 < est.total_cost_gp < 150e6
    assert est.total_cost_usd < 200


def test_breakeven_position_scales_inversely_with_index_move():
    est = estimate_attack(**DAGGER, nav_window="24h", index_move=0.01)
    assert est.breakeven_position_gp == pytest.approx(est.total_cost_gp / 0.01)


def test_buy_limits_force_multi_accounting():
    """8 per 4h = 48/day, so 353 units needs 8 accounts."""
    est = estimate_attack(**DAGGER, nav_window="24h")
    assert est.accounts_required == 8


def test_no_buy_limit_means_one_account():
    est = estimate_attack(**{**DAGGER, "buy_limit": None}, nav_window="24h")
    assert est.accounts_required == 1


def test_smaller_weight_raises_cost_proportionally():
    """What the liquidity cap actually buys you: a linear improvement.

    Halving a member's weight doubles the attack cost. When the attack costs
    55 dollars, doubling it is not a defence.
    """
    base = estimate_attack(**DAGGER, nav_window="24h", member_weight=0.10)
    capped = estimate_attack(**DAGGER, nav_window="24h", member_weight=0.05)
    assert capped.required_item_move == pytest.approx(2 * base.required_item_move)
    assert capped.total_cost_gp > base.total_cost_gp


def test_ge_tax_respects_the_per_item_cap():
    """At high unit prices the 5M cap makes the effective rate far below 2%."""
    assert ge_tax_gp(1_000_000, 1) == pytest.approx(20_000)
    assert ge_tax_gp(1_000_000_000, 1) == pytest.approx(GE_TAX_CAP_GP)


def test_ge_tax_exempts_sub_50gp_items():
    assert ge_tax_gp(49, 1000) == 0.0
    assert ge_tax_gp(50, 1000) > 0.0


def test_weakest_link_picks_the_cheapest_not_the_average():
    """An index is exactly as manipulable as its cheapest member."""
    thin = estimate_attack(**DAGGER, nav_window="24h")
    deep = estimate_attack(
        item_name="Scythe of vitur (uncharged)",
        unit_price=1_235_288_532,
        daily_units=374,
        relative_spread=0.0064,
        buy_limit=8,
        n_members=10,
        nav_window="24h",
    )
    assert weakest_link([deep, thin]) is thin
    assert deep.total_cost_gp > thin.total_cost_gp * 100


def test_max_safe_aum_is_a_fraction_of_breakeven():
    est = estimate_attack(**DAGGER, nav_window="24h")
    assert max_safe_aum_gp(est, safety_factor=10.0) == pytest.approx(
        est.breakeven_position_gp / 10.0
    )


def test_rejects_unknown_window_and_bad_inputs():
    with pytest.raises(ValueError):
        estimate_attack(**DAGGER, nav_window="3d")
    with pytest.raises(ValueError):
        estimate_attack(**{**DAGGER, "unit_price": 0}, nav_window="24h")


def test_large_required_moves_are_flagged_as_out_of_model():
    """A 1.5% capped weight implies a 65% move on the item.

    Linear cost is not credible there. The flag exists so a caller cannot
    quote the number as if it were a measurement.
    """
    est = estimate_attack(**{**DAGGER, "n_members": 10}, member_weight=0.015, nav_window="24h")
    assert est.required_item_move > 0.6
    assert est.beyond_linear_model
    assert "beyond linear model" in est.render()


def test_normal_moves_are_not_flagged():
    est = estimate_attack(**DAGGER, member_weight=0.10, nav_window="24h")
    assert est.required_item_move == pytest.approx(0.10)
    assert not est.beyond_linear_model
