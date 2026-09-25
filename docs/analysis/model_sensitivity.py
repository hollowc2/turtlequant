"""How much would the #7 pricing fixes move today's candidates? (read-only)

For every BTC/ETH market that passes the scanner, parses and has a
plausible strike, compare the bot's current model with:

* fwd  — Deribit's futures price for the expiry as the forward, zero drift
         (instead of spot plus a fixed 5% rate);
* skew — the smile-consistent digital, P(S_T > K) = N(d2) - vega * dsigma/dK
         (instead of N(d2) at the strike's IV). Barriers are approximated as
         2x the skew-adjusted terminal probability (reflection, zero drift).

Also counts YES-side vs NO-side edges (#12) and the delta direction of the
YES candidates (#13).

Usage: uv run python docs/analysis/model_sensitivity.py
"""

from __future__ import annotations

import math
from collections import Counter
from datetime import UTC, datetime
from statistics import NormalDist, mean

import requests

from turtlequant.data.binance import fetch_latest_closes
from turtlequant.market_parser import OptionType, parse_market, set_corpus_file, strike_is_plausible
from turtlequant.market_scanner import MarketScanner
from turtlequant.probability_engine import compute_probability
from turtlequant.vol_surface import VolSurface, _parse_deribit_instrument

N = NormalDist()
THRESHOLD = 0.05
YEAR = 365 * 86400


def forwards(ccy: str) -> list[tuple[float, float]]:
    """(T in years, futures price) per Deribit option expiry."""
    rows = requests.get(
        "https://www.deribit.com/api/v2/public/get_book_summary_by_currency",
        params={"currency": ccy, "kind": "option"},
        timeout=20,
    ).json()["result"]
    now = datetime.now(UTC).timestamp()
    by_expiry: dict[float, float] = {}
    for row in rows:
        parsed = _parse_deribit_instrument(row["instrument_name"])
        if parsed and row.get("underlying_price"):
            by_expiry[(parsed[1].timestamp() - now) / YEAR] = float(row["underlying_price"])
    return sorted(by_expiry.items())


def forward_at(curve: list[tuple[float, float]], spot: float, t: float) -> float:
    """Log-linear in T between spot (T=0) and the Deribit futures curve."""
    points = [(0.0, spot), *curve]
    for (t0, f0), (t1, f1) in zip(points, points[1:]):
        if t0 <= t <= t1:
            w = (t - t0) / (t1 - t0) if t1 > t0 else 0.0
            return math.exp((1 - w) * math.log(f0) + w * math.log(f1))
    t_last, f_last = points[-1]
    return spot * (f_last / spot) ** (t / t_last) if t_last > 0 else spot


def terminal_prob(fwd: float, k: float, t: float, sigma: float, dsigma_dk: float, above: bool) -> tuple[float, float]:
    """(plain N(d2), skew-adjusted) probability of finishing above (or below) K, zero drift."""
    s = sigma * math.sqrt(t)
    d1 = (math.log(fwd / k) + 0.5 * s * s) / s
    d2 = d1 - s
    vega = fwd * N.pdf(d1) * math.sqrt(t)  # undiscounted dC/dsigma
    p_above = N.cdf(d2)
    p_above_skew = min(1.0, max(0.0, p_above - vega * dsigma_dk))
    return (p_above, p_above_skew) if above else (1 - p_above, 1 - p_above_skew)


def main() -> None:
    set_corpus_file(None)
    assets = ("btc", "eth")
    closes = fetch_latest_closes(["BTCUSDT", "ETHUSDT"])
    spots = {"btc": closes["BTCUSDT"], "eth": closes["ETHUSDT"]}
    curves = {"btc": forwards("BTC"), "eth": forwards("ETH")}
    surfaces = {a: VolSurface(asset=a) for a in assets}
    now = datetime.now(UTC)

    rows = []
    for market in MarketScanner(assets=list(assets)).get_active_markets():
        params = parse_market(market.question, market.resolution_time)
        if params is None or params.asset not in assets:
            continue
        spot = spots[params.asset]
        if not strike_is_plausible(params.strike, spot):
            continue
        vs = surfaces[params.asset]
        k, t = params.strike, max((params.expiry - now).total_seconds() / YEAR, 1e-6)
        sigma = vs.get_iv(spot, k, params.expiry)
        if vs.last_source != "deribit":
            continue
        h = 0.01 * k
        dsdk = (vs.get_iv(spot, k + h, params.expiry) - vs.get_iv(spot, k - h, params.expiry)) / (2 * h)
        fwd = forward_at(curves[params.asset], spot, t)
        p_now = compute_probability(params, spot, sigma)
        kind = params.option_type
        above = kind in (OptionType.EUROPEAN, OptionType.BARRIER)
        p_fwd_terminal, p_skew_terminal = terminal_prob(fwd, k, t, sigma, dsdk, above)
        if kind in (OptionType.EUROPEAN, OptionType.EUROPEAN_PUT):
            p_fwd, p_skew = p_fwd_terminal, p_skew_terminal
        else:
            # Touch probability: current model with the forward's drift, and
            # 2x the skew-adjusted terminal probability as a smile-aware proxy.
            drift = math.log(fwd / spot) / t
            from turtlequant.probability_engine import barrier_down_probability, barrier_probability

            touch = barrier_probability if kind == OptionType.BARRIER else barrier_down_probability
            p_fwd = touch(spot, k, t, sigma, drift)
            p_skew = min(1.0, 2 * p_skew_terminal)
        rows.append((market, params, t, spot, fwd, sigma, dsdk, p_now, p_fwd, p_skew))

    print(f"spot BTC {spots['btc']:,.0f} ETH {spots['eth']:,.2f}; {len(rows)} priced markets with Deribit IV")
    for asset in assets:
        curve = curves[asset]
        far = curve[-1]
        print(
            f"{asset.upper()} forward premium, longest expiry ({far[0]:.2f}y): "
            f"{math.log(far[1] / spots[asset]) / far[0]:+.2%}/yr (bot assumes +5.00%/yr)"
        )

    def edge_count(i: int, side: str = "yes") -> int:
        count = 0
        for market, *_rest in rows:
            p = _rest[i - 1]
            mid = market.yes_price
            if not 0.02 < mid < 0.98:
                continue
            count += (p - mid >= THRESHOLD) if side == "yes" else (mid - p >= THRESHOLD)
        return count

    for label, index in (("now", 7), ("fwd", 8), ("skew", 9)):
        diffs = [abs(r[index] - r[7]) for r in rows]
        print(
            f"model={label:4s} mean |p - p_now| {mean(diffs):.4f}  max {max(diffs):.4f}  "
            f"YES edge>=5%: {edge_count(index)}  NO edge>=5%: {edge_count(index, 'no')}"
        )

    print("\nYES candidates today (model_now - mid >= 5%) and how the corrections move them:")
    print(f"{'type':13s}{'asset':>6s}{'K':>9s}{'days':>6s}{'mid':>7s}{'now':>7s}{'fwd':>7s}{'skew':>7s}  delta")
    deltas = Counter()
    for market, params, t, spot, fwd, sigma, dsdk, p_now, p_fwd, p_skew in rows:
        mid = market.yes_price
        if p_now - mid < THRESHOLD or not 0.02 < mid < 0.98:
            continue
        sign = "short" if params.option_type in (OptionType.BARRIER_DOWN, OptionType.EUROPEAN_PUT) else "long"
        deltas[(params.asset, sign)] += 1
        print(
            f"{params.option_type.value:13s}{params.asset:>6s}{params.strike:>9,.0f}{t * 365:>6.0f}"
            f"{mid:>7.3f}{p_now:>7.3f}{p_fwd:>7.3f}{p_skew:>7.3f}  {sign}"
        )
    print("YES candidate delta direction:", dict(deltas))


if __name__ == "__main__":
    main()
