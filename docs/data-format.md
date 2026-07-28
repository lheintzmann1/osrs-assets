# Data format

Everything under `data/` is committed plain text and is the **store of
record**. The SQLite database is a cache: delete it, run
`python3 -m osrs_index restore`, and nothing is lost.

## Why not commit the SQLite file

Measured, not assumed. The real database, fourteen daily updates each
committed, then `git gc`:

| | packed |
|---|---|
| SQLite blob | 3.10 MiB |
| Equivalent NDJSON | 1.00 MiB |

Only ~3×, so the size argument is weaker than it first looks — git delta-
compresses SQLite pages better than people expect. The decisive reasons are
the other ones:

- a diff on `data/bars/24h/2026-07.ndjson` is reviewable; a binary page diff is not
- two collectors racing produce a text conflict you can resolve, not a corrupt database
- the browser can `fetch()` JSON directly; serving SQLite means shipping ~1 MB of sql.js WASM to read a few hundred KB
- `grep` works

## Layout

```
data/
  items.json                    item reference: id, name, members, limit, value
  bars/24h/YYYY-MM.ndjson       daily price bars, month-sharded
  history/<INDEX_ID>.ndjson     append-only index level history
  composition/<INDEX_ID>.json   the live basket: members, units, divisor
```

Bars are sharded by month so that old months stop changing and git stops
re-storing them. A single append-only file that grows forever means every
commit rewrites one ever-larger blob.

## `bars/24h/*.ndjson`

One JSON array per line. **Positional, and the column order must not
change:**

```
[item_id, ts, avg_high, avg_low, vol_high, vol_low]
```

| Field | Meaning |
|---|---|
| `item_id` | OSRS item id |
| `ts` | Unix timestamp of the bucket start (UTC midnight) |
| `avg_high` | Volume-weighted mean **instant-buy** price, or `null` |
| `avg_low` | Volume-weighted mean **instant-sell** price, or `null` |
| `vol_high` | Units bought at the offer |
| `vol_low` | Units sold at the bid |

Arrays rather than objects because repeating six key names across ~1.5M rows
a year triples the file for no added clarity.

Rows are sorted by `(item_id, ts)`. That is load-bearing: an unsorted dump
produces a spurious diff on every run and makes the git history useless.

`avg_high < avg_low` is **normal, not corrupt** — the two legs are
asynchronous event streams and a falling market routinely crosses them.
Roughly 10% of items cross on any given day. See [api-notes.md](api-notes.md).

## `history/<INDEX_ID>.ndjson`

One JSON object per line, sorted by `ts`:

```json
{"ts":1785196800,"date":"2026-07-26","level":1141.8664,"divisor":1000.0,
 "basket_gp":1141866.4,"n":10,"stale":0,"q":"ok","sim":true}
```

| Field | Meaning |
|---|---|
| `level` | Published index level |
| `divisor` | Divisor in force; changes only on membership events |
| `basket_gp` | Basket value in gp before dividing |
| `n` | Constituent count |
| `stale` | Members priced from a carried-forward observation |
| `q` | `ok` / `degraded` / `stale` / `missing` |
| `sim` | **Present and `true` only for backtested levels** |

### The `sim` flag

`sim` marks a level produced by `replay.py` from stored daily bars rather
than observed live. It is not cosmetic. Splicing a backtest and a live record
into one indistinguishable line is how a project that intends to be honest
ends up publishing a performance claim, so the flag travels all the way to
the chart, where simulated history renders dotted and live history solid.

A level with `q == "missing"` is never written. An unpriced constituent is
arithmetically identical to marking it at zero, which craters the basket by
that member's weight — this produced a spurious −100% drawdown before it was
caught.

## `composition/<INDEX_ID>.json`

The live basket. **This file is what makes the index continuous**, and it is
the one people forget.

```json
{"index_id":"OSRS-MELEE","effective_from":1784592000,"divisor":1000.0,
 "members":[{"id":13652,"name":"Dragon claws","weight":0.125,
             "units":0.0036690514,"capped":false}]}
```

CI throws the SQLite cache away on every run, so anything `data/` does not
carry is gone. Units and divisor *are* the index: rebuilding from bars alone
re-seeds the divisor, every scheduled run becomes an inception, and the level
resets to `base_level` twice a day. Nobody catches it quickly, because
1000.00 is an entirely plausible number to see on a chart.

`restore` replays this file into `index_member`, and the committed history
into `index_value`, so the next `build()` finds both the units and the
divisor it needs to run in `revalue` mode. Restoring one without the other
silently re-seeds. Pinned by
`test_index_survives_a_thrown_away_database`.

## Site payload

`site/data/` is **generated**, not committed. `publish` regenerates it and CI
serves it from the Pages artifact. `manifest.json` lists the indices;
`<INDEX_ID>.json` carries series, constituents, screen rejections, the
membership rationale and the manipulation estimate — everything one page
needs in a single fetch.

## Growth

Screening to the four candidate pools, at 24h granularity:

| | per day | per year |
|---|---|---|
| Bars (candidate pools only) | ~4 KB | ~1.5 MB |
| Bars (full universe, if enabled) | ~105 KB | ~38 MB |
| Level history, 4 indices | ~640 B | ~230 KB |

Comfortable indefinitely at pool scope. If you widen collection to the full
universe, revisit in a couple of years — or move `price_bar` to a
TimescaleDB hypertable and keep only the published subset in git.
