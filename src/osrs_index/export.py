"""Plain-text artefacts: the repo is the store of record, SQLite is a cache.

Why not commit the SQLite file
------------------------------
Measured, not assumed. Fourteen daily updates of the real database, each
committed, then `git gc`:

    SQLite blob   3.10 MiB packed
    NDJSON text   1.00 MiB packed

Only ~3x, so the size argument is weaker than it first looks -- git does
delta-compress SQLite pages reasonably well. The real reasons are the other
ones:

  * a diff on `data/bars/24h/2026-07.ndjson` is reviewable; a diff on a
    binary page file is not
  * two collectors racing produce a text merge conflict you can resolve, and
    a corrupt database you cannot
  * the browser can `fetch()` JSON directly, where serving SQLite means
    shipping ~1 MB of sql.js WASM to read a few hundred KB of data
  * `grep` works

So: NDJSON and JSON in `data/`, committed. SQLite is rebuilt from it and can
be deleted at any time without losing anything.

Sharding
--------
Bars are sharded by month. A single append-only file that grows forever means
every commit rewrites one ever-larger blob; month shards bound it, and old
months stop changing entirely so git stops storing them again.
"""

from __future__ import annotations

import json
import time
from collections.abc import Iterable, Sequence
from datetime import UTC, datetime
from pathlib import Path

from .models import Bar, IndexLevel
from .storage import Store

#: Only these steps are published. 5m and 1h are working data -- high volume,
#: low durable value, and recomputable by anyone who wants them.
PUBLISHED_STEPS = ("24h",)


def _month_key(ts: int) -> str:
    return datetime.fromtimestamp(ts, UTC).strftime("%Y-%m")


def _iso(ts: int) -> str:
    return datetime.fromtimestamp(ts, UTC).strftime("%Y-%m-%d")


# ------------------------------------------------------------------- bars


def export_bars(store: Store, data_dir: Path, step: str = "24h") -> dict[str, int]:
    """Write every stored bar of `step` to month-sharded NDJSON.

    Rows are `[item_id, ts, avg_high, avg_low, vol_high, vol_low]` -- a list,
    not an object, because repeating six key names across ~1.5M rows a year
    triples the file for no added clarity. The column order is documented
    here and in docs/data-format.md, and it must not change.

    Sorted by (item_id, ts) so the file is deterministic: an unsorted dump
    produces a spurious diff on every run and makes the git history useless.
    """
    shards: dict[str, list[list]] = {}
    rows = store.conn.execute(
        "SELECT item_id, ts, avg_high, avg_low, vol_high, vol_low "
        "FROM price_bar WHERE step = ? ORDER BY item_id, ts",
        (step,),
    )
    for row in rows:
        shards.setdefault(_month_key(row["ts"]), []).append(
            [row["item_id"], row["ts"], row["avg_high"], row["avg_low"],
             row["vol_high"], row["vol_low"]]
        )

    out_dir = data_dir / "bars" / step
    out_dir.mkdir(parents=True, exist_ok=True)
    written = {}
    for month, entries in sorted(shards.items()):
        entries.sort(key=lambda r: (r[0], r[1]))
        payload = "\n".join(json.dumps(e, separators=(",", ":")) for e in entries) + "\n"
        path = out_dir / f"{month}.ndjson"
        # Skip identical writes so unchanged months stay untouched in git.
        if not path.exists() or path.read_text() != payload:
            path.write_text(payload)
        written[month] = len(entries)
    return written


def import_bars(store: Store, data_dir: Path, step: str = "24h") -> int:
    """Rebuild stored bars from committed NDJSON. Inverse of `export_bars`."""
    in_dir = data_dir / "bars" / step
    if not in_dir.exists():
        return 0
    total = 0
    for path in sorted(in_dir.glob("*.ndjson")):
        bars: list[Bar] = []
        for line in path.read_text().splitlines():
            if not line.strip():
                continue
            item_id, ts, high, low, vol_high, vol_low = json.loads(line)
            bars.append(Bar(item_id, ts, step, high, low, vol_high, vol_low))
        total += store.insert_bars(bars)
    return total


# ------------------------------------------------------------------ items


def export_items(store: Store, data_dir: Path) -> int:
    """Item reference: needed to resolve candidate names without an API call."""
    rows = [
        {
            "id": item.id,
            "name": item.name,
            "members": item.members,
            "limit": item.buy_limit,
            "value": item.ge_value,
        }
        for item in sorted(store.items(), key=lambda i: i.id)
    ]
    path = data_dir / "items.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(rows, separators=(",", ":")) + "\n")
    return len(rows)


def import_items(store: Store, data_dir: Path) -> int:
    from .models import Item

    path = data_dir / "items.json"
    if not path.exists():
        return 0
    payload = json.loads(path.read_text())
    return store.upsert_items(
        [
            Item(
                id=r["id"], name=r["name"], members=r["members"],
                buy_limit=r["limit"], ge_value=r["value"],
            )
            for r in payload
        ],
        int(time.time()),
    )


# --------------------------------------------------------------- history


def append_level(data_dir: Path, level: IndexLevel, simulated: bool = False) -> bool:
    """Append one observation to an index's append-only history.

    Idempotent on timestamp: re-running the job for a timestamp already
    recorded rewrites that line rather than adding a duplicate. Without this
    a retried CI run silently doubles a day.

    `simulated` marks a level produced by replay.py rather than observed
    live. The flag travels all the way to the chart. Splicing backtested and
    observed levels into one indistinguishable line is how an honest project
    accidentally publishes a performance claim.
    """
    path = data_dir / "history" / f"{level.index_id}.ndjson"
    path.parent.mkdir(parents=True, exist_ok=True)

    record = {
        "ts": level.ts,
        "date": _iso(level.ts),
        "level": round(level.level, 4),
        "divisor": round(level.divisor, 6),
        "basket_gp": round(level.basket_value_gp, 2),
        "n": level.n_members,
        "stale": level.n_stale_members,
        "q": level.quality.value,
    }
    if simulated:
        record["sim"] = True
    line = json.dumps(record, separators=(",", ":"))

    existing = path.read_text().splitlines() if path.exists() else []
    kept = [ln for ln in existing if ln.strip() and json.loads(ln)["ts"] != level.ts]
    changed = len(kept) != len(existing) or line not in existing
    kept.append(line)
    kept.sort(key=lambda ln: json.loads(ln)["ts"])
    path.write_text("\n".join(kept) + "\n")
    return changed


def read_history(data_dir: Path, index_id: str) -> list[dict]:
    path = data_dir / "history" / f"{index_id}.ndjson"
    if not path.exists():
        return []
    return [json.loads(ln) for ln in path.read_text().splitlines() if ln.strip()]


# ------------------------------------------------------------ composition


def export_composition(store: Store, data_dir: Path, index_id: str) -> bool:
    """Persist the live basket: members, units, weights, divisor.

    Without this the index resets to `base_level` on every CI run and nobody
    notices for a while, because a level of exactly 1000.00 looks like a
    perfectly ordinary starting value.

    The units and the divisor ARE the index. Bars and history alone are not
    enough to continue one: rebuilding from bars re-seeds the divisor, which
    is the flat-line bug arriving through a different door -- this time via
    the deploy pipeline rather than via `build()`.
    """
    rows = store.current_members(index_id)
    if not rows:
        return False
    levels = store.levels(index_id, limit=1)

    payload = {
        "index_id": index_id,
        "effective_from": rows[0]["effective_from"],
        "divisor": levels[0]["divisor"] if levels else None,
        "members": [
            {
                "id": row["item_id"],
                "name": row["name"],
                "weight": round(row["target_weight"], 8),
                "units": row["units"],
                "capped": bool(row["was_capped"]),
            }
            for row in sorted(rows, key=lambda r: -r["target_weight"])
        ],
    }
    path = data_dir / "composition" / f"{index_id}.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=1) + "\n")
    return True


def import_composition(store: Store, data_dir: Path) -> int:
    """Restore live baskets and their published levels.

    Both halves are needed. `index_member` carries the units; `index_value`
    carries the divisor, and `build()` in revalue mode reads the divisor from
    the most recent level. Restoring one without the other silently re-seeds
    the divisor.
    """
    in_dir = data_dir / "composition"
    if not in_dir.exists():
        return 0

    restored = 0
    for path in sorted(in_dir.glob("*.json")):
        payload = json.loads(path.read_text())
        index_id = payload["index_id"]
        store.set_members(
            index_id,
            payload["effective_from"],
            [
                (m["id"], m["weight"], m["units"], m["capped"])
                for m in payload["members"]
            ],
        )

        # Replay the committed history into index_value so the divisor and the
        # previous level are both available to the next build.
        for record in read_history(data_dir, index_id):
            store.conn.execute(
                """
                INSERT INTO index_value (index_id, ts, level, divisor, basket_value_gp,
                                         n_members, n_stale_members, quality)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(index_id, ts) DO NOTHING
                """,
                (
                    index_id, record["ts"], record["level"],
                    record.get("divisor") or payload.get("divisor") or 1.0,
                    record.get("basket_gp", 0.0), record.get("n", 0),
                    record.get("stale", 0), record.get("q", "ok"),
                ),
            )
        store.conn.commit()
        restored += 1
    return restored


# ------------------------------------------------------------ site payload


def _attack_payload(result) -> dict | None:
    attack = result.cheapest_attack
    if attack is None:
        return None
    return {
        "item": attack.item_name,
        "nav_window": attack.nav_window,
        "index_move": attack.index_move,
        "required_item_move": round(attack.required_item_move, 4),
        "cost_gp": round(attack.total_cost_gp),
        "cost_usd": round(attack.total_cost_usd, 2),
        "accounts": attack.accounts_required,
        "breakeven_gp": round(attack.breakeven_position_gp),
        "beyond_linear_model": attack.beyond_linear_model,
    }


def build_site_payload(result, history: Sequence[dict], prices: dict[int, float]) -> dict:
    """Everything one index page needs, in a single fetch.

    Deliberately includes `excluded` and the attack estimate. A published
    index that shows only its holdings is unfalsifiable; the useful questions
    are what it left out, why, and what it would cost to move.
    """
    spec = result.spec

    # One point per date, observed beating simulated.
    #
    # The replay stamps a level at each daily bar's timestamp while the live
    # job stamps "now", so the handover day produces two records with
    # different timestamps and the same date. Lightweight Charts requires
    # strictly ascending unique times and simply throws on a duplicate, so
    # the chart would go blank on exactly the day the index went live.
    by_date: dict[str, dict] = {}
    for record in sorted(history, key=lambda h: h["ts"]):
        existing = by_date.get(record["date"])
        if existing is None or existing.get("sim") or not record.get("sim"):
            by_date[record["date"]] = record
    ordered = [by_date[d] for d in sorted(by_date)]

    series = [{"time": h["date"], "value": h["level"]} for h in ordered]
    simulated_through = max((h["date"] for h in ordered if h.get("sim")), default=None)
    #: Index into `series` where observed data begins, so the chart can render
    #: the backtest and the live record as visibly different things.
    split_at = next(
        (i for i, h in enumerate(ordered) if not h.get("sim")), len(ordered)
    )

    change_24h = None
    if len(series) >= 2:
        previous = series[-2]["value"]
        if previous:
            change_24h = series[-1]["value"] / previous - 1

    membership = result.membership
    return {
        "index_id": spec.index_id,
        "name": spec.name,
        "description": spec.description,
        "updated": result.level.ts if result.level else None,
        "level": round(result.level.level, 2) if result.level else None,
        "change_24h": round(change_24h, 6) if change_24h is not None else None,
        "quality": result.level.quality.value if result.level else "missing",
        "mode": result.mode,
        "divisor": round(result.divisor, 6),
        "base_level": spec.base_level,
        "weighting": spec.weighting,
        "series": series,
        "simulated_through": simulated_through,
        "observed_from": next((h["date"] for h in ordered if not h.get("sim")), None),
        "split_at": split_at,
        "constituents": [
            {
                "id": c.item_id,
                "name": c.name,
                "weight": round(c.target_weight, 6),
                "units": round(c.units, 8),
                "price": round(prices.get(c.item_id, 0.0), 2),
                "capped": c.capped,
            }
            for c in sorted(result.constituents, key=lambda c: -c.target_weight)
        ],
        "excluded": [
            {"id": r.item_id, "name": r.name, "reasons": list(r.reasons),
             "gp_volume_24h": round(r.gp_volume_24h)}
            for r in sorted(result.excluded, key=lambda r: -r.gp_volume_24h)
        ],
        "ranked": [
            {"rank": c.rank, "id": c.item_id, "name": c.name,
             "liquidity_gp": round(c.liquidity_gp), "eligible": c.eligible}
            for c in (membership.ranked if membership else [])
        ],
        "membership": {
            "undersized": membership.undersized if membership else False,
            "added": membership.added if membership else [],
            "removed": membership.removed if membership else [],
            "rationale": {str(k): v for k, v in (membership.rationale or {}).items()}
            if membership
            else {},
        },
        "attack": _attack_payload(result),
        "creation_capacity_gp": round(result.creation_capacity_gp)
        if result.creation_capacity_gp
        else None,
        "unresolved": result.unresolved,
    }


def write_site(payloads: Iterable[dict], site_dir: Path) -> Path:
    """Write per-index JSON plus a manifest into the site's data directory."""
    out = site_dir / "data"
    out.mkdir(parents=True, exist_ok=True)

    manifest = {"generated": int(time.time()), "indices": []}
    for payload in payloads:
        (out / f"{payload['index_id']}.json").write_text(
            json.dumps(payload, separators=(",", ":")) + "\n"
        )
        manifest["indices"].append(
            {
                "index_id": payload["index_id"],
                "name": payload["name"],
                "level": payload["level"],
                "change_24h": payload["change_24h"],
                "quality": payload["quality"],
                "n_members": len(payload["constituents"]),
            }
        )
    manifest["indices"].sort(key=lambda entry: entry["index_id"])
    (out / "manifest.json").write_text(json.dumps(manifest, indent=1) + "\n")
    return out
