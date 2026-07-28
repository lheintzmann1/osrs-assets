# Index methodology

This document is the contract. If the code and this document disagree, the
document is right and the code is a bug.

## 1. Universe

### 1.1 Eligibility

An item is eligible for a basket if it satisfies **all** of:

| Criterion | Default | Rationale |
|---|---|---|
| 24h gp volume | ≥ 1,000,000,000 | Below 1B/day median spread exceeds 1.9% and attack cost collapses. |
| Unit price | ≥ 50 gp | Integer rounding dominates below this. Also the GE tax exemption threshold, so microstructure genuinely differs. |
| Median spread (30d) | ≤ 5% | Measured over uncrossed two-sided buckets only. |
| Two-sided bucket ratio | ≥ 90% | An item that regularly prints only one side has no reliable level. |
| History | ≥ 90 days | Post-release items are in price discovery. |

Volume is measured over a representative 24h bucket; spread and
two-sidedness over the full 30-day lookback.

### 1.2 Physical replicability — a separate question

`limit × price` per 4h window measures whether a basket can be **assembled**,
not whether it can be **measured**. It is disabled by default
(`require_physical_replicability=False`) and reported alongside every screen
regardless.

This distinction is easy to get wrong and the repo got it wrong first. A
global 20M gp per-window floor rejects **every constituent of the PvM
consumables basket** — the most liquid of the four — because consumables are
cheap items with generous buy limits. Blood rune turns over 21.8B gp/day and
caps at 8.5M gp per account per window. It is a perfectly good index member
and a terrible fund holding.

Enable the criterion only when screening for a physically replicated product.

### 1.3 What the screen costs each basket

Run against live data on 2026-07-28 with default parameters:

| Basket | Eligible / screened | Rejected |
|---|---|---|
| Melee weapons | 10 / 12 | Dragon scimitar (0.46B), Abyssal dagger (0.43B) |
| Body slot | 8 / 10 | Dragon platebody (0.66B), Torag's platebody (0.20B) |
| PvM consumables | 9 / 9 | — |
| Raw materials | 7 / 11 | Green dragonhide, Bow string, Yew logs, Iron ore |

Raw materials loses 4 of 11 seeds, which strips it of most of its thematic
coverage. That is recorded as a negative result rather than fixed by
loosening the screen.

## 2. Membership

### 2.1 Candidate pools are curated

The API has no item taxonomy — nothing in `/mapping` says "this is a melee
weapon". So each basket's candidate pool is a human-maintained list in its
spec, deliberately larger than the target basket size. **Curation decides
what the basket is about; the rules decide what it holds.** Automating the
first would mean inventing a category system and calling it data.

### 2.2 Entry and exit buffers

| Basket | Target | Enter at rank ≤ | Exit past rank | Pool |
|---|---|---|---|---|
| Melee weapons | 10 | 8 | 12 | 28 |
| Body slot | 10 | 8 | 12 | 23 |
| PvM consumables | 12 | 10 | 15 | 26 |
| Raw materials | 12 | 10 | 15 | 33 |

Candidates are ranked by 24h gp volume, ineligible ones last, ties broken on
item id. The total order is not pedantry: without it the same inputs can
yield different baskets across runs, and an index that does not reproduce
cannot be audited.

An item sitting inside the dead band is left exactly where it is, in or out.
That is the entire point — each avoided round trip is ~3.5–4% of friction
(spread plus the 2% GE sell tax) not paid.

### 2.3 Selection is a three-tier priority queue

| Tier | Who | Why |
|---|---|---|
| 0 — mandatory | eligible, rank ≤ `entry_rank` | the top of the pool is always in; no incumbent outranks it |
| 1 — incumbent | current member, not forced or buffered out | where the hysteresis lives |
| 2 — reserve | non-member in the buffer zone | fills genuine vacancies only |

Truncated at `target_size`.

**Tier 2 is what a naive reading of "enter at 8, exit at 12" gets wrong, and
this implementation got wrong first.** If entry is a hard gate, then once
ranks 1–8 are all members a vacancy at rank 9 can never be filled — no
eligible non-member ranks 8 or better. The basket shrinks monotonically and
`target_size` silently becomes a ceiling it only ever drifts below. The
buffer zone must be a priority queue favouring incumbents, not a wall.

### 2.4 Order of operations

1. **Forced exits.** An item failing the hard screen leaves regardless of
   rank and regardless of turnover caps. Holding a delisted item because the
   change budget is spent would be indefensible.
2. **Buffered exits.** Members ranked worse than `exit_rank`, worst first,
   subject to `max_buffer_exits`.
3. **Selection** by (tier, rank), truncated at `target_size`.
4. **Addition cap.** Newly added trimmed to `max_additions`, best-ranked kept.

`max_buffer_exits` is named for its actual scope. It grants a breached
incumbent one more period in the incumbent tier; it does **not** cap total
removals, because a reprieved incumbent can still lose its seat to a
better-ranked candidate — and should.

### 2.5 Reviews are scheduled, not continuous

The twice-daily job runs in `revalue` mode: units and divisor fixed, prices
move, membership untouched. Composition changes only when a review is
explicitly triggered (`publish --review`). A basket that reshuffled itself
twice a day would be a strategy, not an index.

## 3. Weighting

**Equal weight, liquidity-capped.**

Each eligible member starts at `1/N`, capped at `k × (its share of basket
turnover)` with `k = 3`. Excess weight from capped members is redistributed
equally among uncapped members, iterating because redistribution can push a
previously-fine member over its own cap. If every member ends up capped the
constraint is infeasible; weights are normalised and the result is flagged
rather than silently violated.

### Why not the alternatives

**Market cap: impossible.** Jagex publishes no float. Nobody knows how many
Twisted bows exist. Any "implied market cap" from price × volume or price ×
drop rate is fabricated. Do not ship one.

**Price-weighted: absurd at this dispersion.** Scythe of vitur (1.235B gp)
against Dragon scimitar (59,526 gp) is 20,700:1 — the index would be the
Scythe with decoration. Measured over 361 days: −7.9% at 0.92% daily vol,
versus −8.5% at 0.81% for equal weight. More volatility, no more information.

**Volume-weighted: a tracker in costume.** Self-adapting to liquidity, which
is genuinely appealing, but yields baskets ~50% concentrated in three names.

Equal weight is the only defensible prior when float is unobservable. The cap
exists because the thinnest member otherwise carries a full `1/N` of the
index's manipulation surface.

## 4. Index arithmetic

### 4.1 Three modes, never conflated

| Mode | When | Units | Divisor |
|---|---|---|---|
| `inception` | no prior basket | seeded from target weights | solved so level = `base_level` |
| `revalue` | every scheduled run | **fixed** | **fixed** |
| `review` | scheduled composition review | reset to target weights, preserving basket value | unchanged |

Re-solving the divisor on every run gives `level = value / divisor` with both
recomputed from the same prices: the series pins to `base_level` forever. It
is easy to write, produces entirely plausible-looking output, and is
invisible until you plot it.

### 4.2 Carrying units

The index is carried as **units**, not weights:

```
L_t = ( Σ_i units_i × P_i,t ) / D_t
```

Between rebalances units are constant and weights drift with relative
performance. That is what an investable index does; drifted weights are not a
bug to correct daily.

**Rebalancing preserves basket value, so the divisor does not change.** A
reweighting is not an economic event for the level.

**Membership changes adjust the divisor** to keep the level continuous:

```
D_new = D_old × V_after / V_before
```

Every divisor change is written to `corporate_action` with before/after
values. An index whose divisor moves without an audit trail is a number
someone typed in.

`continuity_check()` fails the job if a level jumps more than 5% across a
rebalance. A silent discontinuity makes every backtest downstream worthless.

## 5. Valuation (NAV)

**Source: `/1h` buckets. Never `/latest`.** See
[api-notes.md](api-notes.md) for why — median staleness 37 minutes, p90 14.8
hours, 15.6% crossed.

Pipeline, in order:

1. **Discard buckets under 5 units.** A bucket built from one trade is one
   player's opinion, and for a thin item that player can be the attacker.
2. **Discard crossed buckets.** A crossed print carries no level
   information, only the fact that the two legs were sampled at different
   moments.
3. **Winsorise at 3 robust sigma — do not trim.** Clamping keeps the sample
   size fixed. Dropping outliers would let an attacker shrink the denominator
   and amplify whatever they left behind.
4. **Volume-weight the survivors.** Time-weighting would treat a dead 3am
   bucket as equal to a busy one.
5. **Below 12 surviving buckets, do not publish as fresh.** Carry the
   previous value and flag `STALE`.

### An unpriced constituent is never valued at zero

Skipping a member whose price is unavailable is arithmetically identical to
marking it at zero, so the basket silently loses that member's weight. Since
~10% of items print a crossed daily bar, this produced a spurious **−100%
drawdown** in historical replay before it was caught.

Missing prices are carried forward from the last usable observation and
flagged stale. A level with any genuinely unpriced member is reported with
quality `MISSING` and is **never published**.

Sigma is estimated from the **median absolute deviation**, never stdev. A
single 100× print inflates stdev enough that a 3-sigma band admits the print
itself — the filter would protect exactly what it exists to reject. When MAD
collapses to zero (more than half the buckets share one price, common for
cheap items) sigma falls back to 0.5% of the median, a floor that an outlier
cannot widen.

This was a real bug caught by `test_mad_resists_a_stdev_inflating_outlier`,
not a hypothetical.

### Quality propagation

Every observation carries `OK` / `DEGRADED` / `STALE` / `MISSING`, and every
published level records `n_stale_members`. An index that hides its own
data-quality record cannot be trusted to settle anything.

Settlement, if a product ever settled on this, uses the **24h VWAP published
at T+24h** — delayed deliberately so anomalies can be contested before money
moves.

## 6. Rebalancing schedule

| Event | Frequency | Notes |
|---|---|---|
| Weight rebalance | Quarterly | Round-trip friction is ~3.5–4% (spread + 2% GE tax) against 16% annualised vol. Monthly would burn ~40%/yr of the risk budget. |
| Composition review | Scheduled, not automatic | Rank-based with hysteresis buffers — see section 2.5. |
| Item removed from game | Immediate | Freeze at last valid 24h VWAP, redistribute pro rata, adjust divisor, log as `remove`. |
| Jagex update repricing a member >25% in 48h | Ad hoc | Human judgement, logged as `jagex_update`. Not automatable. |
| Supply shock (drop-rate nerf, dupe fix) | Freeze 72h | Logged as `supply_shock`. |

The ad-hoc case is genuinely discretionary. It is written into the
methodology rather than hidden because otherwise there is a controversy at
every game update.

## 7. Manipulation resistance

**An index is exactly as manipulable as its cheapest member.** Averaging
attack costs across a basket overstates robustness by orders of magnitude —
the attacker is under no obligation to pick an average name.

Measured cost to move each index by 1%, live data, default parameters:

| Basket | Weakest link | Cost (24h VWAP) | Breakeven exposure |
|---|---|---|---|
| Melee weapons | Abyssal whip | 2,079M gp (~$1,772) | 207.9B gp |
| Body slot | Karil's leathertop | 353M gp (~$301) | 35.3B gp |
| PvM consumables | Ranging potion(4) | 187M gp (~$159) | 18.7B gp |
| Raw materials | Red dragonhide | 149M gp (~$127) | 14.9B gp |

Cost scales sharply with the NAV window. Rune platebody, body-slot basket:

| NAV source | Cost to move index 1% |
|---|---|
| `/latest` | **3,340 gp** (~$0.003) |
| `/5m` | 1.26M gp |
| `/1h` | 15.18M gp |
| `/24h` | 364.32M gp |

Five orders of magnitude between the naive choice and the defensible one.
That is the entire argument for the NAV design in section 4.

USD figures use the bond rate (~1.17M gp/USD), which is the **expensive** end.
Grey-market gold trades well below it, so real attack costs are lower than
quoted — plausibly by 3–5×.

### Model validity

Cost is modelled as linear in the required move. That holds for a 5–10%
push. It does **not** hold at 65%, which is what a liquidity-capped 1.5%
weight implies. Such estimates are flagged `beyond_linear_model`; the true
cost is higher and convex, so the error is conservative for the defender, but
the number is not a measurement. Treat it as "prohibitively expensive,
magnitude unknown".

## 8. Known limitations

- Every figure derives from **single snapshots** (2026-07-27 and 2026-07-28),
  not 30-day averages. Intraday volume plausibly varies 2–3× trough to peak.
- `PREMIUM_REALISATION = 0.5` is a reasoned midpoint, not a backtest.
  Plausible range 0.3–0.7. Conclusions survive the range; point estimates do
  not.
- `/timeseries` caps at 365 points, so no basket can be tested across a full
  OSRS meta cycle (~2–3 years) or a major shock. **16% annualised volatility
  is probably an underestimate.**
- No order book data exists in this API, so liquidation impact is
  extrapolated from volume ratios, never simulated.
- Item identity is resolved by **name**, so a Jagex rename breaks resolution
  loudly. That is the intended failure mode — a wrong id is invisible to a
  reviewer, a wrong name is obvious.
- **Published history before go-live is a backtest.** Reconstructed by
  `replay.py` from stored daily bars, flagged `sim` in the data and rendered
  dotted on the site. It is survivorship-biased by today's candidate pool,
  valued daily rather than hourly, and charged no spread or GE tax. Direction
  and rough magnitude only.
