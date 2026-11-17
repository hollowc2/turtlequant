import datetime as dt
import json
import re

from turtlequant.strategy_page import (
    STRATEGY_JS,
    BotRules,
    lab_markets,
    latest_marks,
    load_bot_rules,
    parse_banner,
    render_strategy_page,
)

NOW = dt.datetime(2026, 11, 16, 23, 0, tzinfo=dt.UTC)

BANNER = """\
03:34:12 [INFO] turtlequant_bot: === TurtleQuant Bot ===
03:34:12 [INFO] turtlequant_bot: Mode        : LIVE
03:34:12 [INFO] turtlequant_bot: Entry thresh: 0.060 (6.0%)
03:34:12 [INFO] turtlequant_bot: Kelly frac  : 0.20
03:34:12 [INFO] turtlequant_bot: Sides       : YES,NO
03:34:12 [INFO] turtlequant_bot: Min entry   : 168h to resolution
03:34:12 [INFO] turtlequant_bot: Max entry   : 2160h to resolution
03:34:12 [INFO] turtlequant_bot: Exit rule   : ev
03:34:12 [INFO] turtlequant_bot: Starting NAV: $100.00
03:34:12 [INFO] turtlequant_bot: Max order   : $6.00
03:34:12 [INFO] turtlequant_bot: State dir   : /app/state
03:34:12 [INFO] turtlequant_bot:
03:34:13 [INFO] turtlequant.market_scanner: Scanner: 304 markets passed filters
"""


def _row(market_id, *, t="barrier", k=80000.0, days=30.0, bid=0.20, ask=0.21, pl=0.30, ps=0.29, question=None):
    exp = NOW + dt.timedelta(days=days)
    return {
        "id": market_id, "a": "btc", "t": t, "k": k, "exp": exp.isoformat(), "bid": bid, "ask": ask,
        "spot": 65000.0, "sigma_legacy": 0.5, "v": 0.5, "p_legacy_full": pl, "p_smile_full": ps,
        "contract": {"question": question or f"Will Bitcoin reach ${k:,.0f}? ({market_id})"},
    }


def _marks(rows):
    return {"event": "market_marks", "ts": NOW.isoformat(), "spots": {"btc": 65000.0}, "rows": rows}


def test_parse_banner_reads_the_last_banner():
    rules = parse_banner((BANNER.replace("0.060 (6.0%)", "0.050 (5.0%)") + BANNER).splitlines(keepends=True))
    assert rules.entry_threshold == 0.06
    assert rules.kelly_fraction == 0.20
    assert rules.max_order_usd == 6.0
    assert rules.min_entry_hours == 168 and rules.max_entry_hours == 2160
    assert rules.from_banner


def test_load_bot_rules_falls_back_to_rotated_logs_then_defaults(tmp_path):
    assert load_bot_rules(tmp_path) == BotRules()
    (tmp_path / "turtlequant-bot.log").write_text("no banner here\n")
    (tmp_path / "turtlequant-bot.log.2").write_text(BANNER)
    assert load_bot_rules(tmp_path).entry_threshold == 0.06


def test_latest_marks_skips_a_torn_first_line(tmp_path):
    first = json.dumps(_marks([_row("a")]))
    last = json.dumps(_marks([_row("b")]))
    (tmp_path / "turtlequant-marks.jsonl").write_text(first + "\n" + last + "\n")
    record = latest_marks(tmp_path, tail_bytes=len(last) + 20)
    assert record["rows"][0]["id"] == "b"


def test_lab_markets_keeps_enterable_types_inside_the_window():
    rows = [
        _row("in"),
        _row("short", days=3),
        _row("long", days=200),
        _row("range", t="range"),
        _row("put", t="european_put"),
    ]
    markets, default = lab_markets(_marks(rows), BotRules())
    assert [m["q"] for m in markets] == ["Will Bitcoin reach $80,000? (in)"]
    assert default == 0


def test_lab_default_is_the_mid_priced_market_closest_to_a_trade():
    rows = [
        _row("tail", k=150000, bid=0.01, ask=0.02, pl=0.20, ps=0.20),  # big edge, but a 2c tail
        _row("near", k=90000, bid=0.20, ask=0.21, pl=0.25, ps=0.24),
        _row("far", k=85000, bid=0.30, ask=0.31, pl=0.31, ps=0.30),
    ]
    markets, default = lab_markets(_marks(rows), BotRules())
    assert "(near)" in markets[default]["q"]


def test_strategy_page_has_no_inline_script_and_escapes_data():
    rows = [_row("x", question="</script><script>alert(1)</script>")]
    page = render_strategy_page(rules=BotRules(max_order_usd=6.0), marks=_marks(rows), shadow_trades=[],
                                live_nav=100.0, generated_at=NOW)
    scripts = re.findall(r"<script([^>]*)>(.*?)</script>", page, re.DOTALL)
    assert all("src=" in attrs or "application/json" in attrs for attrs, _ in scripts)
    assert '<script src="strategy.js"></script>' in page
    assert "<script>alert" not in page
    block = re.search(r'id="strategy-data">(.*?)</script>', page, re.DOTALL).group(1)
    assert json.loads(block)["markets"][0]["q"].startswith("</script>")
    assert "$6" in page


def test_generator_writes_the_strategy_page(tmp_path):
    from test_performance_page import _load_generator

    module = _load_generator()
    state_dir = tmp_path / "state"
    state_dir.mkdir()
    (state_dir / "turtlequant-marks.jsonl").write_text(json.dumps(_marks([_row("a")])) + "\n")
    live = state_dir / "live-state"
    live.mkdir()
    (live / "turtlequant-positions.json").write_text(json.dumps({"nav": 100.0, "total_pnl": 0.0, "positions": []}))
    (live / "turtlequant-bot.log").write_text(BANNER)
    site = tmp_path / "site"

    assert module.main(["--state-dir", str(state_dir), "--output", str(site / "index.html")]) == 0

    page = (site / "strategy" / "index.html").read_text()
    assert "6¢" in page and "startup log" in page
    assert (site / "strategy" / "strategy.js").read_text() == STRATEGY_JS
