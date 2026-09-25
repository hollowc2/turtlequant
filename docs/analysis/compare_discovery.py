"""Before/after discovery funnel against live Gamma (read-only).

Usage: git show <old-rev>:src/turtlequant/market_scanner.py > /tmp/old_scanner.py
       uv run python docs/analysis/compare_discovery.py /tmp/old_scanner.py
"""
import importlib.util
import sys
from collections import Counter, defaultdict
from datetime import UTC, datetime
from turtlequant.market_scanner import MarketScanner as NewScanner
from turtlequant.market_parser import parse_market, set_corpus_file, strike_is_plausible
from turtlequant.data.binance import fetch_latest_closes
from turtlequant.vol_surface import VolSurface
from turtlequant.probability_engine import compute_probability

set_corpus_file(None)
spec = importlib.util.spec_from_file_location("old_scanner", sys.argv[1])
old = importlib.util.module_from_spec(spec)
sys.modules["old_scanner"] = old  # dataclasses resolve their module here
spec.loader.exec_module(old)
ASSETS = ["btc", "eth"]
spots_raw = fetch_latest_closes(["BTCUSDT", "ETHUSDT"])
spots = {"btc": spots_raw.get("BTCUSDT"), "eth": spots_raw.get("ETHUSDT")}
vols = {a: VolSurface(asset=a) for a in ASSETS}
now = datetime.now(UTC)
BUCKETS = ["<1d", "1-7d", "7-30d", "30-90d", ">90d"]

def bucket(m):
    d = (m.resolution_time - now).total_seconds() / 86400
    return "<1d" if d < 1 else "1-7d" if d < 7 else "7-30d" if d < 30 else "30-90d" if d < 90 else ">90d"

def funnel(scanner, label):
    markets = scanner.get_active_markets()
    stages = defaultdict(Counter)
    for m in markets:
        b = bucket(m)
        stages["passed_filters"][b] += 1
        p = parse_market(m.question, m.resolution_time)
        if p is None or p.asset not in ASSETS:
            continue
        stages["parsed"][b] += 1
        if not strike_is_plausible(p.strike, spots[p.asset]):
            continue
        stages["plausible"][b] += 1
        model = compute_probability(p, spots[p.asset], vols[p.asset].get_iv(spots[p.asset], p.strike, p.expiry))
        if model - m.yes_price >= 0.05 and 0.02 < m.yes_price < 0.98:
            stages["mid_edge>=5%"][b] += 1
    fetched = getattr(scanner, "last_scan_counts", {}).get("fetched")
    print(f"\n== {label}: fetched={fetched if fetched is not None else '(n/a)'} funnel={getattr(scanner, 'last_scan_counts', {})}")
    print(f"{'stage':16s}" + "".join(f"{b:>8s}" for b in BUCKETS) + f"{'total':>8s}")
    for stage in ("passed_filters", "parsed", "plausible", "mid_edge>=5%"):
        c = stages[stage]
        print(f"{stage:16s}" + "".join(f"{c[b]:8d}" for b in BUCKETS) + f"{sum(c.values()):8d}")
    return stages

old_scanner = old.MarketScanner(min_liquidity=5000, max_spread_pct=0.03, assets=ASSETS)
raw_old = old_scanner._fetch_all_pages()
print("BEFORE raw fetched:", len(raw_old), "| sample:", [r.get("question", "")[:40] for r in raw_old[:3]])
old_scanner._fetch_all_pages = lambda: raw_old
funnel(old_scanner, "BEFORE (markets?tag_slug=crypto top 500, relative spread <= 3%)")
funnel(NewScanner(min_liquidity=5000, max_spread=0.03, assets=ASSETS), "AFTER (events tag 1312 minus up/down, absolute spread <= 3c)")
funnel(NewScanner(min_liquidity=0, max_spread=0.03, assets=ASSETS), "AFTER, min_liquidity=0 (for reference)")
print("\nspot:", spots)
