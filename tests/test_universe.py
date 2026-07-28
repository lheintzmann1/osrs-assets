"""Tests for the eligibility screen."""

from __future__ import annotations

import pytest

from osrs_index.config import UniverseParams
from osrs_index.models import Bar, Item
from osrs_index.universe import basket_creation_capacity_gp, screen_item


def bars_for(price: int, units: int, n: int = 24, spread: float = 0.02) -> list[Bar]:
    high = int(price * (1 + spread / 2))
    low = int(price * (1 - spread / 2))
    return [
        Bar(item_id=1, ts=i * 86400, step="24h", avg_high=high, avg_low=low,
            vol_high=units // 2, vol_low=units // 2)
        for i in range(n)
    ]


def test_deep_item_passes():
    item = Item(id=1, name="Deep", members=True, buy_limit=8, ge_value=50_000_000)
    bars = bars_for(50_000_000, 100)
    result = screen_item(item, bars[-1:], 50_000_000, history_days=365)
    assert result.eligible, result.reasons


def test_thin_volume_is_rejected():
    item = Item(id=1, name="Thin", members=True, buy_limit=8, ge_value=1_000_000)
    result = screen_item(item, bars_for(1_000_000, 100)[-1:], 1_000_000, history_days=365)
    assert not result.eligible
    assert any("gp_volume_24h" in r for r in result.reasons)


def test_buy_limit_rejects_an_item_that_looks_liquid():
    """The Rune platebody case.

    109,201 units and 4.2B gp traded per day, but a buy limit of 70 at 38,428
    gp caps one account at 2.7M gp per 4h window. Liquid to trade, impossible
    to replicate -- and only the notional criterion catches it.
    """
    item = Item(id=1, name="Rune platebody", members=False, buy_limit=70, ge_value=38_428)
    bars = bars_for(38_428, 109_201, n=1)
    params = UniverseParams(require_physical_replicability=True)
    result = screen_item(item, bars, 38_428.0, params, history_days=365)
    assert result.gp_volume_24h > 4e9
    assert not result.eligible
    assert any("buy limit binds" in r for r in result.reasons)


def test_replicability_screen_is_off_by_default():
    """A read-only index must not reject a deep item for being cheap.

    Applying the notional floor globally rejects every constituent of the PvM
    consumables basket -- the most liquid of the four -- because consumables
    are low-priced items with high buy limits. Replicability is a question
    about a fund, not about an index.
    """
    item = Item(id=1, name="Blood rune", members=True, buy_limit=25_000, ge_value=360)
    result = screen_item(item, bars_for(360, 60_425_363, n=1), 360.0, history_days=365)
    assert result.eligible, result.reasons
    assert result.window_notional_gp is not None


def test_sub_50gp_item_is_rejected():
    """Pure essence: 2 gp, where integer rounding is the whole signal."""
    item = Item(id=1, name="Pure essence", members=True, buy_limit=30_000, ge_value=2)
    result = screen_item(item, bars_for(2, 39_522_057, n=1)[-1:], 2.0, history_days=365)
    assert not result.eligible
    assert any("floor" in r for r in result.reasons)


def test_wide_spread_is_rejected():
    item = Item(id=1, name="Wide", members=True, buy_limit=8, ge_value=50_000_000)
    bars = bars_for(50_000_000, 100, spread=0.25)
    result = screen_item(item, bars[-1:], 50_000_000, history_days=365)
    assert not result.eligible
    assert any("median_spread" in r for r in result.reasons)


def test_young_item_is_rejected():
    """Items in price discovery after a Jagex release are not index material."""
    item = Item(id=1, name="New", members=True, buy_limit=8, ge_value=50_000_000)
    result = screen_item(item, bars_for(50_000_000, 100)[-1:], 50_000_000, history_days=10)
    assert not result.eligible
    assert any("history" in r for r in result.reasons)


def test_all_failure_reasons_are_reported():
    """Reporting only the first failure hides how far an item is from passing."""
    item = Item(id=1, name="Bad", members=True, buy_limit=1, ge_value=10)
    bars = bars_for(10, 5, spread=0.4)
    params = UniverseParams(require_physical_replicability=True)
    result = screen_item(item, bars[-1:], 10.0, params, history_days=5)
    assert len(result.reasons) >= 4


def test_missing_buy_limit_is_a_rejection_not_an_assumption():
    """No published limit must never be read as 'unlimited'."""
    item = Item(id=1, name="NoLimit", members=True, buy_limit=None, ge_value=50_000_000)
    params = UniverseParams(require_physical_replicability=True)
    result = screen_item(item, bars_for(50_000_000, 100)[-1:], 50_000_000, params,
                         history_days=365)
    assert not result.eligible
    assert any("no published buy limit" in r for r in result.reasons)


def test_creation_capacity_is_set_by_the_cheapest_leg():
    """X <= N * min_i(daily capacity_i). The weakest leg binds, not the mean."""
    item_a = Item(id=1, name="A", members=True, buy_limit=8, ge_value=100_000_000)
    item_b = Item(id=2, name="B", members=True, buy_limit=70, ge_value=1_000_000)
    results = [
        screen_item(item_a, bars_for(100_000_000, 100, n=1), 100_000_000.0, history_days=365),
        screen_item(item_b, bars_for(1_000_000, 5_000, n=1), 1_000_000.0, history_days=365),
    ]
    capacity = basket_creation_capacity_gp(results)
    # min daily capacity is B: 70 * 1M * 6 = 420M; times 2 members.
    assert capacity == pytest.approx(2 * 420_000_000)


def test_creation_capacity_unknown_when_any_leg_is_unknown():
    item = Item(id=1, name="A", members=True, buy_limit=None, ge_value=None)
    result = screen_item(item, bars_for(1000, 100, n=1), 1000.0, history_days=365)
    assert basket_creation_capacity_gp([result]) is None


def test_custom_params_are_honoured():
    item = Item(id=1, name="Thin", members=True, buy_limit=8, ge_value=1_000_000)
    lenient = UniverseParams(min_gp_volume_24h=1)
    result = screen_item(item, bars_for(1_000_000, 100, n=1), 1_000_000.0, lenient,
                         history_days=365)
    assert result.eligible, result.reasons
