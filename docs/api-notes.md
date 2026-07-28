# API notes

Everything here was verified against the live API by direct probe on
**2026-07-27**, not read from documentation. Where the wiki's own docs and the
live behaviour disagree, both are recorded.

## Endpoints

Base: `https://prices.runescape.wiki/api/v1/osrs`

| Endpoint | Response | Verified |
|---|---|---|
| `/mapping` | bare array, `{id, name, examine, members, limit, value, highalch, lowalch, icon}` | 200, **4,591 items**, 850 KB |
| `/latest` | `{"data": {id: {high, highTime, low, lowTime}}}` | 200, **4,454 items**, 337 KB |
| `/5m` `/1h` `/6h` `/24h` | `{"data": {id: {avgHighPrice, highPriceVolume, avgLowPrice, lowPriceVolume}}}` | 200 all four |
| `/5m?timestamp=` | historical bucket | 200 |
| `/timeseries?id=&timestep=` | `{"data": [{timestamp, avgHighPrice, avgLowPrice, highPriceVolume, lowPriceVolume}]}` | 200, **365 points** at `timestep=24h` |

### Findings that are not in the documentation

**`/6h` exists.** It is absent from the endpoint table on the wiki page but
returns 200 with the same shape as the other aggregates. `/4h`, `/1d` and
`/7d` all return 404.

**v1 and v2 take different parameter names on `/timeseries`.** This is the
single most likely way to break a collector:

```
v1  /timeseries?id=4151&timestep=24h   -> 200, {"data": [...]}
v2  /timeseries?id=4151&timestep=5m    -> {"error":"lookback must be a valid value"}
v2  /timeseries?id=4151&lookback=7d    -> 200, {data, itemId, startTimestamp, endTimestamp, timestep}
```

The wiki documents the v2 contract. A lot of community code targets v1. The
v2 response is an envelope with metadata, not a bare `{"data": ...}`.
`ClientConfig.base` is therefore pinned and never inferred, and
`PricesClient.timeseries()` refuses to run against a v2 base rather than
silently sending the wrong parameter.

v2 also warns that the returned timestep "is NOT guaranteed by this API and
may change with no prior warning" — read it from the response envelope, do
not assume it.

**The User-Agent policy is not technically enforced.** The wiki asks clients
to set a descriptive UA and lists blocked agents. Live probes:

```
no User-Agent header at all       -> HTTP 200
User-Agent: python-requests/2.31.0 -> HTTP 200
```

Both are nominally blocked. They are not. This repo enforces the policy
client-side anyway (`validate_user_agent`), including a requirement that the
UA carry a contact route. The reasoning is not compliance theatre: this is a
volunteer-run service, the cost of being null-routed is the entire product,
and an admin who can reach you will email before they block you.

**Rate limits.** None stated explicitly. The wiki's stated threshold for
intervention is "multiple large queries per second for a sustained period."
This client floors requests at 250 ms apart, which is orders of magnitude
below that. Polling `/latest` every 60 s is 1,440 requests/day — trivial.

## What the prices actually are

This is the part that determines whether a NAV is defensible.

`high` is the price of the **most recent instant-buy** transaction — someone
paying the standing offer. `low` is the most recent **instant-sell**. They are
two independent event streams with independent timestamps.

They are **not** a bid/ask quote, **not** an order book mid, and **not** a
single last-trade price. There is no order book data in this API at all.

### Measured consequences

**Crossed prices are normal, not errors.** `high < low` on:

| Source | Crossed | Sample |
|---|---|---|
| `/latest` | **15.6%** | 692 / 4,450 |
| `/24h` | 10.1% | 382 / 3,773 |
| `/5m` | 5.3% | 38 / 718 |

A falling market routinely prints an instant-buy below an earlier
instant-sell. Pure essence (2 gp) showed a **−66.7%** spread over 24h, which
is integer rounding, not a market.

**`/latest` is badly stale.** Age of the *older* of the two legs:

| percentile | age |
|---|---|
| median | **37 min** |
| p75 | 3.0 h |
| p90 | **14.8 h** |
| p99 | 38.2 h |

**Instantaneous depth is mostly an illusion.** In the last 5-minute bucket
sampled, 1,806 items traded at all but only **718 had both sides print** —
out of 4,591 items in `/mapping`. Under 16% of the universe has a fresh
two-sided price at any moment.

### The rule this forces

**`/latest` cannot be used to value anything.** It is collected into
`price_tick` for microstructure research and anomaly detection, and the NAV
path in `nav.py` never reads that table. Valuation uses volume-weighted
aggregates over `/1h` with outlier handling; settlement uses `/24h`.

An index built on `/latest` can be moved by a single transaction. See
[`manipulation.py`](../src/osrs_index/manipulation.py) for what that costs
(spoiler: on Rune platebody, 3,340 gp — about a third of a US cent).

## Market shape

From the same snapshot, `/24h`, 3,773 items with both sides printing:

| 24h gp volume | Items | Median spread |
|---|---|---|
| > 1M | 2,489 | 11.6% (in the 1–10M band) |
| > 10M | 1,623 | 6.1% (10–100M band) |
| > 100M | 934 | 3.2% (100M–1B band) |
| > 1B | **392** | 1.9% |
| > 10B | **108** | 1.6% |

Total 24h turnover ≈ **7.16T gp**. The **top 20 items are 47.6%** of it.

Spread is essentially a deterministic function of liquidity, and the tail is
unusable: below 1M gp/day the median spread is 23.7% and the 90th percentile
is 112%.

## Reproducing these numbers

```bash
export OSRS_INDEX_USER_AGENT="osrs-assets/0.1 - @yourhandle on Discord"
python3 -m osrs_index collect
python3 -m osrs_index status
```

Figures will differ from those above — they are a single snapshot, taken
once, at one moment on a Monday afternoon UTC. Intraday volume plausibly
varies by a factor of 2–3 between trough and peak. Nothing here has been
averaged over 30 days, and it should be before any of it drives a decision.
