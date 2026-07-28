# Roadmap, stack, and failure modes

## Phases

### Phase 0 — read-only index (current)

Four indices, screened, weighted, valued, backfilled 365 days, published with
an open methodology. **No deposits, no custody, no real money.**

Zero ToS risk, ~$2k, 2–3 weeks. And it is the only way to obtain the missing
datum: does anyone care? Nobody has published a serious OSRS index, so if the
audience does not materialise in three months, the rest of the analysis is
moot and months of work were saved.

Done:

- [x] Rank-based membership with entry/exit buffers (`membership.py`)
- [x] Three-mode valuation: inception / revalue / review (`pipeline.py`)
- [x] Plain-text store of record; SQLite reduced to a rebuildable cache
- [x] Historical replay so the index launches with a series, labelled `sim`
- [x] Static site with charts, constituents, exclusions and attack costs
- [x] Twice-daily publish via GitHub Actions to GitHub Pages
- [x] Public JSON per index — `site/data/<INDEX_ID>.json` is the API

Remaining:

- [ ] Rolling 30-day screening windows to replace single-snapshot thresholds
- [ ] Staleness alerting when a scheduled run is missed
- [ ] Backfill hourly bars so NAV runs on `/1h` from a cold clone
- [ ] Corporate action handling exercised against a real Jagex update
- [ ] A change log page rendering `corporate_action` rows

### Phase 1 — virtual portfolio (3–6 months)

Paper trading and a leaderboard. Still zero gold, still zero risk. This is
the demand test.

### Phase 2 — conditional, and probably never

Gold-only positions with manual settlement. **All four conditions must hold
simultaneously**, per [feasibility.md §8](feasibility.md):

1. ≥500 monthly active users on the virtual portfolio after 3 months
2. AUM capped at 2B gp (~$1,700) — structurally, permanently
3. Zero in-game automation; lots ≤50M gp; one identified natural person
4. Gold only — no real money in, out, via token, or via intermediary

Conditions 2 and 3 guarantee this is never a business. That is deliberate.

### Phase 3

Not recommended and not specified. Any real-money flow changes both the
nature of the project and the nature of the risk.

---

## Stack

| Layer | Choice | Why |
|---|---|---|
| Collector | Python 3.11+, stdlib only | `git clone && python3 -m osrs_index collect` must work with nothing installed |
| Storage | SQLite + WAL | At Phase 0 volumes the difference from Postgres is unmeasurable; the setup cost is not |
| Compute | stdlib `statistics` | The NAV estimator is ~150 lines. numpy would be a dependency for nothing |
| API | static JSON | `site/data/*.json` is the API; nothing to keep alive |
| Site | 3 hand-written files | No framework, no build step, no CDN |
| Charts | Lightweight Charts, vendored | 182 KB, Apache 2.0, no external request |
| Scheduling | GitHub Actions cron | Free on public repos; Airflow at this scale is cosplay |
| Hosting | GitHub Pages | Data, compute and hosting in one place, no extra account |

**When to move off SQLite:** past a year of 5m bars, or once multiple indices
need concurrent writers. Move `price_bar` to a TimescaleDB hypertable — the
schema is deliberately portable and every query is plain SQL.

**Sizing.** Screening to ~2,500 liquid items and storing 5m bars: ~720k
rows/day, ~36 MB/day, **~13 GB/year**. A $20/month VPS is ample.

---

## Data model

```sql
item(id, name, members, buy_limit, ge_value, highalch, lowalch,
     first_seen, last_seen, is_active)

price_bar(item_id, ts, step, avg_high, avg_low, vol_high, vol_low)
    -- step in {5m, 1h, 6h, 24h}. The ONLY sanctioned NAV input.

price_tick(item_id, ts, high, high_time, low, low_time)
    -- /latest. Microstructure research and anomaly detection only.
    -- Never read by the valuation path.

index_def(index_id, name, description, method, rebalance_rule,
          base_level, inception_ts)

index_member(index_id, item_id, effective_from, effective_to,
             target_weight, units, was_capped)

index_value(index_id, ts, level, divisor, basket_value_gp,
            n_members, n_stale_members, quality)

corporate_action(id, index_id, ts, kind, item_id, note,
                 divisor_before, divisor_after)
    -- kind in {rebalance, add, remove, freeze, jagex_update, supply_shock}

collection_run(id, job, started_at, finished_at, rows, ok, error)
```

Two choices worth defending:

**`price_tick` is stored but structurally excluded from valuation.** The
separation is enforced in `nav.py`, which never queries the table. Keeping
the data is useful; letting it near a NAV is not.

**`quality` and `n_stale_members` are persisted on every level.** The index
history carries its own data-quality record. Any auditor can see which
stretches were computed from carried-forward prices. An index that hides that
cannot be trusted to settle anything.

---

## Jobs

| Job | Frequency | Purpose |
|---|---|---|
| `sync_mapping` | daily | Detect item additions and removals |
| `collect_latest` | 60 s | Microstructure, anomaly detection |
| `collect_aggregate 5m` | 5 min (+30 s lag) | Fine bars |
| `collect_aggregate 1h` | hourly | **NAV source** |
| `collect_aggregate 24h` | daily | Screening, settlement VWAP |
| `backfill_timeseries` | on demand | 365d history for new constituents |
| `build` | hourly, T+1h | Index levels |
| `screen` | weekly | Recompute eligibility |

Every job is **idempotent** — bars upsert on `(item_id, ts, step)`. The
ability to replay collection from scratch is what lets you recompute a
disputed level months later, and a settlement you cannot recompute is not one
you can defend.

The `+30 s` lag on 5m collection matters: aggregate buckets publish at window
close, so polling exactly on the boundary races the publisher.

---

## Failure modes

1. **API blocked, or schema changed without notice.** The most likely
   failure. *Mitigation:* full local cache (history is recomputable offline),
   retry with backoff, staleness alerting. Note that the User-Agent policy is
   **not technically enforced** — do not mistake "my requests work" for
   "I am compliant". See [api-notes.md](api-notes.md).

2. **v1 → v2 migration breaking `/timeseries`.** v1 takes `timestep=`; v2
   takes `lookback=` and rejects `timestep` outright. *Mitigation:*
   `ClientConfig.base` is pinned, never inferred, and `timeseries()` raises
   rather than sending the wrong parameter to the wrong version.

3. **Empty or crossed buckets on thin constituents.** Not exceptional — 10%
   of items show crossed prices over 24h. *Mitigation:* handled as the
   nominal path, with quality flags propagated into the published level.

4. **A Jagex update changing items.** No API signals it. Item removal shows up
   only as a disappearance from `/mapping`, which `sync_mapping` logs as a
   warning. Everything else needs manual patch-note watching. **This is an
   unsolved operational dependency on a human.**

5. **A botched divisor adjustment** silently breaking historical continuity —
   the failure that makes every downstream backtest worthless. *Mitigation:*
   `continuity_check()` fails the job when a level jumps >5% across a
   rebalance.

6. **Single-snapshot thresholds.** Current screening constants derive from one
   moment on one afternoon. Until rolling windows land, treat every threshold
   as provisional. This is the largest known weakness in the repo.

7. **The schedule going quiet.** GitHub disables scheduled workflows after 60
   days without repository activity, and it is not clearly documented whether
   a `GITHUB_TOKEN` commit resets that clock. *Mitigation:* watch for a
   stalled `updated` timestamp on the site and trigger the workflow manually
   from the Actions tab, which definitely resets it. Actions cron is also
   best-effort — delays of 5–30 minutes are routine and dropped runs happen.

8. **Concurrent publishes.** Two runs committing `data/` could race. The
   workflow serialises on a `concurrency` group and rebases before pushing.
   With text files a collision is a resolvable conflict; it is one more
   reason the store of record is not a binary database.
