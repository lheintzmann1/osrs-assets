"""Runtime configuration.

Every knob that changes an index value lives here or in an index spec YAML,
never inline in the computation. An index whose parameters are scattered
across the codebase cannot be audited, and an index nobody can audit is
worth nothing.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_DB_PATH = REPO_ROOT / "data" / "osrs_index.sqlite3"
DEFAULT_SPEC_DIR = REPO_ROOT / "indices"

#: Grand Exchange sell-side tax. Introduced 2021-12-09 at 1%, raised to 2% on
#: 2025-05-29. Capped at 5,000,000 gp per item. Items under 50 gp are exempt,
#: as are bonds and a list of low-value staples.
GE_TAX_RATE = 0.02
GE_TAX_CAP_GP = 5_000_000
GE_TAX_MIN_PRICE_GP = 50

#: Buy limits reset every 4 hours, so 6 windows per day. There is no sell-side
#: limit -- an asymmetry that dominates the redemption analysis in
#: docs/feasibility.md.
BUY_LIMIT_WINDOW_HOURS = 4
BUY_LIMIT_WINDOWS_PER_DAY = 24 // BUY_LIMIT_WINDOW_HOURS

#: Reference rate used only to express gp costs in USD for readability.
#: Derived from the bond price: 9.99 USD per bond (Jagex raised the price in
#: March 2026) divided by the GE bond price. This is the EXPENSIVE end of the
#: range -- grey-market gold trades far below it, so every USD figure in this
#: repo is an upper bound on what an attacker actually pays.
BOND_USD_PRICE = 9.99
DEFAULT_BOND_GP_PRICE = 11_720_878


def gp_per_usd(bond_gp_price: int = DEFAULT_BOND_GP_PRICE) -> float:
    return bond_gp_price / BOND_USD_PRICE


@dataclass(frozen=True)
class Settings:
    user_agent: str
    db_path: Path = DEFAULT_DB_PATH
    spec_dir: Path = DEFAULT_SPEC_DIR
    log_level: str = "INFO"

    @classmethod
    def from_env(cls) -> Settings:
        """Read configuration. Deliberately does NOT demand a User-Agent.

        Only `collect` and `backfill` reach the network. Everything else --
        restore, screen, build, replay, attack, publish, status -- reads local
        files. Requiring a contact route to parse committed NDJSON is a
        gratuitous failure, and it is one people hit in CI, where the natural
        pipeline starts with an offline `restore` step.

        Commands that do open a connection call `require_user_agent()`.
        """
        return cls(
            user_agent=os.environ.get("OSRS_INDEX_USER_AGENT", ""),
            db_path=Path(os.environ.get("OSRS_INDEX_DB", DEFAULT_DB_PATH)),
            spec_dir=Path(os.environ.get("OSRS_INDEX_SPECS", DEFAULT_SPEC_DIR)),
            log_level=os.environ.get("OSRS_INDEX_LOG_LEVEL", "INFO"),
        )

    def require_user_agent(self) -> str:
        """Assert a usable User-Agent, with an actionable message if not.

        Not a secret and not a token: it is a public identifier naming the
        project and a route to reach its operator. Put it in the workflow's
        `env:` block, never in repository secrets -- a masked User-Agent
        defeats the entire purpose of sending one.
        """
        if not self.user_agent.strip():
            raise SystemExit(
                "OSRS_INDEX_USER_AGENT is not set.\n\n"
                "The wiki API asks every client to identify itself with a contact\n"
                "route. This is a plain environment variable, not a secret --\n"
                "it is meant to be readable by the people running the API.\n\n"
                "Locally:\n"
                "  export OSRS_INDEX_USER_AGENT='osrs-assets/0.1 - @yourhandle on Discord'\n\n"
                "In GitHub Actions, add it to the job's env: block:\n"
                "  env:\n"
                "    OSRS_INDEX_USER_AGENT: "
                "'osrs-assets/0.1 (+https://github.com/OWNER/REPO)'\n"
            )
        return self.user_agent


@dataclass(frozen=True)
class NavParams:
    """Parameters of the NAV estimator. See nav.py and docs/methodology.md.

    Defaults are chosen to make the estimator as expensive to manipulate as is
    possible with this data source, which is still not very expensive. See
    docs/feasibility.md section 4.
    """

    #: Number of trailing 1h buckets aggregated into one NAV observation.
    window_buckets: int = 24
    #: Buckets with fewer total units than this are discarded outright: a
    #: bucket built from one or two trades is a single actor's opinion.
    min_units_per_bucket: int = 5
    #: Buckets further than this many robust standard deviations from the
    #: window median are winsorised (clamped, not dropped -- dropping would
    #: let an attacker shrink the sample).
    winsor_sigma: float = 3.0
    #: Below this many surviving buckets the observation is not published as
    #: fresh; the previous value is carried and flagged STALE.
    min_valid_buckets: int = 12
    #: A crossed observation (instant-buy below instant-sell) is a
    #: microstructure artefact, not a signal. Seen on ~10% of items over 24h.
    drop_crossed_buckets: bool = True


@dataclass(frozen=True)
class UniverseParams:
    """Eligibility screen. See universe.py.

    The volume floor is the single most consequential number in this repo:
    it trades index breadth against manipulation cost, and there is no
    setting at which both are acceptable. See docs/feasibility.md section 4.
    """

    min_gp_volume_24h: int = 1_000_000_000
    min_price_gp: int = GE_TAX_MIN_PRICE_GP
    max_median_spread: float = 0.05
    min_two_sided_bucket_ratio: float = 0.90
    #: Items younger than this are still in price discovery.
    min_history_days: int = 90

    # -- Physical replicability. Off by default. -------------------------
    #
    # `limit x price` per 4h window measures whether a basket can actually be
    # ASSEMBLED, which is a question about a fund, not about an index. A
    # read-only index has no reason to exclude Blood rune (8.5M per window)
    # when it turns over 21.8B gp a day.
    #
    # Applying it globally is a real modelling error and it is easy to make:
    # a 20M floor rejects every constituent of the PvM consumables basket --
    # the most liquid of the four -- purely because consumables are cheap
    # items with high buy limits. Enable it only when screening for a
    # physically replicated product, and read the capacity figure the screen
    # reports either way.
    require_physical_replicability: bool = False
    min_window_notional_gp: int = 20_000_000
    members_only: bool | None = None
    excluded_item_ids: frozenset[int] = field(default_factory=frozenset)
