"""Strategy explainer for billybitcoin.cloud/turtlequant/strategy/.

Built alongside the performance page by scripts/generate_performance_page.py.
The rules come from the live bot's startup banner and the lab's markets from
the latest market_marks snapshot, so the page describes the bot as it is
running. All behaviour lives in strategy.js, an external file the site's CSP
allows as 'self'; the page carries its data in a non-executable JSON block, so
regenerating it never needs a CSP hash update.
"""

# Generated HTML/CSS/JS strings in this module intentionally exceed 100 columns.
# ruff: noqa: E501

from __future__ import annotations

import datetime as dt
import html
import json
import re
from collections.abc import Iterable
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

from .performance_page import (
    _BASE_CSS,
    GITHUB_URL,
    ClosedTrade,
    _json_data_block,
    _opt_float,
    _usd,
    parse_ts,
)
from .position_manager import (
    DEFAULT_MAX_PER_EXPIRY_PCT,
    DEFAULT_MAX_PER_MARKET_PCT,
    DEFAULT_MAX_TOTAL_EXPOSURE_PCT,
)
from .probability_engine import RISK_FREE_RATE
from .risk_controls import DEFAULT_MAX_DAILY_LOSS_PCT, MAX_DRAWDOWN_PCT

LOG_FILE = "turtlequant-bot.log"
MARKS_FILE = "turtlequant-marks.jsonl"
BANNER = "=== TurtleQuant Bot ==="
# Option types the bot enters; put/range brackets are scored only (README §2).
LAB_TYPES = ("european", "barrier", "barrier_down")
LAB_MARKET_LIMIT = 60


@dataclass(frozen=True)
class BotRules:
    entry_threshold: float = 0.05
    kelly_fraction: float = 0.25
    sides: str = "YES,NO"
    min_entry_hours: float = 168.0
    max_entry_hours: float = 2160.0
    exit_rule: str = "ev"
    pricing: str = "smile (entries need legacy+smile consensus)"
    starting_nav: float = 100.0
    max_order_usd: float | None = None
    max_per_market_pct: float = DEFAULT_MAX_PER_MARKET_PCT
    max_per_expiry_pct: float = DEFAULT_MAX_PER_EXPIRY_PCT
    max_total_exposure_pct: float = DEFAULT_MAX_TOTAL_EXPOSURE_PCT
    max_daily_loss_pct: float = DEFAULT_MAX_DAILY_LOSS_PCT
    max_drawdown_pct: float = MAX_DRAWDOWN_PCT
    from_banner: bool = False


# "Entry thresh: 0.050 (5.0%)" -> key "Entry thresh", value "0.050 (5.0%)"
_BANNER_LINE = re.compile(r"turtlequant_bot: (?P<key>[A-Za-z /]+?)\s*: (?P<value>.+)$")
_NUMBER = re.compile(r"-?\d+(?:\.\d+)?")


def _first_number(text: str) -> float | None:
    match = _NUMBER.search(text.replace(",", ""))
    return float(match.group()) if match else None


def parse_banner(lines: Iterable[str]) -> BotRules | None:
    """Rules from the last startup banner in a bot log, or None if it has none."""
    fields: dict[str, str] | None = None
    current: dict[str, str] | None = None
    for line in lines:
        if BANNER in line:
            current = {}
            fields = current
            continue
        if current is None:
            continue
        match = _BANNER_LINE.search(line.rstrip("\n"))
        if match:
            current[match.group("key").strip()] = match.group("value").strip()
        elif "turtlequant_bot:" in line:
            current = None  # the blank line that closes the banner
    if not fields:
        return None

    def number(key: str, default: float | None) -> float | None:
        value = fields.get(key)
        parsed = _first_number(value) if value else None
        return default if parsed is None else parsed

    defaults = BotRules()
    return BotRules(
        entry_threshold=number("Entry thresh", defaults.entry_threshold) or defaults.entry_threshold,
        kelly_fraction=number("Kelly frac", defaults.kelly_fraction) or defaults.kelly_fraction,
        sides=fields.get("Sides", defaults.sides),
        min_entry_hours=number("Min entry", defaults.min_entry_hours) or 0.0,
        max_entry_hours=number("Max entry", defaults.max_entry_hours) or 0.0,
        exit_rule=fields.get("Exit rule", defaults.exit_rule),
        pricing=fields.get("Pricing", defaults.pricing),
        starting_nav=number("Starting NAV", defaults.starting_nav) or defaults.starting_nav,
        max_order_usd=number("Max order", None),
        from_banner=True,
    )


def load_bot_rules(state_dir: Path) -> BotRules:
    """The newest startup banner across the bot log and its rotations."""
    for path in [state_dir / LOG_FILE, *sorted(state_dir.glob(f"{LOG_FILE}.[0-9]*"), key=lambda p: int(p.suffix[1:]))]:
        try:
            with path.open(encoding="utf-8", errors="replace") as handle:
                rules = parse_banner(handle)
        except OSError:
            continue
        if rules is not None:
            return rules
    return BotRules()


def latest_marks(state_dir: Path, *, tail_bytes: int = 4 * 1024 * 1024) -> dict[str, Any] | None:
    """The last market_marks record, read from the end of a large append-only file."""
    path = state_dir / MARKS_FILE
    try:
        with path.open("rb") as handle:
            handle.seek(0, 2)
            size = handle.tell()
            handle.seek(max(0, size - tail_bytes))
            chunk = handle.read()
    except OSError:
        return None
    for raw in reversed(chunk.splitlines()):
        try:
            record = json.loads(raw)
        except (json.JSONDecodeError, UnicodeDecodeError):
            continue  # first line of the chunk is usually cut in half
        if isinstance(record, dict) and record.get("event") == "market_marks":
            return record
    return None


def lab_markets(marks: dict[str, Any] | None, rules: BotRules) -> tuple[list[dict[str, Any]], int]:
    """Markets the bot could enter from the last scan, and which one to open on.

    Returns the rows ordered by asset, expiry and strike, plus the index of the
    mid-priced market closest to a trade. The bot enters only when both models
    clear the threshold on the same side, so "closest" is the better side's
    smaller (legacy, smile) edge.
    """
    if not marks:
        return [], 0
    as_of = parse_ts(marks.get("ts"))
    if as_of is None:
        return [], 0
    rows: list[dict[str, Any]] = []
    for raw in marks.get("rows") or []:
        if not isinstance(raw, dict) or raw.get("t") not in LAB_TYPES:
            continue
        expiry = parse_ts(raw.get("exp"))
        spot = _opt_float(raw.get("spot"))
        strike = _opt_float(raw.get("k"))
        sigma = _opt_float(raw.get("sigma_legacy")) or _opt_float(raw.get("v"))
        bid = _opt_float(raw.get("bid"))
        ask = _opt_float(raw.get("ask"))
        p_smile = _opt_float(raw.get("p_smile_full"))
        p_legacy = _opt_float(raw.get("p_legacy_full"))
        if None in (expiry, spot, strike, sigma, bid, ask) or not spot or not strike or not sigma:
            continue
        hours = (expiry - as_of).total_seconds() / 3600.0
        if hours < rules.min_entry_hours or (rules.max_entry_hours and hours > rules.max_entry_hours):
            continue
        question = str((raw.get("contract") or {}).get("question") or "")
        if not question:
            continue
        edge = None
        if p_smile is not None and p_legacy is not None:
            edge = max(min(p_legacy, p_smile) - ask, bid - max(p_legacy, p_smile))
        rows.append({
            "q": question[:120],
            "a": str(raw.get("a") or "").upper(),
            "t": raw["t"],
            "s": round(spot, 2),
            "k": strike,
            "d": round(hours / 24.0, 3),
            "v": round(sigma, 4),
            "bid": bid,
            "ask": ask,
            "pl": p_legacy,
            "ps": p_smile,
            "_edge": edge if edge is not None else -1.0,
        })
    # Keep the markets nearest a trade, then list them the way a reader scans a board.
    rows = sorted(rows, key=lambda r: -r["_edge"])[:LAB_MARKET_LIMIT]
    rows.sort(key=lambda r: (r["a"], r["d"], r["t"], r["k"]))
    mid_priced = [i for i, r in enumerate(rows) if 0.10 <= (r["bid"] + r["ask"]) / 2 <= 0.90]
    default = max(mid_priced or range(len(rows)), key=lambda i: rows[i]["_edge"], default=0)
    for row in rows:
        del row["_edge"]
    return rows, default


def _pct0(value: float) -> str:
    return f"{value * 100:.0f}%"


def _best_trade_card(trades: list[ClosedTrade]) -> str:
    winners = [t for t in trades if t.pnl > 0]
    if not winners:
        return ""
    best = max(winners, key=lambda t: t.pnl)
    side = "NO" if best.outcome.upper() == "NO" else "YES"
    entry = f"{best.entry_price * 100:.0f}¢" if best.entry_price is not None else "—"
    exit_ = f"{best.exit_price * 100:.0f}¢" if best.exit_price is not None else "—"
    model = f"{best.model_prob_at_entry * 100:.0f}%" if best.model_prob_at_entry is not None else None
    ret = best.return_pct
    model_line = f" The model put it at <b>{model}</b>." if model else ""
    return f"""
  <section class="highlight" aria-labelledby="h-best">
    <div class="eyebrow" id="h-best">Best shadow trade so far</div>
    <div class="highlight-body">
      <div>
        <div class="highlight-q">{html.escape(best.question)}</div>
        <p>Bought <b>{side}</b> at <b>{entry}</b> on {best.opened_at.strftime('%b %-d, %Y') if best.opened_at else '—'}.{model_line}
        Sold at <b>{exit_}</b> on {best.closed_at.strftime('%b %-d')} when the bid paid more than the model's value.</p>
      </div>
      <div class="highlight-figs">
        <div><div class="label">Return</div><div class="value pos">{f"{ret:+.0f}%" if ret is not None else "—"}</div></div>
        <div><div class="label">P&amp;L</div><div class="value pos">{_usd(best.pnl, signed=True)}</div></div>
      </div>
    </div>
  </section>"""


def render_strategy_page(
    *,
    rules: BotRules,
    marks: dict[str, Any] | None,
    shadow_trades: list[ClosedTrade],
    live_nav: float | None,
    generated_at: dt.datetime,
) -> str:
    markets, default_market = lab_markets(marks, rules)
    as_of = parse_ts(marks.get("ts")) if marks else None
    spots = (marks or {}).get("spots") or {}
    nav = live_nav if live_nav and live_nav > 0 else rules.starting_nav
    payload = {
        "rules": {**asdict(rules), "riskFreeRate": RISK_FREE_RATE, "nav": nav},
        "markets": markets,
        "defaultMarket": default_market,
        "asOf": as_of.strftime("%Y-%m-%d %H:%M UTC") if as_of else None,
    }
    days_lo = rules.min_entry_hours / 24
    days_hi = rules.max_entry_hours / 24
    window = f"{days_lo:.0f}–{days_hi:.0f} days" if rules.max_entry_hours else f"{days_lo:.0f}+ days"
    edge_cents = f"{rules.entry_threshold * 100:.0f}¢"
    max_order = f"${rules.max_order_usd:,.0f}" if rules.max_order_usd else "uncapped"
    spot_note = " · ".join(
        f"{asset.upper()} ${float(price):,.0f}" for asset, price in sorted(spots.items()) if _opt_float(price)
    )
    scan_note = (
        f"{len(markets)} markets in the entry window at the last scan ({payload['asOf']}{' · ' + spot_note if spot_note else ''})"
        if markets else "No scan snapshot was available when this page was built; pick inputs by hand."
    )
    stamp = generated_at.astimezone(dt.UTC).strftime("%Y-%m-%d %H:%M UTC")
    rules_source = "the live bot's startup log" if rules.from_banner else "the bot's defaults"

    return f"""<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>TurtleQuant — Strategy Explained</title>
  <meta name="description" content="How TurtleQuant prices Polymarket crypto markets as options, decides which side is mispriced, sizes the bet and exits. Includes a pricing lab on real markets.">
  <link rel="preload" as="font" type="font/woff2" crossorigin href="/assets/fonts/inter-var-latin.woff2">
  <link rel="stylesheet" href="/assets/fonts.css">
  <style>{_BASE_CSS}{_STRATEGY_CSS}</style>
</head>
<body>
<main class="strategy">
  <nav class="top-links">
    <a class="site-link" href="/"><span aria-hidden="true">←</span>billybitcoin.cloud</a>
    <a class="site-link" href="../"><span aria-hidden="true">←</span>portfolio</a>
    <a class="site-link" href="{GITHUB_URL}" target="_blank" rel="noopener noreferrer">source on GitHub<span aria-hidden="true">↗</span></a>
  </nav>

  <header class="hero">
    <div class="kicker">Strategy explained · Polymarket crypto</div>
    <h1 class="display">Price the bet.<br>Buy the gap.</h1>
    <p class="about">
      Polymarket asks questions like <i>"Will Bitcoin reach $100k by September?"</i> and lets people trade
      YES and NO at prices between 0¢ and $1. To an options trader each one is a <b>digital option</b>,
      and options markets already price that probability. TurtleQuant reads Bitcoin and Ether implied volatility from Deribit,
      turns it into a probability for every market, and buys whichever side the crowd has priced at least
      <b>{edge_cents}</b> too cheap.
    </p>
    <div class="stats keyfigs">
      <div class="stat"><div class="label">Minimum edge</div><div class="value">{edge_cents}</div><div class="sub">model − price, either side</div></div>
      <div class="stat"><div class="label">Sizing</div><div class="value">{rules.kelly_fraction:g}× Kelly</div><div class="sub">capped at {_pct0(rules.max_per_market_pct)} of NAV per market</div></div>
      <div class="stat"><div class="label">Holding window</div><div class="value">{window}</div><div class="sub">to resolution at entry</div></div>
      <div class="stat"><div class="label">Live order cap</div><div class="value">{max_order}</div><div class="sub">per order, real money</div></div>
    </div>
  </header>

  <section class="lab" aria-labelledby="h-lab">
    <div class="eyebrow">Try it</div>
    <h2 id="h-lab" class="section-title">Price a market</h2>
    <p class="section-lede">
      Pick a market the bot priced in its last scan, or set your own inputs. The lab runs the bot's
      Black-Scholes formulas, compares the result with the order book and decides what the bot would do.
    </p>
    <div class="lab-grid">
      <form class="panel lab-inputs" id="labForm" autocomplete="off">
        <label class="field field-wide">
          <span class="label">Market</span>
          <select id="labMarket"><option value="">Custom inputs</option></select>
          <span class="hint">{html.escape(scan_note)}</span>
        </label>
        <label class="field field-wide">
          <span class="label">Question type</span>
          <select id="labType">
            <option value="european">Above the strike at expiry</option>
            <option value="barrier">Reaches the strike before expiry</option>
            <option value="barrier_down">Dips to the strike before expiry</option>
          </select>
        </label>
        <label class="field"><span class="label">Spot ($)</span><input id="labSpot" type="number" step="any" min="0" value="65000"></label>
        <label class="field"><span class="label">Strike ($)</span><input id="labStrike" type="number" step="any" min="0" value="80000"></label>
        <label class="field"><span class="label">Days to expiry</span><input id="labDays" type="number" step="any" min="0" value="45"></label>
        <label class="field"><span class="label">Implied vol (%)</span><input id="labVol" type="number" step="any" min="0" value="55"></label>
        <label class="field"><span class="label">YES bid (¢)</span><input id="labBid" type="number" step="0.1" min="0" max="100" value="12"></label>
        <label class="field"><span class="label">YES ask (¢)</span><input id="labAsk" type="number" step="0.1" min="0" max="100" value="13"></label>
      </form>

      <div class="panel lab-out" aria-live="polite">
        <div class="lab-figs">
          <div><div class="label">Model P(YES)</div><div class="value big" id="outProb">—</div><div class="sub" id="outSmile"></div></div>
          <div><div class="label">YES edge</div><div class="value" id="outYesEdge">—</div><div class="sub">model − YES ask</div></div>
          <div><div class="label">NO edge</div><div class="value" id="outNoEdge">—</div><div class="sub">(1 − model) − NO ask</div></div>
        </div>
        <div class="ladder" aria-hidden="true">
          <div class="ladder-track">
            <div class="ladder-spread" id="ladSpread"></div>
            <div class="ladder-model" id="ladModel"><span>model</span></div>
          </div>
          <div class="ladder-scale"><span>0¢</span><span>25¢</span><span>50¢</span><span>75¢</span><span>$1</span></div>
        </div>
        <div class="verdict" id="outVerdict">
          <div class="verdict-title" id="outVerdictTitle">—</div>
          <div class="verdict-body" id="outVerdictBody"></div>
        </div>
      </div>
    </div>
    <p class="fine">
      The lab uses the bot's flat-vol ("legacy") model with a {RISK_FREE_RATE:.0%} rate. The live bot trades on a
      smile model that adds a skew correction, and it only enters when both models clear the threshold
      on the same side. For a scanned market the lab also applies the smile price from that scan; once you
      edit an input it falls back to flat-vol alone. Sizing assumes an empty book of
      {_usd(nav)} (the live NAV), so caps on total and per-expiry exposure don't bind here.
    </p>
  </section>

  <section aria-labelledby="h-steps">
    <div class="eyebrow">Every 60 seconds</div>
    <h2 id="h-steps" class="section-title">How a scan runs</h2>
    <ol class="steps">
      <li>
        <div class="step-n">01 · find</div>
        <h3>Read the board</h3>
        <p>Pull Polymarket's crypto price events, about 1,400 markets in two requests. Keep those with over $5k of liquidity
        and both sides quoted within 3¢. One-sided markets are dropped, not given a made-up spread.</p>
      </li>
      <li>
        <div class="step-n">02 · parse</div>
        <h3>Turn words into a contract</h3>
        <p>"Will Ethereum dip to $2,000 by December 31?" becomes <i>ETH, $2,000, Dec 31, touch from above</i>.
        Three shapes trade: above at expiry, reach before expiry, dip before expiry.</p>
      </li>
      <li>
        <div class="step-n">03 · price</div>
        <h3>Ask the options market</h3>
        <p>Read Deribit's implied volatility for that strike and date. Price "above" as N(d₂), and
        "reach / dip" with the reflection principle. The smile model adds a skew term, and the flat model has to agree.</p>
      </li>
      <li>
        <div class="step-n">04 · buy</div>
        <h3>Only when it's cheap</h3>
        <p>Buy {html.escape(rules.sides.replace(",", " or "))} when the model beats the executable ask by {edge_cents} or more,
        and the market resolves {window} out. Size at {rules.kelly_fraction:g}× Kelly, inside the exposure caps.</p>
      </li>
      <li>
        <div class="step-n">05 · exit</div>
        <h3>Hold, unless the bid overpays</h3>
        <p>Default is hold to resolution. Sell early only when the bid, after Polymarket's fee, is worth more than the model's
        probability plus a 1¢ margin. Those show as <i>ev exit</i> in the trade log.</p>
      </li>
    </ol>
  </section>

  <section aria-labelledby="h-risk">
    <div class="eyebrow">Guard rails</div>
    <h2 id="h-risk" class="section-title">What stops it</h2>
    <div class="stats limits">
      <div class="stat"><div class="label">Per market</div><div class="value">{_pct0(rules.max_per_market_pct)}</div><div class="sub">of NAV</div></div>
      <div class="stat"><div class="label">Per expiry date</div><div class="value">{_pct0(rules.max_per_expiry_pct)}</div><div class="sub">of NAV</div></div>
      <div class="stat"><div class="label">Total exposure</div><div class="value">{_pct0(rules.max_total_exposure_pct)}</div><div class="sub">of NAV</div></div>
      <div class="stat"><div class="label">Daily loss halt</div><div class="value">{_pct0(rules.max_daily_loss_pct)}</div><div class="sub">of NAV, realized</div></div>
      <div class="stat"><div class="label">Drawdown halt</div><div class="value">{_pct0(rules.max_drawdown_pct)}</div><div class="sub">from peak NAV</div></div>
      <div class="stat"><div class="label">Stale data</div><div class="value">No entry</div><div class="sub">on old books or vol</div></div>
    </div>
  </section>
{_best_trade_card(shadow_trades)}
  <p class="disclaimer">
    Rules on this page are read from {rules_source} each time it is built ({html.escape(stamp)}). The live account is
    small and every order is capped; the longer record is the shadow run, which simulates fills against real order books.
    This describes a trading experiment and is not investment advice.
  </p>
</main>
<script type="application/json" id="strategy-data">
{_json_data_block(payload)}
</script>
<script src="strategy.js"></script>
</body>
</html>
"""


# Written as strategy/strategy.js. It is external, so the site's 'self' script-src
# covers it and edits need no CSP hash.
STRATEGY_JS = r"""'use strict';
(() => {
  const data = JSON.parse(document.getElementById('strategy-data').textContent);
  const rules = data.rules;
  const R = rules.riskFreeRate;
  const $ = (id) => document.getElementById(id);

  // Abramowitz-Stegun 7.1.26 erf; plenty for a display (|error| < 1.5e-7).
  function ncdf(x) {
    const z = Math.abs(x) / Math.SQRT2;
    const t = 1 / (1 + 0.3275911 * z);
    const y = 1 - (((((1.061405429 * t - 1.453152027) * t) + 1.421413741) * t - 0.284496736) * t + 0.254829592) * t * Math.exp(-z * z);
    return 0.5 * (1 + (x >= 0 ? y : -y));
  }
  const clamp = (p) => Math.max(1e-6, Math.min(1 - 1e-6, p));

  // Mirrors probability_engine.digital_probability / barrier_probability / barrier_down_probability.
  function probability(type, S, K, T, sigma) {
    if (!(T > 0 && sigma > 0 && S > 0 && K > 0)) {
      if (type === 'barrier_down') return S <= K ? 1 : 0;
      return S >= K ? 1 : 0;
    }
    const mu = R - 0.5 * sigma * sigma;
    const sT = sigma * Math.sqrt(T);
    if (type === 'barrier') {
      if (S >= K) return 1;
      const l = Math.log(S / K);
      return clamp(ncdf((l + mu * T) / sT) + Math.exp(2 * mu * -l / (sigma * sigma)) * ncdf((l - mu * T) / sT));
    }
    if (type === 'barrier_down') {
      if (S <= K) return 1;
      const l = Math.log(K / S);
      return clamp(ncdf((l - mu * T) / sT) + Math.exp(-2 * mu * -l / (sigma * sigma)) * ncdf((l + mu * T) / sT));
    }
    return clamp(ncdf((Math.log(S / K) + mu * T) / sT));
  }

  // position_manager.kelly_size on an empty book: fraction x full Kelly x NAV, per-market and order caps.
  function kellySize(p, price) {
    if (!(price > 0 && price < 1) || p <= price) return 0;
    const b = (1 - price) / price;
    const full = (b * p - (1 - p)) / b;
    let size = Math.max(0, full) * rules.kelly_fraction * rules.nav;
    size = Math.min(size, rules.max_per_market_pct * rules.nav, rules.max_total_exposure_pct * rules.nav);
    if (rules.max_order_usd) size = Math.min(size, rules.max_order_usd);
    return size;
  }

  const cents = (p) => `${(p * 100).toFixed(1)}¢`;
  const pp = (x) => `${x >= 0 ? '+' : '−'}${Math.abs(x * 100).toFixed(1)}¢`;
  const money = (v) => `$${v.toFixed(2)}`;
  const num = (id) => parseFloat($(id).value);
  const payoff = {
    european: ['the price is above the strike at expiry', 'the price is not above the strike at expiry'],
    barrier: ['the price reaches the strike before expiry', 'the price never reaches the strike'],
    barrier_down: ['the price dips to the strike before expiry', 'the price never dips to the strike'],
  };

  const select = $('labMarket');
  const groups = {};
  data.markets.forEach((m, i) => {
    if (!groups[m.a]) {
      groups[m.a] = document.createElement('optgroup');
      groups[m.a].label = m.a;
      select.appendChild(groups[m.a]);
    }
    const opt = document.createElement('option');
    opt.value = String(i);
    opt.textContent = `${m.q} · ${Math.round(m.d)}d · ${(m.ask * 100).toFixed(1)}¢`;
    groups[m.a].appendChild(opt);
  });

  function load(m) {
    $('labType').value = m.t;
    $('labSpot').value = m.s;
    $('labStrike').value = m.k;
    $('labDays').value = m.d.toFixed(1);
    $('labVol').value = (m.v * 100).toFixed(1);
    $('labBid').value = (m.bid * 100).toFixed(1);
    $('labAsk').value = (m.ask * 100).toFixed(1);
  }

  function setTone(el, x) {
    el.classList.toggle('pos', x >= rules.entry_threshold);
    el.classList.toggle('neg', x < 0);
  }

  function render() {
    const type = $('labType').value;
    const S = num('labSpot');
    const K = num('labStrike');
    const days = num('labDays');
    const sigma = num('labVol') / 100;
    const bid = num('labBid') / 100;
    const ask = num('labAsk') / 100;
    const verdict = $('outVerdict');
    const title = $('outVerdictTitle');
    const body = $('outVerdictBody');
    if (![S, K, days, sigma, bid, ask].every(Number.isFinite) || bid > ask || ask > 1 || bid < 0) {
      $('outProb').textContent = '—';
      $('outYesEdge').textContent = '—';
      $('outNoEdge').textContent = '—';
      verdict.className = 'verdict';
      title.textContent = 'Check the inputs';
      body.textContent = 'Every field needs a number, and the bid can\'t be above the ask.';
      return;
    }
    const p = probability(type, S, K, days / 365, sigma);
    const m = select.value === '' ? null : data.markets[Number(select.value)];
    const yesEdge = p - ask;          // buy YES at the ask
    const noEdge = (1 - p) - (1 - bid); // buy NO at 1 - YES bid
    $('outProb').textContent = `${(p * 100).toFixed(1)}%`;
    $('outYesEdge').textContent = pp(yesEdge);
    $('outNoEdge').textContent = pp(noEdge);
    setTone($('outYesEdge'), yesEdge);
    setTone($('outNoEdge'), noEdge);

    $('outSmile').textContent = m && m.ps != null ? `bot's smile model at scan: ${(m.ps * 100).toFixed(1)}%` : '';

    $('ladSpread').style.left = `${bid * 100}%`;
    $('ladSpread').style.width = `${Math.max(0.6, (ask - bid) * 100)}%`;
    $('ladModel').style.left = `${p * 100}%`;

    const hours = days * 24;
    const sides = rules.sides.toUpperCase();
    const thr = rules.entry_threshold;
    // A scanned market with untouched inputs also has the smile model's price,
    // and the bot needs both models past the threshold on the same side.
    const ps = m && m.ps != null ? m.ps : null;
    const smileYes = ps == null ? Infinity : ps - ask;
    const smileNo = ps == null ? Infinity : bid - ps;
    const yesOk = sides.includes('YES') && yesEdge >= thr && smileYes >= thr;
    const noOk = sides.includes('NO') && noEdge >= thr && smileNo >= thr;
    verdict.className = 'verdict';
    if (hours < rules.min_entry_hours || (rules.max_entry_hours && hours > rules.max_entry_hours)) {
      title.textContent = 'Pass: outside the holding window';
      body.textContent = `It resolves in ${days.toFixed(1)} days. The bot only enters ${(rules.min_entry_hours / 24).toFixed(0)}–${(rules.max_entry_hours / 24).toFixed(0)} days out: shorter barrier markets are where the flat model overprices, and longer ones tie up capital for a thin return.`;
      return;
    }
    const flatWants = yesEdge >= thr ? 'YES' : noEdge >= thr ? 'NO' : null;
    const smileWants = smileYes >= thr && ps != null ? 'YES' : smileNo >= thr && ps != null ? 'NO' : null;
    if (!yesOk && !noOk && ps != null && (flatWants || smileWants)) {
      title.textContent = 'Pass: the models disagree';
      body.textContent = `The flat-vol model ${flatWants ? `wants ${flatWants} (${pp(flatWants === 'YES' ? yesEdge : noEdge)})` : 'sees no edge'}, `
        + `but the smile model ${smileWants ? `wants ${smileWants} (${pp(smileWants === 'YES' ? smileYes : smileNo)})` : 'sees no edge'}. `
        + `The bot only buys when both clear ${(thr * 100).toFixed(0)}¢ on the same side.`;
      return;
    }
    if (!yesOk && !noOk) {
      const best = Math.max(yesEdge, noEdge);
      title.textContent = 'Pass: priced about right';
      body.textContent = `The best side is only ${pp(best)} cheap. The bot wants ${(rules.entry_threshold * 100).toFixed(0)}¢ of edge before paying the spread and fees.`;
      return;
    }
    const buyYes = yesOk && (!noOk || yesEdge >= noEdge);
    const price = buyYes ? ask : 1 - bid;
    const prob = buyYes ? p : 1 - p;
    if ((buyYes ? (bid + ask) / 2 : 1 - (bid + ask) / 2) >= 0.98) {
      title.textContent = 'Pass: nothing left to win';
      body.textContent = 'That side already trades at 98¢ or more, so the upside cannot pay for the risk.';
      return;
    }
    const size = kellySize(prob, price);
    verdict.className = 'verdict buy';
    title.textContent = `Buy ${buyYes ? 'YES' : 'NO'} at ${cents(price)}`;
    body.textContent = `${ps != null ? 'Both models agree. ' : ''}The flat-vol model says ${buyYes ? 'YES' : 'NO'} is worth ${(prob * 100).toFixed(1)}¢, so it is ${pp(prob - price)} cheap. `
      + `${rules.kelly_fraction}× Kelly on the ${money(rules.nav)} live book: about ${money(size)}`
      + `${rules.max_order_usd && size >= rules.max_order_usd - 1e-9 ? ' (order cap)' : ''}. `
      + `It pays $1 a share if ${payoff[type][buyYes ? 0 : 1]}.`;
  }

  select.addEventListener('change', () => {
    if (select.value !== '') load(data.markets[Number(select.value)]);
    render();
  });
  $('labForm').addEventListener('input', (event) => {
    if (event.target !== select) select.value = '';
    render();
  });
  if (data.markets.length) {
    const start = data.markets[data.defaultMarket] ? data.defaultMarket : 0;
    select.value = String(start);
    load(data.markets[start]);
  }
  render();
})();
"""


_STRATEGY_CSS = """
main.strategy { max-width: 1100px; }
h1.display { font-size: clamp(34px, 6vw, 56px); line-height: 1.05; letter-spacing: -0.02em; margin: 6px 0 18px; }
.keyfigs { margin-top: 22px; }
.eyebrow { color: var(--accent); font-family: "IBM Plex Mono", ui-monospace, monospace; font-size: 12px; letter-spacing: 0.06em; text-transform: uppercase; margin: 48px 0 6px; }
h2.section-title { color: var(--text); font-size: 22px; text-transform: none; letter-spacing: -0.01em; margin: 0 0 8px; font-weight: 600; }
.section-lede { max-width: 72ch; margin: 0 0 16px; color: var(--muted); font-size: 14.5px; line-height: 1.55; }
.lab-grid { display: grid; grid-template-columns: minmax(0, 5fr) minmax(0, 6fr); gap: 12px; }
.lab-inputs { display: grid; grid-template-columns: 1fr 1fr; gap: 12px; align-content: start; }
.field { display: flex; flex-direction: column; gap: 6px; min-width: 0; }
.field-wide { grid-column: 1 / -1; }
.field .hint { color: var(--muted); font-size: 12px; line-height: 1.45; }
.field input, .field select {
  background: #0f0f0f; color: var(--text); border: 1px solid var(--border); border-radius: 8px;
  padding: 9px 10px; font: 500 14px "IBM Plex Mono", ui-monospace, monospace; min-width: 0; width: 100%; box-sizing: border-box;
}
.field select { font-family: Inter, ui-sans-serif, system-ui, sans-serif; font-size: 13.5px; }
.field input:focus, .field select:focus { border-color: rgba(200, 146, 42, 0.6); outline: none; }
.lab-out { display: flex; flex-direction: column; gap: 18px; }
.lab-figs { display: grid; grid-template-columns: repeat(3, minmax(0, 1fr)); gap: 12px; }
.value.big { font-size: 30px; }
.ladder-track { position: relative; height: 34px; border-radius: 8px; background: linear-gradient(90deg, rgba(232,226,214,0.04), rgba(232,226,214,0.08)); border: 1px solid var(--border); }
.ladder-spread { position: absolute; top: 6px; bottom: 6px; background: rgba(200, 146, 42, 0.35); border: 1px solid var(--accent); border-radius: 4px; transition: left 160ms ease, width 160ms ease; }
.ladder-model { position: absolute; top: -4px; bottom: -4px; width: 2px; margin-left: -1px; background: var(--text); transition: left 160ms ease; }
.ladder-model span { position: absolute; top: -18px; left: 50%; transform: translateX(-50%); font: 11px "IBM Plex Mono", ui-monospace, monospace; color: var(--muted); }
.ladder-scale { display: flex; justify-content: space-between; color: var(--muted); font: 11px "IBM Plex Mono", ui-monospace, monospace; margin-top: 6px; }
.ladder { padding-top: 14px; }
.verdict { border: 1px solid var(--border); border-radius: 10px; padding: 14px 16px; background: #0f0f0f; }
.verdict.buy { border-color: rgba(106, 170, 120, 0.55); background: rgba(106, 170, 120, 0.08); }
.verdict-title { font-weight: 600; font-size: 16px; }
.verdict.buy .verdict-title { color: var(--up); }
.verdict-body { color: var(--muted); font-size: 13.5px; line-height: 1.55; margin-top: 6px; }
.fine { color: var(--muted); font-size: 12.5px; line-height: 1.55; max-width: 90ch; margin: 12px 2px 0; }
.steps { list-style: none; padding: 0; margin: 0; display: grid; grid-template-columns: repeat(auto-fit, minmax(190px, 1fr)); gap: 10px; }
.steps li { background: var(--panel); border: 1px solid var(--border); border-radius: 10px; padding: 16px; }
.step-n { color: var(--accent); font: 12px "IBM Plex Mono", ui-monospace, monospace; letter-spacing: 0.04em; }
.steps h3 { font-size: 15.5px; margin: 8px 0 6px; }
.steps p { color: var(--muted); font-size: 13.5px; line-height: 1.55; margin: 0; }
.limits .stat { flex-basis: 150px; }
.highlight { margin-top: 48px; background: var(--panel); border: 1px solid rgba(200, 146, 42, 0.35); border-radius: 12px; padding: 18px 20px; }
.highlight .eyebrow { margin-top: 0; }
.highlight-body { display: flex; flex-wrap: wrap; gap: 24px; justify-content: space-between; align-items: center; }
.highlight-body > div:first-child { flex: 1 1 420px; }
.highlight-q { font-size: 17px; font-weight: 600; margin: 4px 0 6px; }
.highlight p { color: var(--muted); font-size: 14px; line-height: 1.55; margin: 0; }
.highlight p b { color: var(--text); font-weight: 500; }
.highlight-figs { display: flex; gap: 28px; }
.disclaimer { color: var(--muted); font-size: 12.5px; line-height: 1.55; max-width: 90ch; margin-top: 40px; border-top: 1px solid var(--border); padding-top: 16px; }
@media (max-width: 860px) {
  .lab-grid { grid-template-columns: 1fr; }
}
@media (max-width: 520px) {
  .lab-figs { grid-template-columns: 1fr 1fr; }
  .lab-figs > div:first-child { grid-column: 1 / -1; }
}
"""
