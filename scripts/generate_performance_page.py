#!/usr/bin/env python3
"""Generate the public TurtleQuant performance page on billybitcoin.cloud.

Reads the bot's state files and writes a static page. Run hourly from host cron
(see scripts/performance_page.cron).

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
    build_closed_trades,
    open_positions_from_state,
    render_page,
)

POSITIONS_FILE = "turtlequant-positions.json"
DEFAULT_STATE_DIR = Path("/opt/turtlequant/state")
DEFAULT_OUTPUT = Path("/var/www/billybitcoin.cloud/html/turtlequant/index.html")
DEFAULT_STARTING_NAV = 1000.0


def load_positions(state_dir: Path) -> dict:
    path = state_dir / POSITIONS_FILE
    if not path.exists():
        return {}
    data = json.loads(path.read_text())
    if not isinstance(data, dict):
        raise ValueError(f"positions file must be a JSON object: {path}")
    return data


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


def generate(*, state_dir: Path, output: Path, mode: str, nav_override: float | None) -> int:
    state = load_positions(state_dir)
    trades = build_closed_trades(load_history(state_dir))
    page = render_page(
        trades=trades,
        open_positions=open_positions_from_state(state),
        starting_nav=starting_nav(state, nav_override),
        generated_at=datetime.now(UTC),
        mode=mode,
    )
    write_atomic(output, page)
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
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    try:
        count = generate(
            state_dir=args.state_dir,
            output=args.output,
            mode=args.mode,
            nav_override=args.starting_nav,
        )
    except Exception as exc:
        print(f"generate_performance_page failed: {exc}", file=sys.stderr)
        return 1
    print(f"{datetime.now(UTC).isoformat()} wrote {args.output} ({count} closed trades)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
