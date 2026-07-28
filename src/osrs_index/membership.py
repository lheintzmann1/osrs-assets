"""Rank-based index membership with hysteresis buffers.

The problem this solves: a fixed seed list goes stale. Items get nerfed, new
raids reprice the meta, and an index that still holds what was liquid a year
ago is measuring history. But naive "top N by liquidity" is worse -- an item
oscillating around rank 10 enters and exits every review, and each round trip
costs ~3.5-4% (spread plus the 2% GE sell tax). Turnover eats the index.

The standard fix, used by essentially every real index provider, is a
buffer: an item must be clearly good to get in, and clearly bad to get out.

    target_size = 10
    entry_rank  =  8   ->  join only if you rank 8th or better
    exit_rank   = 12   ->  leave only if you rank worse than 12th

An item sitting at rank 9-12 is left exactly where it is, in or out. The dead
band absorbs the noise, and only a decisive move triggers a trade.

Candidate pools are curated, not derived
----------------------------------------
The API has no item taxonomy: nothing in `/mapping` says "this is a melee
weapon". So the candidate pool for each basket is a human-maintained list in
the index spec, and these rules choose which N of those candidates are live
at any moment. Curation decides what the basket is *about*; the rules decide
what it *holds*. Pretending the second could be automated too would mean
inventing a category system and calling it data.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, field

from .universe import ScreenResult


@dataclass(frozen=True)
class MembershipRules:
    """Entry/exit thresholds. `entry_rank <= target_size <= exit_rank`.

    The asymmetry is the whole mechanism. Equal thresholds
    (entry_rank == exit_rank == target_size) degenerate to naive top-N and
    reintroduce the churn the buffer exists to prevent.
    """

    target_size: int = 10
    entry_rank: int = 8
    exit_rank: int = 12
    #: Cap on entries per review, so a data glitch or one disruptive game
    #: update cannot rewrite the whole index. This is the *real* turnover
    #: control: bounding entries bounds how many incumbents get displaced.
    max_additions: int | None = 3

    #: Stay of execution for incumbents that breached `exit_rank`. At most
    #: this many are pushed out of the incumbent tier per review; the rest
    #: keep tier-1 priority for one more period.
    #:
    #: Note carefully what this does NOT do: it does not cap total removals.
    #: An incumbent granted a stay can still lose its seat at selection if
    #: better-ranked candidates fill the basket first -- and it should. Holding
    #: a rank-26 item in a ten-name index because the change budget is spent
    #: would be indefensible. The field is named for its actual scope rather
    #: than the guarantee people expect from it.
    max_buffer_exits: int | None = 3

    def __post_init__(self) -> None:
        if self.target_size < 1:
            raise ValueError("target_size must be at least 1")
        if not self.entry_rank <= self.target_size <= self.exit_rank:
            raise ValueError(
                f"need entry_rank <= target_size <= exit_rank, got "
                f"{self.entry_rank} <= {self.target_size} <= {self.exit_rank}"
            )


@dataclass(frozen=True)
class RankedCandidate:
    item_id: int
    name: str
    rank: int
    liquidity_gp: float
    eligible: bool
    reasons: tuple[str, ...]


@dataclass(frozen=True)
class MembershipDecision:
    ranked: list[RankedCandidate]
    members: list[int]
    added: list[int] = field(default_factory=list)
    removed: list[int] = field(default_factory=list)
    retained: list[int] = field(default_factory=list)
    forced_out: list[int] = field(default_factory=list)
    #: Human-readable justification per changed item. Published alongside the
    #: index: a composition change nobody can explain is indistinguishable
    #: from a composition change nobody should trust.
    rationale: dict[int, str] = field(default_factory=dict)
    #: True when fewer than target_size candidates were eligible.
    undersized: bool = False

    @property
    def changed(self) -> bool:
        return bool(self.added or self.removed)


def rank_candidates(screened: Sequence[ScreenResult]) -> list[RankedCandidate]:
    """Order candidates by liquidity, ineligible ones last.

    Ties break on item_id. That looks pedantic and is not: without a total
    order the same inputs can yield different baskets across runs, and an
    index that does not reproduce cannot be audited. Every ordering in this
    module is total for that reason.
    """
    ordered = sorted(
        screened,
        key=lambda r: (not r.eligible, -r.gp_volume_24h, r.item_id),
    )
    return [
        RankedCandidate(
            item_id=result.item_id,
            name=result.name,
            rank=position,
            liquidity_gp=result.gp_volume_24h,
            eligible=result.eligible,
            reasons=result.reasons,
        )
        for position, result in enumerate(ordered, start=1)
    ]


#: Selection priority tiers. Lower is better; ties inside a tier break on rank.
_TIER_MANDATORY = 0  # eligible and ranked at or inside entry_rank
_TIER_INCUMBENT = 1  # already a member and not forced or buffered out
_TIER_RESERVE = 2  # non-member sitting in the buffer zone


def review(
    screened: Sequence[ScreenResult],
    current_members: Sequence[int],
    rules: MembershipRules | None = None,
) -> MembershipDecision:
    """Decide the basket for this review, given last review's basket.

    The selection is a three-tier priority queue, truncated at `target_size`:

    - **Tier 0, mandatory.** Eligible and ranked at or inside `entry_rank`.
      The top of the pool is always in the index; no incumbent outranks it.
    - **Tier 1, incumbents.** Current members that neither failed the screen
      nor breached `exit_rank`. This is where the hysteresis lives: an
      incumbent in the buffer zone outranks an equally-placed challenger.
    - **Tier 2, reserve.** Non-members in the buffer zone
      (`entry_rank < rank <= exit_rank`), used only to fill genuine vacancies.

    Tier 2 is the part that a naive reading of "enter at 8, exit at 12" gets
    wrong, and this module got wrong first. If entry is a hard gate, then once
    ranks 1-8 are all members a vacancy at rank 9 can never be filled -- no
    eligible non-member ranks 8 or better. The basket shrinks monotonically
    and target_size silently becomes a ceiling it can only drift below. The
    reserve tier fixes that: the buffer zone is a priority queue favouring
    incumbents, not a wall.

    Order of operations:

    1. **Forced exits.** An item failing the hard screen leaves regardless of
       rank and regardless of turnover caps.
    2. **Buffered exits.** Members ranked worse than `exit_rank`, worst first,
       subject to `max_buffer_exits`.
    3. **Selection.** Fill to `target_size` by (tier, rank).
    4. **Addition cap.** Trim the newly added down to `max_additions`.
    """
    rules = rules or MembershipRules()
    ranked = rank_candidates(screened)
    by_id = {c.item_id: c for c in ranked}

    current = [item_id for item_id in current_members if item_id in by_id]
    current_set = set(current)
    rationale: dict[int, str] = {}

    # 1. Forced exits: failed the hard screen.
    forced_out = [item_id for item_id in current if not by_id[item_id].eligible]
    for item_id in forced_out:
        rationale[item_id] = f"forced out: {', '.join(by_id[item_id].reasons)}"

    surviving = [item_id for item_id in current if item_id not in set(forced_out)]

    # 2. Buffered exits: ranked outside the exit buffer. Worst first.
    breached = sorted(
        (item_id for item_id in surviving if by_id[item_id].rank > rules.exit_rank),
        key=lambda item_id: -by_id[item_id].rank,
    )
    if rules.max_buffer_exits is not None:
        breached = breached[: rules.max_buffer_exits]
    breached_set = set(breached)
    for item_id in breached:
        rationale[item_id] = (
            f"rank {by_id[item_id].rank} is outside the exit buffer (> {rules.exit_rank})"
        )

    retained_incumbents = {i for i in surviving if i not in breached_set}

    # 3. Selection by (tier, rank).
    def tier(candidate: RankedCandidate) -> int | None:
        if not candidate.eligible:
            return None
        if candidate.rank <= rules.entry_rank:
            return _TIER_MANDATORY
        if candidate.item_id in retained_incumbents:
            return _TIER_INCUMBENT
        if candidate.rank <= rules.exit_rank:
            return _TIER_RESERVE
        return None

    pool_: list[tuple[int, int, int]] = []
    for candidate in ranked:
        candidate_tier = tier(candidate)
        if candidate_tier is not None:
            pool_.append((candidate_tier, candidate.rank, candidate.item_id))
    pool_.sort()

    selected = [item_id for _, _, item_id in pool_[: rules.target_size]]

    # 4. Cap additions, keeping the best-ranked ones.
    additions = [i for i in selected if i not in current_set]
    if rules.max_additions is not None and len(additions) > rules.max_additions:
        additions.sort(key=lambda item_id: by_id[item_id].rank)
        rejected = set(additions[rules.max_additions :])
        additions = additions[: rules.max_additions]
        selected = [i for i in selected if i not in rejected]

    for item_id in additions:
        candidate = by_id[item_id]
        why = (
            f"rank {candidate.rank} is inside the entry buffer (<= {rules.entry_rank})"
            if candidate.rank <= rules.entry_rank
            else f"rank {candidate.rank} filled a vacancy from the buffer zone"
        )
        rationale[item_id] = why

    members = sorted(selected, key=lambda item_id: by_id[item_id].rank)
    members_set = set(members)

    # An incumbent can also drop out by being outranked at selection time --
    # distinct from breaching the exit buffer, and worth saying so.
    for item_id in surviving:
        if item_id not in members_set and item_id not in breached_set:
            rationale.setdefault(
                item_id,
                f"outranked at selection (rank {by_id[item_id].rank}, "
                f"target size {rules.target_size})",
            )

    removed = sorted(
        (i for i in current if i not in members_set),
        key=lambda item_id: by_id[item_id].rank,
    )

    eligible_total = sum(1 for c in ranked if c.eligible)
    return MembershipDecision(
        ranked=ranked,
        members=members,
        added=sorted(additions, key=lambda item_id: by_id[item_id].rank),
        removed=removed,
        retained=[i for i in members if i in current_set],
        forced_out=forced_out,
        rationale=rationale,
        undersized=eligible_total < rules.target_size or len(members) < rules.target_size,
    )


def seed(
    screened: Sequence[ScreenResult], rules: MembershipRules | None = None
) -> MembershipDecision:
    """First composition for an index with no history.

    Inception ignores the buffers -- there is nothing to be hysteretic about
    -- and simply takes the top `target_size` eligible candidates. Turnover
    caps are also bypassed, since every member is an addition by definition.
    """
    rules = rules or MembershipRules()
    ranked = rank_candidates(screened)
    eligible = [c for c in ranked if c.eligible]
    members = [c.item_id for c in eligible[: rules.target_size]]
    return MembershipDecision(
        ranked=ranked,
        members=members,
        added=members,
        rationale={
            item_id: f"inception: rank {ranked[i].rank}"
            for i, item_id in enumerate(members)
        },
        undersized=len(eligible) < rules.target_size,
    )
