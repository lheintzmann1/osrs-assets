"""Command line interface.

    python3 -m osrs_index collect          # bootstrap the database
    python3 -m osrs_index backfill         # 365d of daily history for seeds
    python3 -m osrs_index screen           # eligibility report per index
    python3 -m osrs_index build            # compute and persist index levels
    python3 -m osrs_index attack           # manipulation cost table
    python3 -m osrs_index status           # collection health
"""

from __future__ import annotations

import argparse
import logging
import sys
import time

from .client import AGGREGATE_STEPS, ClientConfig, PricesClient
from .collect import backfill_timeseries, collect_aggregate, collect_all, collect_latest
from .config import REPO_ROOT, NavParams, Settings, UniverseParams, gp_per_usd
from .export import (
    append_level,
    build_site_payload,
    export_bars,
    export_composition,
    export_items,
    import_bars,
    import_composition,
    import_items,
    read_history,
    write_site,
)
from .manipulation import estimate_attack
from .pipeline import SCREEN_STEP, build, load_rules, load_spec, load_specs
from .replay import replay, summarise
from .storage import Store


def _client(settings: Settings) -> PricesClient:
    """Build an API client, refusing early if we cannot identify ourselves."""
    return PricesClient(ClientConfig(user_agent=settings.require_user_agent()))


def _fmt_gp(value: float | None) -> str:
    if value is None:
        return "-"
    for unit, scale in (("T", 1e12), ("B", 1e9), ("M", 1e6), ("K", 1e3)):
        if abs(value) >= scale:
            return f"{value / scale:,.2f}{unit}"
    return f"{value:,.0f}"


def cmd_collect(args: argparse.Namespace, settings: Settings) -> int:
    client = _client(settings)
    with Store(settings.db_path) as store:
        if args.step:
            count = collect_aggregate(client, store, args.step)
            print(f"collected {count} {args.step} bars")
        elif args.latest:
            count = collect_latest(client, store)
            print(f"collected {count} latest ticks")
        else:
            results = collect_all(client, store)
            for key, count in results.items():
                print(f"{key:>10}: {count:,}")
    return 0


def cmd_backfill(args: argparse.Namespace, settings: Settings) -> int:
    client = _client(settings)
    with Store(settings.db_path) as store:
        items = store.items()
        by_name = {i.name.lower(): i.id for i in items}
        if not by_name:
            print("no items in database -- run `collect` first", file=sys.stderr)
            return 1

        wanted: list[int] = []
        for spec in load_specs(settings.spec_dir):
            for name in spec.candidate_item_names:
                item_id = by_name.get(name.lower())
                if item_id is None:
                    print(f"  ! unresolved seed name: {name}", file=sys.stderr)
                elif item_id not in wanted:
                    wanted.append(item_id)

        print(f"backfilling {len(wanted)} items at {args.step} granularity...")
        total = backfill_timeseries(client, store, wanted, step=args.step)
        print(f"inserted/updated {total:,} bars")
    return 0


def cmd_screen(args: argparse.Namespace, settings: Settings) -> int:
    universe = UniverseParams(
        min_gp_volume_24h=args.min_volume,
        require_physical_replicability=args.replicable,
        min_window_notional_gp=args.min_window_notional,
    )
    with Store(settings.db_path) as store:
        for spec in load_specs(settings.spec_dir):
            result = build(store, spec, universe_params=universe)
            print(f"\n=== {spec.name} ({spec.index_id}) ===")
            if result.unresolved:
                print(f"  unresolved names: {', '.join(result.unresolved)}")
            print(
                f"  {len(result.included)} eligible / {len(result.screened)} screened "
                f"(volume floor {_fmt_gp(universe.min_gp_volume_24h)} gp/24h)"
            )
            print(
                f"  {'item':32s} {'24h gp vol':>12s} {'spread':>8s} "
                f"{'4h notional':>12s} {'status'}"
            )
            for row in sorted(result.screened, key=lambda r: -r.gp_volume_24h):
                spread = f"{row.median_spread:.2%}" if row.median_spread is not None else "-"
                status = "OK" if row.eligible else f"REJECT: {row.rejected_for}"
                print(
                    f"  {row.name[:32]:32s} {_fmt_gp(row.gp_volume_24h):>12s} "
                    f"{spread:>8s} {_fmt_gp(row.window_notional_gp):>12s} {status}"
                )
            if result.creation_capacity_gp:
                print(
                    f"  physical creation capacity: "
                    f"{_fmt_gp(result.creation_capacity_gp)} gp per account per day"
                )
    return 0


def cmd_build(args: argparse.Namespace, settings: Settings) -> int:
    nav = NavParams(min_valid_buckets=args.min_buckets)
    universe = UniverseParams(min_gp_volume_24h=args.min_volume)
    with Store(settings.db_path) as store:
        for spec in load_specs(settings.spec_dir):
            result = build(store, spec, nav_params=nav, universe_params=universe, persist=True)
            print(f"\n=== {spec.name} ({spec.index_id}) ===")
            if result.level is None:
                print("  no eligible constituents -- index not computed")
                for row in result.excluded:
                    print(f"    - {row.name}: {row.rejected_for}")
                continue
            print(
                f"  level {result.level.level:,.2f}  "
                f"divisor {result.divisor:,.4f}  "
                f"quality {result.level.quality.value}  "
                f"({result.level.n_stale_members} stale of {result.level.n_members})"
            )
            print(f"  {'constituent':32s} {'weight':>8s} {'units':>14s} {'capped'}")
            for c in sorted(result.constituents, key=lambda c: -c.target_weight):
                print(
                    f"  {c.name[:32]:32s} {c.target_weight:7.2%} "
                    f"{c.units:14,.4f} {'yes' if c.capped else ''}"
                )
            if result.cap_infeasible:
                print("  ! liquidity caps infeasible; weights normalised")
            if result.cheapest_attack is not None:
                attack = result.cheapest_attack
                print(f"  weakest link: {attack.render()}")  # type: ignore[attr-defined]
            if result.max_safe_aum_gp:
                print(
                    f"  suggested AUM ceiling: {_fmt_gp(result.max_safe_aum_gp)} gp "
                    f"(~${result.max_safe_aum_gp / gp_per_usd():,.0f})"
                )
    return 0


def cmd_attack(args: argparse.Namespace, settings: Settings) -> int:
    """Show how attack cost scales with the NAV window.

    The table this prints is the single most important output of the repo.
    """
    with Store(settings.db_path) as store:
        for spec in load_specs(settings.spec_dir):
            result = build(store, spec)
            if not result.constituents:
                continue
            print(f"\n=== {spec.name} ===")
            print(f"  moving the index {args.move:.1%} costs, per NAV window:")
            print(
                f"  {'constituent':30s} {'latest':>12s} {'5m':>12s} "
                f"{'1h':>12s} {'24h':>12s} {'accts':>6s}"
            )
            items = {i.id: i for i in store.items()}
            for c in sorted(result.constituents, key=lambda c: c.name):
                screen_row = next(r for r in result.included if r.item_id == c.item_id)
                bars = store.bars(c.item_id, SCREEN_STEP, limit=1)
                if not bars or bars[0].total_volume <= 0:
                    continue
                price = bars[0].vwap
                if not price:
                    continue
                costs = []
                accounts = 1
                for window in ("latest", "5m", "1h", "24h"):
                    est = estimate_attack(
                        item_name=c.name,
                        unit_price=price,
                        daily_units=bars[0].total_volume,
                        relative_spread=screen_row.median_spread or 0.0,
                        buy_limit=items[c.item_id].buy_limit,
                        n_members=len(result.constituents),
                        index_move=args.move,
                        nav_window=window,
                        member_weight=c.target_weight,
                    )
                    costs.append(est.total_cost_gp)
                    accounts = est.accounts_required
                print(
                    f"  {c.name[:30]:30s} "
                    + " ".join(f"{_fmt_gp(x):>12s}" for x in costs)
                    + f" {accounts:>6d}"
                )
            print(
                "\n  Costs are gp. USD equivalents use the bond rate "
                f"({gp_per_usd():,.0f} gp/USD), which is the expensive end -- "
                "grey-market gold is cheaper, so real attack costs are lower."
            )
    return 0


def cmd_replay(args: argparse.Namespace, settings: Settings) -> int:
    """Reconstruct index history over stored daily bars.

    Produces a BACKTEST, not a track record. See replay.py for the three
    specific reasons it flatters: survivorship bias in the candidate pool,
    daily rather than hourly valuation, and no transaction costs.
    """
    data_dir = REPO_ROOT / "data"
    with Store(settings.db_path) as store:
        for path in sorted(settings.spec_dir.glob("*.json")):
            spec = load_spec(path)
            result = replay(store, spec, load_rules(path), warmup_days=args.warmup)
            stats = summarise(result)
            if stats.get("observations", 0) < 2:
                print(f"  {spec.index_id:12s} not enough history to replay", file=sys.stderr)
                continue
            print(
                f"  {spec.index_id:12s} {stats['observations']:>4} days  "
                f"total {stats['total_return']:+7.1%}  "
                f"vol {stats['annualised_vol']:>5.1%} ann  "
                f"maxDD {stats['max_drawdown']:+6.1%}  "
                f"{stats['reviews']} reviews"
            )
            if args.write:
                path_out = data_dir / "history" / f"{spec.index_id}.ndjson"
                path_out.parent.mkdir(parents=True, exist_ok=True)
                path_out.write_text("")
                for level in result.levels:
                    append_level(data_dir, level, simulated=True)
    if args.write:
        print("wrote simulated history; levels are flagged simulated=true")
    else:
        print("dry run -- pass --write to persist the simulated history")
    return 0


def cmd_publish(args: argparse.Namespace, settings: Settings) -> int:
    """Build every index, append to history, and regenerate the site payload.

    This is what CI runs. It is deliberately one command: a publish that can
    half-succeed leaves the committed history and the served JSON disagreeing,
    and there is no way to tell which one is right after the fact.
    """
    data_dir = REPO_ROOT / "data"
    site_dir = REPO_ROOT / "site"
    nav = NavParams()
    universe = UniverseParams()
    payloads = []

    with Store(settings.db_path) as store:
        for path in sorted(settings.spec_dir.glob("*.json")):
            spec = load_spec(path)
            rules = load_rules(path)
            result = build(
                store, spec, nav, universe, rules,
                review_now=args.review, persist=True,
            )
            if result.level is None:
                print(f"  ! {spec.index_id}: no eligible constituents, skipped",
                      file=sys.stderr)
                continue

            append_level(data_dir, result.level)
            history = read_history(data_dir, spec.index_id)
            prices = {}
            for constituent in result.constituents:
                bars = store.bars(constituent.item_id, SCREEN_STEP, limit=1)
                prices[constituent.item_id] = (bars[0].vwap or 0.0) if bars else 0.0

            export_composition(store, data_dir, spec.index_id)
            payloads.append(build_site_payload(result, history, prices))
            flag = "" if not result.membership or not result.membership.changed else "  [changed]"
            print(
                f"  {spec.index_id:12s} {result.level.level:>10,.2f}  "
                f"{result.mode:9s} {result.level.quality.value:8s} "
                f"{len(result.constituents)} members{flag}"
            )

        export_bars(store, data_dir)
        export_items(store, data_dir)

    out = write_site(payloads, site_dir)
    print(f"wrote {len(payloads)} index payloads to {out}")
    return 0


def cmd_restore(args: argparse.Namespace, settings: Settings) -> int:
    """Rebuild the local database from committed plain-text data.

    The repo is the store of record; SQLite is a cache that can be deleted at
    any time. CI relies on this: it restores, collects the new day, rebuilds,
    and re-exports.
    """
    data_dir = REPO_ROOT / "data"
    with Store(settings.db_path) as store:
        items = import_items(store, data_dir)
        bars = import_bars(store, data_dir)
        baskets = import_composition(store, data_dir)
    print(
        f"restored {items:,} items, {bars:,} bars and {baskets} live basket(s) "
        f"from {data_dir}"
    )
    return 0


def cmd_status(args: argparse.Namespace, settings: Settings) -> int:
    now = int(time.time())
    with Store(settings.db_path) as store:
        items = store.items()
        print(f"database: {settings.db_path}")
        print(f"items:    {len(items):,} active")
        for step in AGGREGATE_STEPS:
            latest = store.latest_bar_ts(step)
            count = store.bar_count(step)
            age = f"{(now - latest) / 60:,.0f} min ago" if latest else "never"
            print(f"  {step:>4s} bars: {count:>10,}   last: {age}")
        print("\nrecent jobs:")
        for run in store.recent_runs(10):
            state = "ok" if run["ok"] else "FAIL"
            print(
                f"  {run['job']:>22s} {state:>4s} "
                f"{run['rows'] or 0:>8,} rows  {run['error'] or ''}"
            )
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="osrs_index", description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)

    p = sub.add_parser("collect", help="fetch mapping and price data")
    p.add_argument("--step", choices=AGGREGATE_STEPS, help="collect one window only")
    p.add_argument("--latest", action="store_true", help="collect /latest ticks only")
    p.set_defaults(func=cmd_collect)

    p = sub.add_parser("backfill", help="pull historical buckets for index seeds")
    p.add_argument("--step", default="24h", choices=AGGREGATE_STEPS)
    p.set_defaults(func=cmd_backfill)

    p = sub.add_parser("screen", help="eligibility report")
    p.add_argument("--min-volume", type=int, default=UniverseParams().min_gp_volume_24h)
    p.add_argument(
        "--replicable",
        action="store_true",
        help="also require the basket to be physically buyable under buy limits",
    )
    p.add_argument(
        "--min-window-notional", type=int, default=UniverseParams().min_window_notional_gp
    )
    p.set_defaults(func=cmd_screen)

    p = sub.add_parser("build", help="compute and persist index levels")
    p.add_argument("--min-volume", type=int, default=UniverseParams().min_gp_volume_24h)
    p.add_argument("--min-buckets", type=int, default=NavParams().min_valid_buckets)
    p.set_defaults(func=cmd_build)

    p = sub.add_parser("attack", help="manipulation cost by NAV window")
    p.add_argument("--move", type=float, default=0.01, help="target index move (default 1%%)")
    p.set_defaults(func=cmd_attack)

    p = sub.add_parser("replay", help="reconstruct index history from stored daily bars")
    p.add_argument("--warmup", type=int, default=30, help="days skipped before inception")
    p.add_argument("--write", action="store_true", help="persist the simulated history")
    p.set_defaults(func=cmd_replay)

    p = sub.add_parser("publish", help="build indices, append history, write site data")
    p.add_argument(
        "--review",
        action="store_true",
        help="also re-run membership selection and reset weights (scheduled, not daily)",
    )
    p.set_defaults(func=cmd_publish)

    p = sub.add_parser("restore", help="rebuild the database from committed data/")
    p.set_defaults(func=cmd_restore)

    p = sub.add_parser("status", help="collection health")
    p.set_defaults(func=cmd_status)

    args = parser.parse_args(argv)
    settings = Settings.from_env()
    logging.basicConfig(
        level=settings.log_level, format="%(asctime)s %(levelname)s %(name)s: %(message)s"
    )
    return int(args.func(args, settings))


if __name__ == "__main__":
    raise SystemExit(main())
