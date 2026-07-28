# osrs-assets

**Index construction over the Old School RuneScape Grand Exchange — and an
honest feasibility study of why the fund on top of it should not be built.**

This repo does two things:

1. **Ships working code** that collects OSRS market data, screens a universe,
   and computes published, auditable, equal-weighted indices over baskets of
   items (melee weapons, body armour, PvM consumables, raw materials).
2. **Publishes the analysis** that says: the index is a good idea, and every
   product that would custody player gold on top of it is not.

It holds no gold, custodies nothing, and accepts no deposits. That boundary
is a conclusion, not a limitation — see
[docs/feasibility.md](docs/feasibility.md).

```
$ python3 -m osrs_index publish

  OSRS-BODY        804.34  revalue   ok        9 members
  OSRS-MELEE     1,141.87  revalue   ok        8 members
  OSRS-PVM       1,005.96  revalue   ok       12 members
  OSRS-RAW         752.70  revalue   ok        9 members
wrote 4 index payloads to site/data
```

---

## Why this exists

"An ETF for OSRS items" is an idea that surfaces on every trading-adjacent
corner of the community. It sounds obviously good: players hold directional
views on gear prices and currently have no way to express them without
picking a single item and eating its idiosyncratic volatility.

The idea is half right. This repo separates the half that works from the half
that gets people banned, and puts numbers on both.

**The measured case for an index.** The melee basket over 361 days:

```
total return  -8.5%      constituent range: -35.4% (Abyssal whip)
daily vol      0.81%                        +44.1% (Inquisitor's mace)
annualised    16%
max drawdown -24.2%
```

Roughly half the volatility of its average member. Real diversification, and
a 79-point spread between best and worst inside a single thematic basket —
exactly the condition under which an index is useful. The drift is negative,
so the honest framing is a trading instrument, not a savings vehicle.

**The measured case against the fund.** Three findings, each in the code:

- **A 24/7 custody bot is a rule violation, not a risk to manage.** Jagex's
  macroing rule is *"using software or hardware that can help you play the
  game with the software or hardware doing things for you."* An account that
  automatically receives and sends gold is a bot by definition.
- **Creation is rate-limited; redemption is not.** Buy limits cap basket
  assembly at ~101–252M gp per account per day. There is no sell-side limit.
  Slow capped entry plus instant unlimited exit is a run-prone design by
  construction.
- **The index is cheap to move.** Even with every defence applied, the PvM
  and raw-materials baskets can be pushed 1% for **$127–$159**. Tighten the
  screen until attacks cost ~$1,300 and the universe collapses to 108
  mutually-correlated endgame items.

The paradox, stated plainly: **the interesting baskets are manipulable, and
the robust baskets are redundant.**

---

## Quick start

No dependencies. The collector uses `urllib` and `sqlite3` from the standard
library, because an index nobody can reproduce is not an index.

```bash
git clone https://github.com/lheintzmann1/osrs-assets
cd osrs-assets

# The wiki API asks every client to identify itself with a contact route.
export OSRS_INDEX_USER_AGENT="osrs-assets/0.1 - @yourhandle on Discord"

python3 -m osrs_index restore     # rebuild the cache from committed data/
python3 -m osrs_index collect     # ~4,600 items, all four aggregate windows
python3 -m osrs_index publish     # build indices + write site/data/
python3 -m osrs_index status      # collection health
```

Then open the site:

```bash
python3 -m http.server -d site 8000
```

Other commands: `backfill` (365d of daily history), `screen` (eligibility
report with rejection reasons), `build`, `replay` (reconstruct history —
a backtest, see below), `attack` (manipulation cost by NAV window).

Tests (`pip install pytest`):

```bash
python3 -m pytest        # 105 tests, no network access
```

---

## What the code actually gets right

Five design decisions carry most of the value. Each was forced by measured
data, and every one of the last three was a bug in this repo before it was a
feature. They have tests because they were caught, not because they were
foreseen.

### 1. `/latest` is never used for valuation

The API does not publish a price. It publishes the last instant-buy and the
last instant-sell as **two independent event streams with independent
timestamps**. Measured:

| | |
|---|---|
| Median staleness of the older leg | **37 min** |
| p90 / p99 | 14.8 h / 38.2 h |
| Items where `high < low` | **15.6%** |
| Items with both sides fresh in the last 5m | 718 of 4,591 |

What that costs if you ignore it — cost to move the body-slot index by 1%
through Rune platebody:

| NAV source | Cost |
|---|---|
| `/latest` | **3,340 gp** (~$0.003) |
| `/5m` | 1.26M gp |
| `/1h` | 15.18M gp |
| `/24h` | 364.32M gp |

Five orders of magnitude. `nav.py` reads hourly aggregates; the tick table
exists only for anomaly detection.

### 2. Outliers are winsorised with MAD, never stdev, never trimmed

Trimming lets an attacker shrink the sample and amplify what remains. And a
single 100× print inflates *stdev* enough that a 3-sigma band admits the
print itself — the filter would protect exactly what it exists to reject.

That was a real bug in this repo, caught by
`test_mad_resists_a_stdev_inflating_outlier`, not a hypothetical.

### 3. Membership is rank-based with buffers, and the naive version is broken

Baskets are not fixed lists. Each review ranks the candidate pool by
liquidity and applies entry/exit buffers: join at rank ≤ 8, leave only past
rank 12, target size 10. An item drifting inside the dead band is left alone,
which matters because every avoided round trip is ~3.5–4% of friction not
paid.

The obvious implementation is wrong, and this repo shipped it wrong first. If
entry is a hard gate, then once ranks 1–8 are all members, a vacancy at rank
9 can never be filled — no eligible non-member ranks 8 or better. The basket
shrinks monotonically and `target_size` silently becomes a ceiling it only
ever drifts below. The fix is a third tier: the buffer zone is a priority
queue favouring incumbents, not a wall. Pinned by
`test_vacancies_are_filled_from_the_buffer_zone`.

### 4. Liquidity screening and physical replicability are different questions

`limit × price` per 4h window measures whether a basket can be **assembled**,
not whether it can be **measured**. Applying it globally rejects every
constituent of the PvM consumables basket — the most liquid of the four —
because consumables are cheap items with generous buy limits. Blood rune
turns over 21.8B gp/day and caps at 8.5M gp per account per window: a fine
index member, a terrible fund holding.

Also a real bug, also fixed, also tested. It is off by default and reported
either way.

### 5. An index must actually move

`build()` re-solving the divisor on every run gives `level = value / divisor`
with both recomputed from the same prices — the series pins to `base_level`
forever and looks entirely plausible in a table. So valuation is split into
three modes: `inception` seeds the divisor, `revalue` holds units and divisor
fixed (what the twice-daily job runs), and `review` resets weights while
preserving basket value so the level stays continuous.

Related, and worse: skipping an unpriced constituent is arithmetically
marking it at zero. Since ~10% of items print a crossed daily bar, a replay
dropped that weight outright and produced a −100% drawdown. Missing prices
are now carried forward and flagged stale, and a level with any unpriced
member is never published.

---

## Repository layout

```
src/osrs_index/
  client.py         API client; UA policy, retries, v1/v2 pinning
  nav.py            valuation: VWAP, winsorisation, quality flags
  universe.py       eligibility screening
  membership.py     rank-based entry/exit with hysteresis buffers
  index.py          weights, units, divisor, continuity
  manipulation.py   attack cost model  <- the interesting one
  replay.py         historical reconstruction (a backtest, labelled as one)
  storage.py        SQLite schema and persistence
  collect.py        idempotent collection jobs
  export.py         plain-text artefacts + site payload
  pipeline.py       spec -> screen -> membership -> weights -> level
  cli.py            command line interface

indices/            index specs (JSON, name-based, human-reviewable)
site/               static frontend: 3 files + vendored Lightweight Charts
data/               committed store of record (NDJSON + JSON)
docs/
  api-notes.md      live API probe results, incl. undocumented behaviour
  methodology.md    the index contract
  data-format.md    on-disk formats and why they are text
  feasibility.md    the go/no-go analysis
  roadmap.md        phases, stack, failure modes
tests/              105 tests, no network
```

---

## Hosting: free, static, twice a day

The whole thing runs on GitHub's free tier for public repositories, with no
server and no account beyond GitHub:

- **GitHub Actions** runs `restore → collect → publish` on a cron at 06:17
  and 18:17 UTC. Public repos get unlimited Actions minutes; a run takes
  about a minute.
- **The refreshed `data/` is committed back** to the repo. It is the store of
  record and it is plain text, so every index level is reproducible and every
  change is reviewable in a diff. It carries the live basket — members, units
  and divisor — because the runner keeps no state between runs, and an index
  that cannot restore its units re-seeds its divisor and quietly resets to
  1000.00 on every run.
- **GitHub Pages** serves `site/` from the run's artifact. Three files plus a
  vendored copy of [Lightweight Charts](https://github.com/tradingview/lightweight-charts)
  (182 KB, Apache 2.0). No framework, no build step, no CDN, no external
  requests at all.

Twice a day is not a compromise here — the indices value off 24h VWAPs, so a
more frequent job would republish the same number with more commits.

Cloudflare Pages and Vercel both work equally well if you prefer them; they
just add an account and a deploy token for no gain, since Actions is already
doing the data work.

**Two gotchas worth knowing before you rely on the schedule:**

1. GitHub **disables scheduled workflows after 60 days without repository
   activity**. Commits pushed by `GITHUB_TOKEN` do not trigger workflows, and
   whether they reset the inactivity clock is not clearly documented — do not
   assume they do. If the schedule goes quiet, trigger the workflow manually
   once from the Actions tab.
2. Actions cron is **best-effort**. Runs are routinely delayed 5–30 minutes at
   peak and are occasionally dropped. Fine at this cadence; not fine for
   anything settling money, which is one more reason this project settles
   nothing.

### Why plain text rather than a committed SQLite file

Measured over fourteen daily commits of the real database, after `git gc`:

| | packed |
|---|---|
| SQLite blob | 3.10 MiB |
| Equivalent NDJSON | 1.00 MiB |

Only ~3×, so the size argument is weaker than it first looks — git delta-
compresses SQLite pages better than expected, and my initial guess of "1.4 GB
a year" was simply wrong. The decisive reasons are the others: a text diff is
reviewable, a text conflict is resolvable where a corrupt database is not,
the browser can `fetch()` JSON directly instead of loading ~1 MB of sql.js
WASM, and `grep` works. Full detail in [docs/data-format.md](docs/data-format.md).

---

## The manipulation model

`manipulation.py` is the module most index projects do not ship. Every
provider claims robustness; almost none publish the price of breaking their
own product.

In an equal-weighted index of `N` members, moving one constituent by `X%`
moves the index by `X/N %`. **An index is exactly as manipulable as its
cheapest member** — averaging attack costs across a basket overstates
robustness by orders of magnitude, because the attacker picks the weakest
name, not an average one.

Live results, cost to move each index 1%:

| Basket | Weakest link | Cost | Breakeven exposure |
|---|---|---|---|
| Melee weapons | Abyssal whip | 2,079M gp (~$1,772) | 207.9B gp |
| Body slot | Karil's leathertop | 353M gp (~$301) | 35.3B gp |
| PvM consumables | Ranging potion(4) | 187M gp (~$159) | 18.7B gp |
| Raw materials | Red dragonhide | 149M gp (~$127) | 14.9B gp |

USD uses the bond rate (~1.17M gp/$), which is the **expensive** end —
grey-market gold is cheaper, so real costs are plausibly 3–5× lower.

The model is linear in the required move, which holds for a 5–10% push and
not for the 65% implied by a capped 1.5% weight. Those estimates are flagged
`beyond_linear_model` rather than quoted as measurements. The error is
conservative for the defender, but it is not a number.

---

## Game rules

Read [docs/feasibility.md §5](docs/feasibility.md) before building anything
that touches gold. The summary:

| Component | Status |
|---|---|
| Read-only published index, no deposits | ✅ Clean |
| Virtual portfolio / leaderboard | ✅ Clean |
| Gold-only positions, manual settlement | ⚠️ Grey — not explicitly prohibited ≠ permitted |
| Automated 24/7 custody account | ❌ Direct macroing violation |
| Custody account shared between operators | ❌ Direct account-sharing violation |
| Real money in or out, incl. tokens | ❌ RWT — **bans users, not just operators** |

One correction to a claim that circulates widely: **staking was not removed
in 2016.** March 2013 added the rule banning unofficial player-hosted
gambling; 2021 capped Duel Arena stakes at 10M gp; **6 July 2022** removed the
Duel Arena entirely.

This repo is not affiliated with or endorsed by Jagex.

---

## Status and honesty notes

**Phase 0.** Read-only indices, working end to end against live data,
published twice a day by GitHub Actions to GitHub Pages. No deposits, no
custody, no real money — and per the analysis, no plans for any.

**The charted history before go-live is a backtest.** It is reconstructed
from stored daily bars, rendered dotted rather than solid, and flagged `sim`
in the data. It is survivorship-biased by today's candidate pool, valued
daily rather than hourly, and charged no spread or GE tax. Direction and
rough magnitude only — it is not a track record.

Things this repo does **not** claim:

- Figures come from **single snapshots** (2026-07-27 and 2026-07-28), not
  30-day averages. Intraday volume plausibly varies 2–3× trough to peak.
- The attack cost model is **reasoned, not backtested**. Its central
  assumption (`PREMIUM_REALISATION = 0.5`) is plausible anywhere in 0.3–0.7.
- `/timeseries` caps at 365 points, so nothing can be tested across a full
  OSRS meta cycle. **16% annualised volatility is probably an
  underestimate.**
- Liquidation impact is extrapolated from volume ratios, never simulated —
  the API exposes no order book.
- Nobody has checked whether a comparable index already exists, or whether
  similar "gold bank" projects have been publicly banned. That base rate
  would materially change the risk ranking.

Full list in [docs/feasibility.md §9](docs/feasibility.md).

---

## Contributing

Useful contributions, roughly in order:

- **Rolling 30-day screening data** to replace the single-snapshot
  thresholds. This is the biggest known weakness.
- **A better attack cost model** — anything empirical beats the current
  reasoned estimate.
- **Additional basket specs** in `indices/`. Specs are name-based JSON so a
  reviewer can sanity-check them without looking up item ids.
- **Evidence about enforcement**: documented cases of OSRS financial services
  being banned, or not.

Please do not open PRs that add custody, deposits, or real-money flows. The
analysis explaining why is in the repo; if you disagree with it, argue with
the numbers in an issue first.

## Data source

[OSRS Wiki real-time prices API](https://oldschool.runescape.wiki/w/RuneScape:Real-time_Prices),
run by wiki volunteers. Set a descriptive User-Agent with a contact route,
keep request rates sane, and do not make them regret publishing it.

## License

MIT — see [LICENSE](LICENSE).
