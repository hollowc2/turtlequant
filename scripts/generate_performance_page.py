#!/usr/bin/env python3
"""Generate the public TurtleQuant pages on billybitcoin.cloud.

Reads the bots' state files and writes two static pages. Run hourly from host
cron (see scripts/performance_page.cron).

- <output>: the live bot's portfolio (when <live-state-dir> has a positions
  file) above the shadow run's track record.
- <strategy-dir>/index.html and strategy.js: the strategy explainer, with rules
  from the live bot's startup banner and a lab seeded from the last scan.

The track record shows only the current shadow run. A state reset moves the old
files into <state-dir>/archive/<YYYYmmddTHHMMSSZ>/, and those runs are not published.

Usage:
    uv run python scripts/generate_performance_page.py
    uv run python scripts/generate_performance_page.py \\
        --state-dir /opt/turtlequant/state --output /tmp/turtlequant/index.html
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import UTC, datetime
from pathlib import Path

from turtlequant.history import load_history
from turtlequant.performance_page import (
    MODE_LABELS,
    LivePortfolio,
    RunSummary,
    build_closed_trades,
    open_positions_from_state,
    parse_ts,
    render_page,
)
from turtlequant.strategy_page import (
    STRATEGY_JS,
    latest_marks,
    load_bot_rules,
    render_strategy_page,
)

POSITIONS_FILE = "turtlequant-positions.json"
RISK_FILE = "turtlequant-risk.json"
LIVE_SUBDIR = "live-state"
DEFAULT_STATE_DIR = Path("/opt/turtlequant/state")
DEFAULT_OUTPUT = Path("/var/www/billybitcoin.cloud/html/turtlequant/index.html")
DEFAULT_STARTING_NAV = 1000.0


def _load_object(path: Path) -> dict:
    if not path.exists():
        return {}
    data = json.loads(path.read_text())
    if not isinstance(data, dict):
        raise ValueError(f"state file must be a JSON object: {path}")
    return data


def load_positions(state_dir: Path) -> dict:
    return _load_object(state_dir / POSITIONS_FILE)


def load_live(live_dir: Path) -> LivePortfolio | None:
    """The live bot's book, or None before it has ever started."""
    state = load_positions(live_dir)
    if not state:
        return None
    risk = _load_object(live_dir / RISK_FILE)
    trades = build_closed_trades(load_history(live_dir))
    try:
        wallet = float(risk["wallet_collateral_usd"])
    except (KeyError, TypeError, ValueError):
        wallet = None
    halt = str(risk.get("halt_reason") or risk.get("entry_halt") or "")
    return LivePortfolio(
        starting_nav=starting_nav(state, None),
        nav=float(state.get("nav", 0.0)),
        trades=trades,
        open_positions=open_positions_from_state(state),
        wallet_usd=wallet,
        wallet_at=parse_ts(risk.get("wallet_balance_at")),
        halt=halt,
    )


def starting_nav(state: dict, override: float | None) -> float:
    """NAV before any realized P&L: the file's nav minus its cumulative total_pnl."""
    if override is not None:
        return override
    try:
        return float(state["nav"]) - float(state.get("total_pnl", 0.0))
    except (KeyError, TypeError, ValueError):
        return DEFAULT_STARTING_NAV


def write_atomic(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = path.with_suffix(path.suffix + ".tmp")
    tmp_path.write_text(content, encoding="utf-8")
    tmp_path.replace(path)


def generate(
    *,
    state_dir: Path,
    output: Path,
    mode: str,
    nav_override: float | None,
    live_dir: Path | None = None,
    strategy_dir: Path | None = None,
) -> int:
    state = load_positions(state_dir)
    events = load_history(state_dir)
    trades = build_closed_trades(events)
    live = load_live(live_dir) if live_dir is not None else None
    generated_at = datetime.now(UTC)
    stamps = [ts for ts in (parse_ts(e.get("ts")) for e in events) if ts is not None]
    current = RunSummary(
        run_id="current",
        path="",
        label="Current run",
        started_at=min(stamps, default=None),
        ended_at=None,
        trade_count=len(trades),
        total_pnl=sum(t.pnl for t in trades),
    )
    write_atomic(output, render_page(
        trades=trades,
        open_positions=open_positions_from_state(state),
        starting_nav=starting_nav(state, nav_override),
        generated_at=generated_at,
        mode=mode,
        runs=[current],
        active_run=current.run_id,
        live=live,
    ))
    if strategy_dir is not None:
        # Rules from the bot that trades real money; marks from shadow, which
        # prices the same board every scan.
        rules_dir = live_dir if live is not None and live_dir is not None else state_dir
        write_atomic(strategy_dir / "strategy.js", STRATEGY_JS)
        write_atomic(strategy_dir / "index.html", render_strategy_page(
            rules=load_bot_rules(rules_dir),
            marks=latest_marks(state_dir) or (latest_marks(live_dir) if live_dir else None),
            shadow_trades=trades,
            live_nav=live.nav if live is not None else None,
            generated_at=generated_at,
        ))
    return len(trades)


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Generate the TurtleQuant performance page")
    parser.add_argument("--state-dir", type=Path, default=DEFAULT_STATE_DIR)
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    parser.add_argument(
        "--mode",
        choices=sorted(MODE_LABELS),
        default="shadow",
        help="Labels the page; must match the state dir (shadow: /opt/turtlequant/state)",
    )
    parser.add_argument("--starting-nav", type=float, default=None, help="Override the derived starting NAV")
    parser.add_argument(
        "--live-state-dir",
        type=Path,
        default=None,
        help=f"Live bot state shown above the track record (default: <state-dir>/{LIVE_SUBDIR}; skipped if it has no positions file)",
    )
    parser.add_argument("--no-live", action="store_true", help="Leave the live portfolio off the page")
    parser.add_argument(
        "--strategy-dir",
        type=Path,
        default=None,
        help="Where the strategy explainer goes (default: <output dir>/strategy)",
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    try:
        count = generate(
            state_dir=args.state_dir,
            output=args.output,
            mode=args.mode,
            nav_override=args.starting_nav,
            live_dir=None if args.no_live else (args.live_state_dir or args.state_dir / LIVE_SUBDIR),
            strategy_dir=args.strategy_dir or args.output.parent / "strategy",
        )
    except Exception as exc:
        print(f"generate_performance_page failed: {exc}", file=sys.stderr)
        return 1
    print(f"{datetime.now(UTC).isoformat()} wrote {args.output} ({count} closed trades)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
