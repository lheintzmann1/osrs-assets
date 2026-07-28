# Feasibility: can you build an "OSRS ETF"?

**Short answer: you can build the index. You cannot build the fund.**

This document is the go/no-go analysis behind the code. Every number is
either measured from the live API or derived from a stated assumption. Where
a figure is soft, it says so.

---

## 1. What the data supports

See [api-notes.md](api-notes.md) for the full probe results. Three facts
constrain everything downstream:

1. **There is no price.** The API publishes the last instant-buy and the last
   instant-sell as independent event streams. Median staleness of the older
   leg is 37 minutes; 15.6% of items show `high < low`.
2. **The market is ~100 items deep and a very long unusable tail.** Total
   turnover ~7.16T gp/24h, of which the top 20 items are 47.6%. Only 392
   items clear 1B gp/day; only 108 clear 10B.
3. **Spread is a deterministic function of liquidity.** Median 1.6% above
   10B gp/day, 23.7% below 1M.

## 2. Does the index itself have value?

Yes, and this is the one genuinely positive finding.

Melee weapons basket, equal-weighted, 361 days of daily history:

```
total return  -8.5%
daily vol      0.81%   ->  16% annualised
max drawdown -24.2%
```

against constituent daily vols of 0.38%–2.60%. Diversification works: the
basket has roughly half the volatility of its average member.

Constituent dispersion over the same period:

| Item | Return | Daily vol |
|---|---|---|
| Inquisitor's mace | **+44.1%** | 2.60% |
| Soulreaper axe | **+40.2%** | 2.20% |
| Dragon scimitar | −0.5% | 0.38% |
| Elder maul | −11.4% | 1.81% |
| Dragon warhammer | −20.8% | 2.14% |
| Dragon claws | −22.2% | 1.57% |
| Scythe of vitur | −23.0% | 1.00% |
| Osmumten's fang | −25.6% | 2.01% |
| Ghrazi rapier | −30.8% | 2.28% |
| Abyssal whip | **−35.4%** | 1.38% |

A 79-point spread between best and worst inside one thematic basket is
exactly the condition under which an index is useful: "I think melee gear
goes up" is a view many players hold and currently cannot express without
picking the right item and eating 2.3%/day of idiosyncratic vol.

**But the drift is negative.** −8.5% in nominal gp over a year, because gold
inflation is more than offset by gear deflation as drops accumulate. The
honest pitch is a **directional trading instrument, not a savings vehicle**.

## 3. Product structure

### Physical replication is capped by buy limits

To create `X` gp of an `N`-name equal-weighted basket you need `X/N` of each
member, and each member is capped at `limit × price × 6` per account per day.
The binding constraint is the **cheapest leg**, not the average:

```
X ≤ N × min_i(daily_capacity_i)
```

| Basket | Binding member | Capacity per account per day |
|---|---|---|
| Melee weapons | Dragon scimitar, 4.2M/4h | **252M gp** |
| Body slot | Rune platebody, 2.7M/4h | **178M gp** |
| PvM consumables | Ranging potion, 4.2M/4h | **227M gp** |
| Raw materials | Yew logs, 1.4M/4h | **101M gp** |

A 10B gp fund — roughly $8,500, i.e. *tiny* — needs ~40 account-days of
buying. Every additional account is a fresh ToS surface (section 5).

**The structural danger: creation is rate-limited, redemption is not.** There
is no sell-side buy limit. Entering is slow and capped; exiting is instant
and unlimited. That is the worst possible combination for an open-ended
vehicle — it is a run-prone design by construction.

### Round-trip friction

Spread (1.0–1.9% median by basket) plus **2% GE sell tax** (raised from 1% on
2025-05-29, capped at 5M gp/item) ≈ **3.5–4% per round trip**, against 16%
annualised vol. Quarterly rebalancing burns ~14%/yr of the risk budget;
monthly would burn ~40%.

### Synthetic is better, but relocates the problem

Zero tracking error by construction, marginal capital requirement. But:
**you cannot short in OSRS.** There is no borrow, no futures, no way to
hedge. A market maker taking the other side of buy flow can only hedge by
buying the basket physically — inheriting every constraint above, with a
lag.

In practice a synthetic counterparty is **structurally short and unhedged**,
carrying gap risk from Jagex updates (a raid nerf can move gear 25% in 48h).
That is not an asset manager. It is a betting book.

### "What if everyone exits at once?"

The product breaks or defaults. There is no third option.

- **Physical:** liquidating 10B gp of the melee basket is ~1.4× a full day's
  volume in the thin names. Estimated impact 10–30%, and that impact feeds
  back into the NAV — exits push NAV down, which triggers exits.
- **Synthetic:** worse, because it is a binary counterparty default. Margin
  covers ~25% of movement; beyond that, holders are unpaid.

There is no lender of last resort and no credible suspension mechanism —
reputation is the only asset, and suspending redemptions destroys it.

Working guardrails: redemption gates (7-day notice), per-user exposure caps,
and a total AUM cap below the manipulation breakeven. All three make the
product safer *and* uninteresting. That trade-off has no elegant solution.

## 4. Manipulation

**Measured, live, with default parameters** — cost to move each index 1%:

| Basket | Weakest link | Cost | Breakeven exposure |
|---|---|---|---|
| Melee weapons | Abyssal whip | 2,079M gp (~$1,772) | 207.9B gp |
| Body slot | Karil's leathertop | 353M gp (~$301) | 35.3B gp |
| PvM consumables | Ranging potion(4) | 187M gp (~$159) | 18.7B gp |
| Raw materials | Red dragonhide | 149M gp (~$127) | 14.9B gp |

The volume screen plus liquidity cap do materially more work than a
back-of-envelope equal-weight calculation suggests — the screen removes the
two toxic names outright, and the cap pushes the remaining thin member's
weight down (Abyssal whip to 1.53%), so moving the index through it requires
an implausible 65% move on the item.

**Caveat, stated plainly:** at a 65% required move the linear cost model is
out of its validity range. Those estimates are flagged `beyond_linear_model`.
The error is conservative — real cost is higher and convex — but the number
is not a measurement.

### The NAV window is the whole defence

Rune platebody, body-slot basket, cost to move the index 1%:

| NAV source | Cost |
|---|---|
| `/latest` | **3,340 gp** (~$0.003) |
| `/5m` | 1.26M gp |
| `/1h` | 15.18M gp |
| `/24h` | 364.32M gp |

**Five orders of magnitude.** An index built naively on `/latest` can be
moved for a third of a US cent. This is why `nav.py` structurally refuses to
read the tick table.

### The uncomfortable conclusion

Even with every defence applied, a PvM or raw-materials index is movable for
**$127–$159**. An attacker holding position `P` gains `1% × P`, so the attack
pays above ~15–19B gp of exposure. For a product to be interesting it needs
AUM well above that.

Tightening the screen trades breadth for cost:

| Volume floor | Eligible items | Cost to move 1% |
|---|---|---|
| none | 3,773 | ~$0.10 (via `/latest`) |
| 100M gp | 934 | ~$8 |
| 1B gp | 392 | ~$130 |
| **10B gp** | **108** | ~$1,300 |

And those 108 items are all endgame PvM gear with correlations near one — a
"diversified basket" built from them diversifies nothing.

**The paradox is frontal: the interesting baskets are manipulable, and the
robust baskets are redundant.**

## 5. Game rules

Verbatim, from the [Rules of Old School
RuneScape](https://legal.jagex.com/docs/rules/rules-of-old-school-runescape):

- **RWT** — *"Real World trading (RWT) means buying or selling in the real
  world, for real money **or in exchange for anything of value**, things that
  relate to Jagex accounts."* The emphasised clause covers crypto, gift
  cards, and services. It is drafted broadly on purpose.
- **Games of chance** — *"You must not advertise, organise, promote, or take
  part in any games of chance."* Four verbs; promoting is enough.
- **Account sharing** — *"Players must not share, transfer or lend their
  account to anyone else."*
- **Macroing** — *"Using software or hardware that can help you play the game
  with the software or hardware doing things for you."*

### Correcting a common claim

Staking was **not** removed in 2016. The actual timeline: **March 2013**, rule
added banning unofficial player-hosted gambling. **2021**, 10M gp stake cap
at the Duel Arena. **6 July 2022**, Duel Arena removed from the game
entirely. Jagex took nine years to go from writing the rule to amputating the
feature — which is the relevant base rate for how enforcement risk
materialises.

### Design components, assessed

| Component | Status |
|---|---|
| Read-only published index, no deposits | ✅ **Clean.** Public API, no gold touched. |
| Virtual portfolio / paper trading / leaderboard | ✅ **Clean.** No gold changes hands. |
| Gold-only positions, manual P2P settlement | ⚠️ **Grey.** Not a game of chance (outcome is market-driven, not random) and not RWT (no real money). But enforcement is discretionary and the gambling rule has historically swept up anything resembling betting gold on a future outcome. Not explicitly prohibited ≠ permitted. |
| **Automated 24/7 custody account** | ❌ **Direct violation** of the macroing rule. A bot that receives and sends gold is a bot. No grey area. |
| Custody account operated by several people | ❌ **Direct violation** of account sharing. |
| Leverage or any random element | ❌ Becomes a game of chance. |
| Buying units with real money | ❌ **RWT.** Bans users, not just the operator. |
| On-chain token backed by gold | ❌ **RWT** ("anything of value"), aggravated by native fiat liquidity. |

**The point that matters most:** in any custody scenario it is not only the
operator's accounts at risk. **Every user who sends gold to a custody service
risks their own account** — often thousands of hours of play. The fact that
this warning is necessary is itself the signal.

Beyond Jagex: a product where users pay real money for exposure to a basket
of prices, settling in fiat, is a **derivative on a virtual asset**. Depending
on jurisdiction that may engage MiFID II or gambling regulation. *Not legal
advice — flagged as something to verify before writing code, not after.*

## 6. Settlement options

The in-game world has **no escrow, no multisig, no timelock, no contracts**. A
trade is atomic between two parties. There is nothing to compose.

| | A. 24/7 bot | A′. Manual custodian | B. Lots + caps | C. On-chain token | D. Read-only |
|---|---|---|---|---|---|
| Trust required | Total | Total | Bounded (~50M gp/user) | Total + oracle + contract | **None** |
| ToS compliant | ❌ macroing | ⚠️ grey | ⚠️ grey | ❌ RWT | ✅ |
| Operator ban risk | **Certain** | High | Medium | Certain | None |
| **User** ban risk | High | High | Medium | **High** | **None** |
| Max loss on failure | 100% AUM | 100% AUM | 1 lot | 100% AUM | 0 |
| Setup cost | $15–30k | $10k | $5k | **$50–150k** | **~$2k** |
| Running cost | $3–5k/yr | ~$30k/yr | ~$15k/yr | $10k/yr | **$600/yr** |
| Latency | seconds | hours | hours–days | min on-chain, days to redeem | N/A |
| Scalability | high → 0 at ban | ~100 tx/day | ~20 tx/day | high on-chain, bridge-capped | **unlimited** |
| Actually trustless | no | no | no | **no** | n/a |

### On option C, without the marketing

**Nothing in a tokenised design is trustless.** The chain adds accounting
transparency and nothing else:

- The underlying asset is **a row in a database Jagex controls**. They can
  modify, freeze, delete, or roll back a server — and do, after every dupe.
  No cryptography constrains that.
- **The oracle problem is unsolvable here.** There is no cryptographic proof
  of an OSRS account's state. A 5-of-9 multi-signer oracle reduces collusion
  risk; it does not remove it, and every signer is themselves a bannable
  account.
- **Redemption needs a human with a game account.** The bridge from chain to
  game is purely social.
- **It makes the legal position worse.** A token swappable for gold →
  crypto → fiat is RWT by construction.

What it genuinely buys: an auditable share register and P2P transfer of
units without touching the custodian. Useful. Not decentralisation, and
presenting it as such would be dishonest to users.

### On option B — approximations that actually help

| Mechanism | Real value |
|---|---|
| Reputable third-party escrow | Moves trust, doesn't reduce it. Cost 1–5%. |
| Over-collateralised market maker | Genuinely effective, but the collateral is gold in an account — **circular**, it needs option A to hold it. |
| Public proof of reserves | Screenshots forge trivially, and it proves no absence of liabilities. Social signal only. |
| **Settlement in small lots** | **Most effective.** Tranches ≤50M gp, parties alternate. Max loss = one tranche. Cost: latency. |
| Per-user exposure caps | Bounds loss, doesn't prevent it. Necessary, not sufficient. |

Combined, these move you from total trust to "max loss ~50M gp per user".
Real progress. Still very far from trustless.

## 7. Top five risks, ranked

1. **Ban of any custody infrastructure.** Probability ≈ 1 within 12 months;
   impact = 100% of AUM. An automated account receiving and sending gold is a
   bot under the literal rule. Not a risk to provision — a design
   incompatibility. *Avoidable only by having no custody.*
2. **Index manipulation, profitable from ~15–19B gp of exposure** ($127–$159
   to move a basket 1%). Hardening the screen until attacks cost ~$1,300
   shrinks the universe to 108 mutually-correlated items, destroying the
   basket's purpose. *Partial mitigation only.*
3. **Counterparty failure / rug pull.** 100% of AUM. No escrow, no multisig,
   no in-game recourse. Proof-of-reserves by screenshot proves nothing.
   *Mitigation: lot-based settlement and exposure caps bound the loss.*
4. **Friction that hollows out the product.** 3.5–4% round trip against 16%
   annualised vol, on an underlying that returned −8.5% over the measured
   year. *Mitigation: synthetic — which relocates the problem onto a
   counterparty who cannot hedge.*
5. **Real-world regulatory exposure**, distinct from ToS. Any real money makes
   this a derivative on a virtual asset or a gambling product. *Flagged, not
   concluded — verify before building, not after.*

## 8. Recommendation

**GO on the index. NO-GO on custody of gold. No exceptions.**

The two halves have incomparable risk profiles, and fusing them destroys the
half that works.

**Build:** the four indices, computed properly, backfilled 365 days,
published with an open methodology, an open API, and charts. ~$2k, 2–3 weeks,
zero ToS risk. The methodology and the accumulated history are the asset.

**Then:** a virtual portfolio with a leaderboard. Still zero gold, still zero
risk. This is the demand test, and it produces the one piece of data nobody
currently has — whether anyone actually wants this.

**Conditions under which Phase 2 (real gold positions) would be
reconsidered.** All four must hold simultaneously:

1. ≥ **500 monthly active users** on the virtual portfolio after 3 months.
   Below that the market does not exist.
2. AUM capped at **2B gp** (~$1,700) — structurally below the manipulation
   breakeven, permanently, not temporarily.
3. **Zero in-game automation.** 100% manual settlement, lots ≤50M gp, accounts
   operated by one identified natural person. If it does not scale, that is
   the signal that it should not.
4. **Gold only.** No real money in, out, via token, or via intermediary.

Be clear-eyed: conditions 2 and 3 guarantee Phase 2 is never a business — at
best a demonstration for twenty people. **If the goal is to make money, the
answer is no-go, full stop.** If the goal is to build something interesting
and useful for the community, Phase 0 is excellent and sufficient on its own.

## 9. Uncertainties, unsmoothed

- **Single snapshots**, 2026-07-27 and 2026-07-28. Nothing is averaged over 30
  days. Intraday volume plausibly varies 2–3× trough to peak. Every screen
  threshold should be recomputed on rolling data before driving a decision.
- **The attack cost model is reasoned, not backtested.** `PREMIUM_REALISATION
  = 0.5` is a midpoint; plausible range 0.3–0.7. The qualitative conclusion
  ("cheap") survives easily. The precise breakeven figures do not.
- **All USD conversions use the bond rate** (~1.17M gp/$, from $9.99/bond
  after Jagex's March 2026 price rise). That is the *expensive* end.
  Grey-market gold is cheaper — I have seen $0.15–0.25/M cited but have no
  source I trust. **Real attack costs are plausibly 3–5× lower than quoted.**
- **Rule enforcement.** The quoted text is official. How Jagex actually
  enforces against gold-only financial services is unobserved; "grey area,
  discretionary" is inference from their gambling history, not established
  fact.
- **Not checked:** whether an OSRS index already exists, and whether
  comparable "gold bank" projects have been publicly banned. That should be
  the first research task before any code — the historical base rate would
  materially change risk 3.
- **361 days of history maximum** (`/timeseries` caps at 365 points). No
  basket can be tested across a full meta cycle (~2–3 years) or a major
  shock. **16% annualised vol is probably an underestimate.**
- **Acknowledged blind spot:** liquidation impact ("10–30%") is extrapolated
  from volume ratios, not simulated. The API exposes no order book, so it
  cannot be simulated with this data source at all.
