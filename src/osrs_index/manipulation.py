"""What it costs to move the index, and at what size that becomes rational.

This module is the reason the repo exists in the form it does. Every index
provider claims robustness; almost none publish the price of breaking their
own product. The numbers below are unflattering and that is the point.

The attack
----------
In an equal-weighted index of N members, moving one constituent by X% moves
the index by X/N %. A rational attacker picks the cheapest member, not the
average one, so basket robustness is set entirely by its weakest name.

The attacker's cost is not the notional they push through -- they keep the
items. It is the round trip: the premium paid on the way up, the spread
crossed, and the sell-side tax on the way out.

    cost ~= notional x (average_premium + spread + tax)

with average_premium below the target move because you buy into a rising
book. `PREMIUM_REALISATION` below encodes that; it is a judgement, not a
backtest, and it is the softest number in the repo.

The defence, and its limits
---------------------------
The NAV window is the only real lever. Against /latest, one unit at a silly
price is enough. Against a 24h volume-weighted window, an attacker must
absorb a full day of flow. Measured on Abyssal dagger (1.229M gp, 353
units/day, buy limit 8 per 4h):

    /latest      1 unit      ~0.12M gp   (~0.10 USD)
    5m VWAP      ~2 units    ~0.25M gp
    1h VWAP      ~15 units   ~1.8M gp    (~1.5 USD)
    24h VWAP     ~353 units  ~65M gp     (~55 USD)

Four hundred times harder, and still 55 dollars to move a ten-name index by
1%. That is the honest conclusion: OSRS market depth is not sufficient for a
manipulation-resistant index outside the top ~100 items, and those are all
end-game PvM gear with correlations near one.
"""

from __future__ import annotations

from dataclasses import dataclass

from .config import (
    BUY_LIMIT_WINDOWS_PER_DAY,
    GE_TAX_CAP_GP,
    GE_TAX_MIN_PRICE_GP,
    GE_TAX_RATE,
    gp_per_usd,
)

#: Fraction of the headline move actually paid as premium. Buying into a
#: rising book means early units cost near the old price and only the last
#: cost the new one, so realised premium sits well below the target move.
#: 0.5 is a reasoned midpoint; the true value is plausibly anywhere in
#: 0.3-0.7 and the qualitative conclusion survives the whole range.
PREMIUM_REALISATION = 0.5

#: Above this required move the linear cost model stops being credible.
#:
#: Cost is modelled as proportional to the move, which is defensible for a
#: 5-10% push. It is not defensible at 65% -- the figure that comes out of a
#: liquidity-capped 1.5% weight. You cannot move an item 65% by absorbing one
#: day of volume at a linear premium; supply responds, other holders sell into
#: you, and the true cost is far higher and highly convex.
#:
#: The error is conservative for the defender (real attacks cost MORE than
#: reported), so estimates above this threshold are flagged rather than
#: suppressed. Treat a flagged estimate as "prohibitively expensive, magnitude
#: unknown" rather than as a number.
LINEAR_MODEL_MAX_MOVE = 0.20

#: Share of a window's volume an attacker must account for to dominate its
#: volume-weighted average. Not 100%: organic flow keeps trading alongside,
#: but past roughly this share the attacker sets the print.
DOMINANCE_SHARE = 1.0

_WINDOW_FRACTION_OF_DAY = {
    "latest": 0.0,  # special-cased: a single print
    "5m": 5 / (60 * 24),
    "1h": 1 / 24,
    "6h": 6 / 24,
    "24h": 1.0,
}


def ge_tax_gp(unit_price: float, units: float) -> float:
    """Sell-side Grand Exchange tax.

    2% since 2025-05-29 (1% from 2021-12-09), capped at 5M gp per item, and
    not levied below 50 gp. The per-item cap matters for exactly the names an
    attacker would target at the top of the market: on a 1.2B gp Scythe the
    tax is 5M, an effective 0.4%, not 2%.
    """
    if unit_price < GE_TAX_MIN_PRICE_GP:
        return 0.0
    per_unit = min(unit_price * GE_TAX_RATE, GE_TAX_CAP_GP)
    return per_unit * units


@dataclass(frozen=True)
class AttackEstimate:
    item_name: str
    nav_window: str
    index_move: float
    required_item_move: float
    units_required: float
    notional_gp: float
    premium_gp: float
    spread_gp: float
    tax_gp: float
    total_cost_gp: float
    total_cost_usd: float
    accounts_required: int
    breakeven_position_gp: float
    breakeven_position_usd: float
    #: True when required_item_move exceeds LINEAR_MODEL_MAX_MOVE, i.e. the
    #: linear cost model is out of its validity range and the true cost is
    #: higher by an unknown, convex amount.
    beyond_linear_model: bool = False

    def render(self) -> str:
        caveat = " [beyond linear model: true cost higher]" if self.beyond_linear_model else ""
        return (
            f"{self.item_name} via {self.nav_window} NAV: "
            f"{self.units_required:,.0f} units, "
            f"cost {self.total_cost_gp/1e6:,.0f}M gp (~${self.total_cost_usd:,.0f}), "
            f"{self.accounts_required} account(s), "
            f"profitable above {self.breakeven_position_gp/1e9:,.1f}B gp of exposure"
            f"{caveat}"
        )


def estimate_attack(
    item_name: str,
    unit_price: float,
    daily_units: float,
    relative_spread: float,
    buy_limit: int | None,
    n_members: int,
    index_move: float = 0.01,
    nav_window: str = "24h",
    member_weight: float | None = None,
    bond_gp_price: int | None = None,
) -> AttackEstimate:
    """Cost of moving an index by `index_move` through one constituent.

    `member_weight` defaults to equal weight (1/N). Pass the actual capped
    weight to see how much the liquidity cap in index.py is buying you --
    usually less than people expect, because halving a weight only doubles
    the attack cost while the attack was three orders of magnitude too cheap
    to begin with.
    """
    if nav_window not in _WINDOW_FRACTION_OF_DAY:
        raise ValueError(f"unknown nav window {nav_window!r}")
    if unit_price <= 0 or daily_units <= 0:
        raise ValueError("unit_price and daily_units must be positive")

    weight = member_weight if member_weight is not None else 1.0 / n_members
    if weight <= 0:
        raise ValueError("member weight must be positive")

    required_item_move = index_move / weight

    if nav_window == "latest":
        # /latest reports the most recent print. One transaction rewrites it.
        # This is not a hypothetical: it is why nav.py refuses to read it.
        units_required = 1.0
    else:
        fraction = _WINDOW_FRACTION_OF_DAY[nav_window]
        units_required = max(1.0, daily_units * fraction * DOMINANCE_SHARE)

    notional = units_required * unit_price
    premium = notional * required_item_move * PREMIUM_REALISATION
    spread_cost = notional * max(relative_spread, 0.0)
    tax = ge_tax_gp(unit_price * (1 + required_item_move), units_required)
    total = premium + spread_cost + tax

    if buy_limit and buy_limit > 0:
        daily_limit_per_account = buy_limit * BUY_LIMIT_WINDOWS_PER_DAY
        # Buy limits are per account, so the constraint is a multi-accounting
        # cost, not a wall. Extra accounts cost a bond each -- material, but
        # not decisive at these attack sizes.
        accounts = max(1, -(-int(units_required) // daily_limit_per_account))
    else:
        accounts = 1

    rate = gp_per_usd(bond_gp_price) if bond_gp_price else gp_per_usd()
    breakeven = total / index_move if index_move > 0 else float("inf")

    return AttackEstimate(
        item_name=item_name,
        nav_window=nav_window,
        index_move=index_move,
        required_item_move=required_item_move,
        units_required=units_required,
        notional_gp=notional,
        premium_gp=premium,
        spread_gp=spread_cost,
        tax_gp=tax,
        total_cost_gp=total,
        total_cost_usd=total / rate,
        accounts_required=accounts,
        breakeven_position_gp=breakeven,
        breakeven_position_usd=breakeven / rate,
        beyond_linear_model=required_item_move > LINEAR_MODEL_MAX_MOVE,
    )


def weakest_link(estimates: list[AttackEstimate]) -> AttackEstimate | None:
    """The constituent that sets the basket's real robustness.

    An index is exactly as manipulable as its cheapest member. Averaging
    attack costs across the basket -- which is the intuitive thing to do --
    overstates robustness by orders of magnitude, because the attacker is
    under no obligation to pick an average name.
    """
    return min(estimates, key=lambda e: e.total_cost_gp) if estimates else None


def max_safe_aum_gp(cheapest: AttackEstimate, safety_factor: float = 10.0) -> float:
    """Largest AUM at which manipulation is not obviously rational.

    An attacker holding position P profits `index_move x P` from an attack
    costing `total_cost_gp`, so the attack pays above
    `breakeven_position_gp`. For that to be unreachable, no single
    participant should plausibly hold it -- hence dividing by a safety
    factor representing the largest tolerable single-holder share.

    Applied to the melee basket with a 24h VWAP this lands around 6.5B gp of
    single-participant exposure, i.e. a cap in the low billions. That is a
    product with a ceiling of a few thousand dollars of notional. Stating
    that plainly is more useful than shipping a fund that discovers it.
    """
    return cheapest.breakeven_position_gp / safety_factor
