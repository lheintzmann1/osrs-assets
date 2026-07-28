"""Tests for the plain-text artefacts and the round trip through them.

The property that matters: CI throws the SQLite cache away on every run, so
whatever `data/` does not carry is lost. If the live basket is not in there,
every scheduled run re-seeds the divisor and the index silently resets to
base_level twice a day -- while displaying an entirely plausible 1000.00.
"""

from __future__ import annotations

import json

import pytest
from tests.test_pipeline import DAY, basic_universe

from osrs_index.config import NavParams, UniverseParams
from osrs_index.export import (
    append_level,
    export_bars,
    export_composition,
    export_items,
    import_bars,
    import_composition,
    import_items,
    read_history,
)
from osrs_index.membership import MembershipRules
from osrs_index.models import Bar, Item
from osrs_index.pipeline import build
from osrs_index.storage import Store

NAV = NavParams(min_valid_buckets=12, window_buckets=24)
UNIVERSE = UniverseParams(min_gp_volume_24h=1_000_000_000, min_history_days=90)
RULES = MembershipRules(target_size=4, entry_rank=3, exit_rank=6, max_additions=None)


@pytest.fixture
def store(tmp_path):
    with Store(tmp_path / "a.sqlite3") as s:
        yield s


@pytest.fixture
def fresh_store(tmp_path):
    """A second, empty store -- stands in for CI's throwaway checkout."""
    with Store(tmp_path / "b.sqlite3") as s:
        yield s


# ------------------------------------------------------------- the round trip


def test_index_survives_a_thrown_away_database(store, fresh_store, tmp_path):
    """The headline property, and the bug this test was written for.

    CI runs restore -> collect -> publish on a machine with no cache. If
    `data/` does not carry units and divisor, every run is an inception, the
    level resets to 1000.00, and the chart is a sawtooth nobody questions
    because each individual value looks fine.
    """
    data = tmp_path / "data"
    spec = basic_universe(store)
    first = build(store, spec, NAV, UNIVERSE, RULES, ts=DAY * 200, review_now=True, persist=True)
    append_level(data, first.level)
    export_items(store, data)
    export_bars(store, data)
    assert export_composition(store, data, spec.index_id)

    import_items(fresh_store, data)
    import_bars(fresh_store, data)
    assert import_composition(fresh_store, data) == 1

    second = build(fresh_store, spec, NAV, UNIVERSE, RULES, ts=DAY * 201, persist=True)
    assert second.mode == "revalue", "a thrown-away cache re-seeded the index"
    assert second.divisor == pytest.approx(first.divisor)
    assert second.level.level == pytest.approx(first.level.level)


def test_composition_carries_units_not_just_membership(store, tmp_path):
    """Units *are* the index. Membership alone cannot continue one."""
    data = tmp_path / "data"
    spec = basic_universe(store)
    result = build(store, spec, NAV, UNIVERSE, RULES, ts=DAY * 200, review_now=True, persist=True)
    export_composition(store, data, spec.index_id)

    payload = json.loads((data / "composition" / "TEST.json").read_text())
    assert payload["divisor"] == pytest.approx(result.divisor)
    exported = {m["id"]: m["units"] for m in payload["members"]}
    assert exported == pytest.approx({c.item_id: c.units for c in result.constituents})


def test_export_composition_is_a_noop_without_a_basket(store, tmp_path):
    assert export_composition(store, tmp_path / "data", "NOPE") is False


# -------------------------------------------------------------------- bars


def test_bars_round_trip_exactly(store, fresh_store, tmp_path):
    data = tmp_path / "data"
    store.upsert_items([Item(1, "x", True, 100, 1000)], 0)
    original = [
        Bar(1, d * DAY, "24h", 1010, 990, 50, 60) for d in range(5)
    ] + [Bar(1, 5 * DAY, "24h", None, 990, 0, 60)]  # one-sided bucket
    store.insert_bars(original)

    export_bars(store, data)
    import_bars(fresh_store, data)
    assert fresh_store.bars(1, "24h", limit=99) == original


def test_bars_are_sharded_by_month(store, tmp_path):
    data = tmp_path / "data"
    store.insert_bars(
        [Bar(1, 1751328000, "24h", 100, 99, 5, 5), Bar(1, 1754006400, "24h", 100, 99, 5, 5)]
    )
    export_bars(store, data)
    months = sorted(p.stem for p in (data / "bars" / "24h").glob("*.ndjson"))
    assert months == ["2025-07", "2025-08"]


def test_export_is_byte_stable_across_runs(store, tmp_path):
    """An unsorted dump produces a spurious diff on every CI run."""
    data = tmp_path / "data"
    store.insert_bars([Bar(i, d * DAY, "24h", 100, 99, 5, 5) for i in (3, 1, 2) for d in range(3)])
    export_bars(store, data)
    before = (data / "bars" / "24h" / "1970-01.ndjson").read_bytes()
    export_bars(store, data)
    assert (data / "bars" / "24h" / "1970-01.ndjson").read_bytes() == before


# ----------------------------------------------------------------- history


def test_append_level_is_idempotent_on_timestamp(store, tmp_path):
    """A retried CI run must not double a day."""
    data = tmp_path / "data"
    spec = basic_universe(store)
    result = build(store, spec, NAV, UNIVERSE, RULES, ts=DAY * 200, review_now=True, persist=True)
    append_level(data, result.level)
    append_level(data, result.level)
    assert len(read_history(data, "TEST")) == 1


def test_simulated_levels_are_flagged(store, tmp_path):
    """Backtested and observed levels must stay distinguishable in the data."""
    data = tmp_path / "data"
    spec = basic_universe(store)
    result = build(store, spec, NAV, UNIVERSE, RULES, ts=DAY * 200, review_now=True, persist=True)
    append_level(data, result.level, simulated=True)
    assert read_history(data, "TEST")[0]["sim"] is True

    append_level(data, result.level, simulated=False)
    assert "sim" not in read_history(data, "TEST")[0]


def test_history_stays_sorted_when_appended_out_of_order(store, tmp_path):
    data = tmp_path / "data"
    spec = basic_universe(store)
    for ts in (DAY * 202, DAY * 200, DAY * 201):
        result = build(store, spec, NAV, UNIVERSE, RULES, ts=ts, review_now=True, persist=True)
        append_level(data, result.level)
    stamps = [record["ts"] for record in read_history(data, "TEST")]
    assert stamps == sorted(stamps)
