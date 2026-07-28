"""Tests for rank-based membership with hysteresis.

The property that matters most is the one in
`test_item_oscillating_in_the_buffer_never_trades`: the buffer exists to stop
turnover, and turnover is what makes the index expensive.
"""

from __future__ import annotations

import pytest

from osrs_index.membership import (
    MembershipRules,
    rank_candidates,
    review,
    seed,
)
from osrs_index.universe import ScreenResult


def candidate(
    item_id: int, volume: float, eligible: bool = True, reasons: tuple[str, ...] = ()
) -> ScreenResult:
    return ScreenResult(
        item_id=item_id,
        name=f"item{item_id}",
        eligible=eligible,
        reasons=reasons if not eligible else (),
        gp_volume_24h=volume,
        median_spread=0.02,
        two_sided_ratio=1.0,
        window_notional_gp=50e6,
        daily_creation_capacity_gp=300e6,
    )


def pool(n: int, top_volume: float = 100e9) -> list[ScreenResult]:
    """n candidates with strictly decreasing liquidity."""
    return [candidate(i, top_volume / i) for i in range(1, n + 1)]


RULES = MembershipRules(target_size=10, entry_rank=8, exit_rank=12)


# --------------------------------------------------------------- rule sanity


def test_rules_reject_an_inverted_buffer():
    with pytest.raises(ValueError):
        MembershipRules(target_size=10, entry_rank=12, exit_rank=8)


def test_rules_reject_entry_rank_above_target():
    with pytest.raises(ValueError):
        MembershipRules(target_size=10, entry_rank=11, exit_rank=12)


def test_equal_thresholds_are_allowed_but_degenerate_to_top_n():
    """Documented, not prevented: entry == exit == target is naive top-N."""
    rules = MembershipRules(target_size=10, entry_rank=10, exit_rank=10)
    assert rules.entry_rank == rules.exit_rank


# ------------------------------------------------------------------- ranking


def test_ranking_is_by_liquidity_descending():
    ranked = rank_candidates(pool(5))
    assert [c.item_id for c in ranked] == [1, 2, 3, 4, 5]
    assert [c.rank for c in ranked] == [1, 2, 3, 4, 5]


def test_ineligible_candidates_rank_last_regardless_of_liquidity():
    screened = [candidate(1, 1e9), candidate(2, 500e9, eligible=False, reasons=("thin",))]
    ranked = rank_candidates(screened)
    assert [c.item_id for c in ranked] == [1, 2]


def test_ties_break_deterministically_on_item_id():
    """Without a total order the same inputs can yield different baskets."""
    screened = [candidate(7, 1e9), candidate(3, 1e9), candidate(5, 1e9)]
    assert [c.item_id for c in rank_candidates(screened)] == [3, 5, 7]
    assert rank_candidates(screened) == rank_candidates(list(reversed(screened)))


# ---------------------------------------------------------------- inception


def test_seed_takes_the_top_n():
    decision = seed(pool(20), RULES)
    assert decision.members == list(range(1, 11))
    assert decision.added == decision.members


def test_seed_ignores_entry_buffer():
    """At inception there is no prior basket to be hysteretic about."""
    decision = seed(pool(20), RULES)
    assert len(decision.members) == 10  # not capped at entry_rank=8


def test_seed_flags_an_undersized_pool():
    decision = seed(pool(6), RULES)
    assert decision.undersized
    assert len(decision.members) == 6


# ----------------------------------------------------- the buffer's whole job


def test_item_oscillating_in_the_buffer_never_trades():
    """The headline property.

    An incumbent drifting between rank 9 and 12, and a challenger drifting
    between 9 and 12, must both stay exactly where they are. Every trade
    avoided here is ~3.5-4% of round-trip friction not paid.
    """
    members = list(range(1, 11))
    for rank in (9, 10, 11, 12):
        screened = pool(20)
        # Force incumbent 10 to sit at `rank` by rewriting liquidity ordering.
        screened[9] = candidate(10, 100e9 / rank)
        screened[rank - 1] = candidate(rank, 100e9 / 10)
        decision = review(screened, members, RULES)
        assert not decision.changed, f"basket churned at rank {rank}"


def test_incumbent_leaves_only_past_the_exit_buffer():
    screened = pool(20)
    screened[9] = candidate(10, 100e9 / 13.5)  # incumbent falls past rank 13
    decision = review(screened, list(range(1, 11)), RULES)
    assert 10 in decision.removed
    assert decision.changed


def test_challenger_enters_only_inside_the_entry_buffer():
    """Rank 9 is good enough to keep a seat, not good enough to take one."""
    members = list(range(1, 10)) + [11]  # 9 members plus a straggler
    screened = pool(20)
    decision = review(screened, members, RULES)
    # Item 10 sits at rank 10, outside entry_rank=8, so the slot stays open.
    assert 10 not in decision.members
    assert len(decision.members) == 10


def test_a_mandatory_tier_challenger_displaces_the_worst_incumbent():
    """Nothing outranks the top of the pool.

    An eligible item inside `entry_rank` is tier 0 and enters even when the
    basket is full -- the buffer protects incumbents from *equals*, not from
    the best names in the universe.
    """
    members = list(range(2, 12))  # item 1 (rank 1) is not a member
    decision = review(pool(20), members, RULES)
    assert 1 in decision.added
    assert 1 in decision.members
    assert 11 in decision.removed  # worst incumbent gives up the seat


def test_vacancies_are_filled_from_the_buffer_zone():
    """The bug this module shipped with, pinned.

    With entry_rank=8 and ranks 1-8 already held, a hard entry gate means no
    eligible non-member can ever fill a vacancy at rank 9, so the basket
    shrinks monotonically forever. The reserve tier must fill it.
    """
    members = list(range(1, 11))
    screened = pool(20)
    screened[9] = candidate(10, 1.0, eligible=False, reasons=("delisted",))
    decision = review(screened, members, RULES)
    assert 10 in decision.forced_out
    assert len(decision.members) == 10, "vacancy was not filled from the buffer zone"
    assert 11 in decision.members


# --------------------------------------------------------------- forced exits


def test_ineligible_member_is_forced_out_regardless_of_rank():
    screened = pool(20)
    screened[0] = candidate(1, 100e9, eligible=False, reasons=("delisted",))
    decision = review(screened, list(range(1, 11)), RULES)
    assert 1 in decision.forced_out
    assert 1 not in decision.members
    assert "delisted" in decision.rationale[1]


def test_forced_exits_bypass_the_turnover_cap():
    """An ineligible item must always be able to leave.

    Holding a delisted item because the quarterly change budget is spent
    would be indefensible, so removals forced by screen failure are exempt
    from max_buffer_exits.
    """
    rules = MembershipRules(target_size=10, entry_rank=8, exit_rank=12,
                            max_buffer_exits=1)
    screened = pool(20)
    for i in range(5):
        screened[i] = candidate(i + 1, 100e9 / (i + 1), eligible=False, reasons=("gone",))
    decision = review(screened, list(range(1, 11)), rules)
    assert set(decision.forced_out) == {1, 2, 3, 4, 5}


# ------------------------------------------------------------- turnover caps


def test_additions_are_capped_per_review():
    rules = MembershipRules(target_size=10, entry_rank=8, exit_rank=12, max_additions=2)
    decision = review(pool(20), [11, 12, 13, 14, 15], rules)
    assert len(decision.added) == 2


def test_buffer_exit_cap_is_a_stay_of_execution_not_a_removal_cap():
    """Pins the field's real scope.

    `max_buffer_exits` keeps breached incumbents in the incumbent tier for
    one more period. It does not cap total removals: a reprieved incumbent
    still loses its seat if better-ranked candidates fill the basket, which
    is correct -- holding a rank-26 name in a ten-item index because the
    change budget is spent would be indefensible.
    """
    rules = MembershipRules(
        target_size=10, entry_rank=8, exit_rank=12, max_buffer_exits=2, max_additions=None
    )
    screened = pool(30)
    members = [1, 2, 3, 20, 21, 22, 23, 24, 25, 26]  # seven breach the buffer
    decision = review(screened, members, rules)
    # The top of the pool wins the seats regardless of the cap.
    assert decision.members[:8] == list(range(1, 9))
    assert len(decision.removed) > 2
    # But the two worst breachers are the ones explicitly marked as buffer exits.
    assert "outside the exit buffer" in decision.rationale[26]
    assert "outside the exit buffer" in decision.rationale[25]


def test_uncapped_rules_allow_full_replacement():
    rules = MembershipRules(
        target_size=10, entry_rank=8, exit_rank=12,
        max_additions=None, max_buffer_exits=None,
    )
    decision = review(pool(30), list(range(21, 31)), rules)
    assert len(decision.members) == 10
    # Ranks 1-8 are mandatory; 9 and 10 come from the reserve tier.
    assert decision.members == list(range(1, 11))


# ------------------------------------------------------------------ invariants


def test_basket_never_exceeds_target_size():
    for members in ([], list(range(1, 6)), list(range(1, 11)), list(range(1, 16))):
        decision = review(pool(30), members, RULES)
        assert len(decision.members) <= RULES.target_size


def test_membership_is_stable_under_repeated_review():
    """A review with unchanged inputs must be a no-op. Idempotence is what
    makes a twice-daily automated job safe to run."""
    screened = pool(20)
    first = review(screened, seed(screened, RULES).members, RULES)
    second = review(screened, first.members, RULES)
    assert not second.changed
    assert first.members == second.members


def test_every_change_carries_a_rationale():
    """A composition change nobody can explain is one nobody should trust."""
    screened = pool(20)
    screened[9] = candidate(10, 100e9 / 15)
    decision = review(screened, list(range(1, 11)), RULES)
    for item_id in decision.added + decision.removed:
        assert decision.rationale.get(item_id)


def test_unknown_current_members_are_dropped_quietly():
    """An item that vanished from /mapping entirely is not in the pool."""
    decision = review(pool(20), [1, 2, 9999], RULES)
    assert 9999 not in decision.members


def test_undersized_pool_is_flagged_not_padded():
    screened = [candidate(i, 100e9 / i) for i in range(1, 5)]
    decision = review(screened, [1, 2], RULES)
    assert decision.undersized
    assert len(decision.members) <= 4
