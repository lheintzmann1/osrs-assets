"""Collection jobs.

Every job is idempotent: bars upsert on (item_id, ts, step), so re-running a
job overwrites rather than duplicates. This matters more than it sounds --
the ability to replay collection from scratch is what lets you recompute a
disputed index level months later, and a settlement mechanism you cannot
recompute is not one you can defend.
"""

from __future__ import annotations

import logging
import time
from collections.abc import Sequence

from .client import AGGREGATE_STEPS, PricesClient, Timestep
from .models import Bar, Item
from .storage import Store, iter_bars_from_payload

log = logging.getLogger(__name__)

#: Seconds. Aggregate buckets are published at the close of the window, so
#: polling exactly on the boundary races the publisher.
BUCKET_PUBLISH_LAG = 30

_STEP_SECONDS: dict[str, int] = {"5m": 300, "1h": 3600, "6h": 21600, "24h": 86400}


def bucket_start(ts: int, step: str) -> int:
    """Align a timestamp down to its bucket boundary."""
    size = _STEP_SECONDS[step]
    return (ts // size) * size


def sync_mapping(client: PricesClient, store: Store) -> int:
    """Refresh the item reference and detect additions/removals.

    Run daily. A Jagex update that adds or removes tradeable items shows up
    here first and nowhere else -- there is no changelog endpoint.
    """
    now = int(time.time())
    run_id = store.start_run("sync_mapping", now)
    try:
        payload = client.mapping()
        items = [
            Item(
                id=entry["id"],
                name=entry["name"],
                members=bool(entry.get("members")),
                buy_limit=entry.get("limit"),
                ge_value=entry.get("value"),
                highalch=entry.get("highalch"),
                lowalch=entry.get("lowalch"),
            )
            for entry in payload
        ]
        count = store.upsert_items(items, now)
        removed = store.deactivate_missing_items([i.id for i in items], now)
        if removed:
            log.warning(
                "%d item(s) disappeared from /mapping -- check for a Jagex removal "
                "before the next index computation",
                removed,
            )
        store.finish_run(run_id, int(time.time()), count, True)
        return count
    except Exception as exc:
        store.finish_run(run_id, int(time.time()), 0, False, str(exc))
        raise


def collect_aggregate(
    client: PricesClient, store: Store, step: Timestep, timestamp: int | None = None
) -> int:
    """Fetch one aggregate bucket for every item and persist it."""
    now = int(time.time())
    run_id = store.start_run(f"collect_{step}", now)
    try:
        payload = client.aggregate(step, timestamp)
        ts = timestamp if timestamp is not None else bucket_start(now - BUCKET_PUBLISH_LAG, step)
        bars = list(iter_bars_from_payload(payload, ts, step))
        # Buckets where neither side traded carry no information and would
        # otherwise dominate the table: ~40% of the universe on a 5m bucket.
        bars = [b for b in bars if b.avg_high is not None or b.avg_low is not None]
        count = store.insert_bars(bars)
        store.finish_run(run_id, int(time.time()), count, True)
        log.info("collected %d %s bars at ts=%d", count, step, ts)
        return count
    except Exception as exc:
        store.finish_run(run_id, int(time.time()), 0, False, str(exc))
        raise


def collect_latest(client: PricesClient, store: Store) -> int:
    """Snapshot the last instant-buy/instant-sell prints.

    Stored for anomaly detection only. If you find yourself reading
    price_tick from a valuation path, stop: median staleness of the older leg
    is 37 minutes and 15.6% of entries are crossed.
    """
    now = int(time.time())
    run_id = store.start_run("collect_latest", now)
    try:
        payload = client.latest()
        ticks = [
            (
                int(raw_id),
                now,
                entry.get("high"),
                entry.get("highTime"),
                entry.get("low"),
                entry.get("lowTime"),
            )
            for raw_id, entry in payload.items()
        ]
        count = store.insert_ticks(ticks)
        store.finish_run(run_id, int(time.time()), count, True)
        return count
    except Exception as exc:
        store.finish_run(run_id, int(time.time()), 0, False, str(exc))
        raise


def backfill_timeseries(
    client: PricesClient, store: Store, item_ids: Sequence[int], step: str = "24h"
) -> int:
    """Pull up to 365 historical buckets per item.

    This is how a new constituent gets enough history to be screened and how
    the index gets a backtest at all. Rate-limited by the client's own
    throttle; 100 items at 24h granularity takes about 30 seconds.
    """
    now = int(time.time())
    run_id = store.start_run("backfill_timeseries", now)
    total = 0
    try:
        for item_id in item_ids:
            points = client.timeseries(item_id, step=step)  # type: ignore[arg-type]
            bars = [
                Bar(
                    item_id=item_id,
                    ts=point["timestamp"],
                    step=step,
                    avg_high=point.get("avgHighPrice"),
                    avg_low=point.get("avgLowPrice"),
                    vol_high=point.get("highPriceVolume") or 0,
                    vol_low=point.get("lowPriceVolume") or 0,
                )
                for point in points
                if point.get("avgHighPrice") is not None or point.get("avgLowPrice") is not None
            ]
            total += store.insert_bars(bars)
            log.info("backfilled %d %s bars for item %d", len(bars), step, item_id)
        store.finish_run(run_id, int(time.time()), total, True)
        return total
    except Exception as exc:
        store.finish_run(run_id, int(time.time()), total, False, str(exc))
        raise


def collect_all(client: PricesClient, store: Store) -> dict[str, int]:
    """One-shot bootstrap: mapping plus every aggregate window."""
    results = {"mapping": sync_mapping(client, store)}
    for step in AGGREGATE_STEPS:
        results[step] = collect_aggregate(client, store, step)
    results["latest"] = collect_latest(client, store)
    return results
