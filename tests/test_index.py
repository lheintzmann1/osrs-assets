"""Tests for weighting, units and divisor continuity."""

from __future__ import annotations

import pytest

from osrs_index.index import (
    adjust_divisor_for_membership_change,
    basket_value,
    capped_equal_weights,
    compute_level,
    continuity_check,
    divisor_for_base,
    level_from_units,
    liquidity_caps,
    rebalance,
    units_from_weights,
)
from osrs_index.models import Constituent, IndexLevel, PriceObservation, Quality


def obs(item_id: int, value: float, quality: Quality = Quality.OK) -> PriceObservation:
    return PriceObservation(item_id, 0, value, quality, 24, 0, 0)


def test_equal_weights_when_caps_do_not_bind():
    result = capped_equal_weights({1: 1.0, 2: 1.0, 3: 1.0})
    for weight in result.weights.values():
        assert weight == pytest.approx(1 / 3)
    assert not result.capped
    assert not result.cap_infeasible


def test_cap_redistributes_excess_to_uncapped_members():
    result = capped_equal_weights({1: 0.10, 2: 1.0, 3: 1.0})
    assert result.weights[1] == pytest.approx(0.10)
    assert result.weights[2] == pytest.approx(0.45)
    assert result.weights[3] == pytest.approx(0.45)
    assert sum(result.weights.values()) == pytest.approx(1.0)
    assert result.capped == frozenset({1})


def test_waterfall_handles_cascading_caps():
    """Redistribution can push a previously-fine member over its own cap."""
    result = capped_equal_weights({1: 0.05, 2: 0.30, 3: 1.0, 4: 1.0})
    assert result.weights[1] == pytest.approx(0.05)
    assert result.weights[2] == pytest.approx(0.30)
    assert sum(result.weights.values()) == pytest.approx(1.0)
    assert all(result.weights[i] <= caps for i, caps in {1: 0.05, 2: 0.30}.items())


def test_cap_infeasible_is_reported_not_hidden():
    """When every cap binds, say so instead of silently breaking the rule."""
    result = capped_equal_weights({1: 0.1, 2: 0.1, 3: 0.1})
    assert result.cap_infeasible
    assert sum(result.weights.values()) == pytest.approx(1.0)


def test_liquidity_caps_scale_with_turnover_share():
    caps = liquidity_caps({1: 900.0, 2: 100.0}, max_weight_multiple=3.0)
    assert caps[1] == pytest.approx(2.7)
    assert caps[2] == pytest.approx(0.3)


def test_units_reproduce_target_weights_at_seed_prices():
    weights = {1: 0.5, 2: 0.5}
    prices = {1: 100.0, 2: 400.0}
    units = units_from_weights(weights, prices, 1000.0)
    assert units[1] == pytest.approx(5.0)
    assert units[2] == pytest.approx(1.25)
    assert basket_value(units, prices) == pytest.approx(1000.0)


def test_divisor_seeds_index_at_base_level():
    prices = {1: 100.0, 2: 400.0}
    units = units_from_weights({1: 0.5, 2: 0.5}, prices, 1000.0)
    divisor = divisor_for_base(units, prices, 1000.0)
    assert level_from_units(units, prices, divisor) == pytest.approx(1000.0)


def test_rebalance_preserves_level():
    """A reweighting is not an economic event: the level must not jump."""
    prices = {1: 100.0, 2: 400.0}
    units = units_from_weights({1: 0.5, 2: 0.5}, prices, 1000.0)
    divisor = divisor_for_base(units, prices, 1000.0)

    drifted = {1: 150.0, 2: 380.0}
    level_before = level_from_units(units, drifted, divisor)
    new_units = rebalance(units, drifted, {1: 0.5, 2: 0.5})
    level_after = level_from_units(new_units, drifted, divisor)

    assert level_after == pytest.approx(level_before)


def test_membership_change_keeps_level_continuous():
    """Adding a member must not create a phantom return."""
    prices = {1: 100.0, 2: 400.0}
    units = units_from_weights({1: 0.5, 2: 0.5}, prices, 1000.0)
    divisor = divisor_for_base(units, prices, 1000.0)
    level_before = level_from_units(units, prices, divisor)

    value_before = basket_value(units, prices)
    prices_after = {**prices, 3: 250.0}
    units_after = units_from_weights({1: 1 / 3, 2: 1 / 3, 3: 1 / 3}, prices_after, 1500.0)
    value_after = basket_value(units_after, prices_after)
    new_divisor = adjust_divisor_for_membership_change(divisor, value_before, value_after)

    level_after = level_from_units(units_after, prices_after, new_divisor)
    assert level_after == pytest.approx(level_before)


def test_divisor_adjustment_rejects_degenerate_values():
    with pytest.raises(ValueError):
        adjust_divisor_for_membership_change(1.0, 0.0, 100.0)
    with pytest.raises(ValueError):
        adjust_divisor_for_membership_change(1.0, 100.0, 0.0)


def test_compute_level_propagates_stale_members():
    constituents = [
        Constituent(1, "A", 0.5, 5.0),
        Constituent(2, "B", 0.5, 1.25),
    ]
    observations = {1: obs(1, 100.0), 2: obs(2, 400.0, Quality.STALE)}
    level = compute_level("TEST", 0, constituents, observations, divisor=1.0)
    assert level.n_stale_members == 1
    assert level.quality is Quality.STALE  # 1 of 2 exceeds the 25% threshold
    assert level.basket_value_gp == pytest.approx(1000.0)


def test_compute_level_flags_missing_constituent():
    constituents = [Constituent(1, "A", 0.5, 5.0), Constituent(2, "B", 0.5, 1.25)]
    observations = {1: obs(1, 100.0)}
    level = compute_level("TEST", 0, constituents, observations, divisor=1.0)
    assert level.quality is Quality.MISSING


def test_continuity_check_catches_a_botched_divisor():
    previous = IndexLevel("TEST", 0, 1000.0, 1.0, 1000.0, 2, 0, Quality.OK)
    current = IndexLevel("TEST", 3600, 1500.0, 1.0, 1500.0, 3, 0, Quality.OK)
    assert continuity_check(previous, current) is not None


def test_continuity_check_passes_normal_moves():
    previous = IndexLevel("TEST", 0, 1000.0, 1.0, 1000.0, 2, 0, Quality.OK)
    current = IndexLevel("TEST", 3600, 1020.0, 1.0, 1020.0, 2, 0, Quality.OK)
    assert continuity_check(previous, current) is None
