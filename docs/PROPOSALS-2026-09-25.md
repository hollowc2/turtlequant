# Strategy and model proposals: review items 5, 7, 12, 13

These change what TurtleQuant trades, so none is implemented yet. Each proposal would
ship behind a flag whose default keeps today's behaviour, run in shadow first, and be
judged on the evidence described. Numbers below come from `docs/analysis/`, run read-only
against live Gamma, CLOB and Deribit data on 2026-09-25 (BTC ≈ 84,100, ETH ≈ 2,676).

## Why the order matters: today's edge is mostly a model artifact

Since the discovery fix (#4), the bot sees about 280 priced BTC/ETH markets. In its first
shadow scan it opened seven positions: six touch markets (five "dip to X", one "reach X")
and one other barrier. `model_sensitivity.py` reprices every candidate two ways:

| Model | Mean change in p | Max change | YES candidates (edge ≥ 5%) | NO candidates |
|---|---|---|---|---|
| Current (strike IV, spot + 5% drift) | — | — | 11 | 4 |
| Deribit forward, zero drift | 0.06pp | 0.4pp | 11 | 4 |
| Smile-consistent (skew term) | 2.0pp | 9.4pp | **3** | **17** |

With the skew term, most of today's YES candidates land close to the market mid. For
example, BTC dip to 80k in 3 days: mid 0.065, current model 0.127, skew-adjusted 0.059.
ETH reach 2,900 in 6 days: mid 0.145, model 0.201, skew 0.146. The bot prices a
down-touch with the put wing's IV (the smile's highest vol) and leaves out the
`−vega·∂σ/∂K` term. It therefore overprices exactly the markets it keeps buying, and it
is structurally short delta: 9 of 11 YES candidates are "dip" bets. The barrier column is
a proxy (twice the skew-adjusted terminal probability, zero drift), so treat it as
direction and size, not a precise price.

**Recommendation:** do #7 first; it decides whether any edge is left. #13 caps are cheap
insurance and can go in alongside. #12 is only worth doing on a corrected model. #5
needs settled trades (item 2 is now fixed) to judge. Until #7 lands, consider pausing
barrier types in shadow, or accept that shadow P&L on them measures the artifact.

---

## #7 Pricing: skew, forward, IV age, calibration

**Status: implemented behind flags, all off by default:** `--pricing-model smile`
(items 1–3) and `--max-iv-age-secs` (item 4). Legacy lookups are numerically unchanged,
checked against the previous code on the live surface. On live data a dry-run would have
bought 8 markets with legacy pricing and 2 with smile pricing.

**Change** (`--pricing-model legacy|smile`, default `legacy`):
1. **Digitals:** `P(S_T > K) = N(d2) − vega·∂σ/∂K`, with ∂σ/∂K taken from the
   interpolated smile (a finite difference on the vol surface). `below` types use the
   complement.
2. **Touches:** a smile-consistent price instead of the flat-vol reflection formula.
   Start with `2 × skew-adjusted P(S_T beyond K)` under zero drift (the reflection
   identity), and move to vanna-volga if residuals demand it.
3. **Forward:** use Deribit's `underlying_price` for the bracketing expiries, log-linear
   in T, as F with zero drift, instead of spot and a fixed 5%. Today this is worth at most
   0.4pp (BTC futures +4.99%/yr, ETH +4.57%/yr), but it is free and removes a constant.
4. **IV age** (safety; I'd make this the default): drop Deribit points older than 15
   minutes, and when there are none, block entries (`vol_source=stale`) instead of pricing
   on stale "deribit" points. Store strikes, not moneyness frozen at the fetch-time spot.
   `_interpolate` currently mixes fetch-time moneyness with current-spot wing selection.
   `test_interpolate_uses_otm_wing_and_total_variance` passes only because its points
   default to moneyness 0.
5. **Calibration:** retire the realized-vol Brier gate as the deploy criterion. It tests
   a different model.

**Expected effect:** far fewer YES entries on touch markets (11 → 3 today); the NO side
becomes where the model disagrees with the market; Brier/edge statistics start to
describe the model that trades.

**Validation:**
- *Forward-looking, cheapest and cleanest:* log `p_legacy` and `p_smile` on every
  `signal_evaluation` (diagnostics only, before any flag flip). Join with resolutions
  (now recorded). Compare Brier score, reliability by bucket, and, the actual hypothesis,
  the realised win rate of "model − ask ≥ 5%" trades against the price paid. About 2–4
  weeks of daily/weekly series gives hundreds of resolved markets.
- *Historical:* resolved Gamma crypto-price markets + CLOB `prices-history` (hourly,
  verified available for resolved tokens) + Deribit DVOL history (verified available) as
  a flat-vol proxy. This can test the legacy model's calibration against the market's.
  It cannot test the smile model, because historical option smiles are not public.

## #12 NO-side trading

**Change** (`--sides yes|yes,no`, default `yes`): `Position.outcome` ("YES"/"NO") with
the side's token id, `p_no = 1 − p_yes`, the NO book from `no_token_id` (already
parsed), and settlement from the NO index of `outcomePrices`. Exits, fees and the intent
journal work per token. A state migration defaults existing positions to YES. The
exporter and performance page gain a side column rendered server-side; `_PAGE_SCRIPT`
stays untouched, so the CSP hash does not change.

**Expected effect:** today 4 NO candidates under the current model, 17 under the smile
model. It also balances the book's delta, because the NO side of a dip market is long.

**Validation:** shadow only, same resolution-joined evaluation as #7; check NO-book
depth and spreads (the CLOB mirrors YES/NO, but executable depth differs).

## #13 Portfolio risk

**Change:**
1. `--max-asset-exposure-pct` (gross USD per asset, e.g. 25% of NAV) and
   `--max-asset-delta-pct` (net dollar delta per asset, from each position's
   `∂p/∂S·S·shares`, e.g. ±20% of NAV). Default off, or on at loose values; your call.
2. `--kelly-shrink w`: size on `w·model + (1−w)·mid` instead of the raw model (default
   `w = 1` = today), so model error (RMSE ≈ 5pp) shrinks size rather than the entry gate.
3. An exporter gauge for net delta per asset.

**Expected effect:** today the first scan put five short-delta dip positions on across
BTC and ETH, all the same factor bet. Caps would have stopped after two or three.

**Validation:** replay shadow `signal_evaluation` history through the cap logic and
count blocked entries. No backtest needed; this is risk limiting, not alpha.

## #5 Exits

**Correction to the review:** `edge_at_entry = model − fill` is measured against the
ask, and `current_edge = model − bid` against the bid. Since bid < ask, the current edge
starts about one spread *above* the entry edge, not below. The asymmetry delays
`edge_decayed`. The main point stands: `edge_decayed` and `time_cleanup` sell while
`model − bid > 0`, below the model's own value, and pay the taker fee to do it.

**Change** (`--exit-rule legacy|ev`, default `legacy`): under `ev`, exit only when
selling is worth more than holding after fees, i.e. `bid − fee(bid) ≥ p_model + margin`
(margin ≈ 1pp, to cover model error), or when inputs degrade (stale IV or a fallback vol
source). Otherwise hold to resolution, which now settles correctly. If recycling capital
matters, make that an explicit opportunity-cost rule (swap into a better candidate), not
edge decay.

**Expected effect:** fewer round trips and fees, more trades held to resolution (higher
per-trade variance), capital tied up longer under the 40% cap.

**Validation:** a counterfactual over shadow history. For every `edge_decayed` /
`time_cleanup` close, compare realised P&L with hold-to-resolution P&L from the market's
Gamma `outcomePrices`. That needs the VPS history (not available here); I can write the
script to run there.
