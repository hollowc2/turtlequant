#!/usr/bin/env python3
"""Generate the public TurtleQuant performance page on billybitcoin.cloud.

Reads the bot's state files and writes a static page. Run hourly from host cron
(see scripts/performance_page.cron).

A state reset moves the old files into <state-dir>/archive/<YYYYmmddTHHMMSSZ>/.
Each archived run gets a frozen page at runs/<ts>/ next to the main page, and
runs/all/ chains every run's closed trades onto the first run's starting NAV.
The main page always shows the current run.

Usage:
    uv run python scripts/generate_performance_page.py
    uv run python scripts/generate_performance_page.py \\
        --state-dir /opt/turtlequant/state --output /tmp/turtlequant/index.html
"""

from __future__ import annotations

import argparse
import json
import sys
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from turtlequant.history import load_history
from turtlequant.performance_page import (
    MODE_LABELS,
    ClosedTrade,
    OpenPosition,
    RunSummary,
    build_closed_trades,
    open_positions_from_state,
    parse_ts,
    render_page,
)

POSITIONS_FILE = "turtlequant-positions.json"
DEFAULT_STATE_DIR = Path("/opt/turtlequant/state")
DEFAULT_OUTPUT = Path("/var/www/billybitcoin.cloud/html/turtlequant/index.html")
DEFAULT_STARTING_NAV = 1000.0
ARCHIVE_DIR = "archive"
ARCHIVE_STAMP = "%Y%m%dT%H%M%SZ"
RUNS_DIR = "runs"


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


@dataclass(frozen=True)
class Run:
    summary: RunSummary
    trades: list[ClosedTrade]
    open_positions: list[OpenPosition]
    starting_nav: float


def load_run(
    state_dir: Path, *, run_id: str, path: str, label: str, ended_at: datetime | None, nav_override: float | None
) -> Run:
    state = load_positions(state_dir)
    events = load_history(state_dir)
    trades = build_closed_trades(events)
    stamps = [ts for ts in (parse_ts(e.get("ts")) for e in events) if ts is not None]
    return Run(
        summary=RunSummary(
            run_id=run_id,
            path=path,
            label=label,
            started_at=min(stamps, default=None),
            ended_at=ended_at,
            trade_count=len(trades),
            total_pnl=sum(t.pnl for t in trades),
        ),
        trades=trades,
        open_positions=open_positions_from_state(state),
        starting_nav=starting_nav(state, nav_override),
    )


def archived_runs(state_dir: Path) -> list[Run]:
    """Archived runs, oldest first. Skips directories not named by timestamp or holding no history."""
    runs = []
    archive = state_dir / ARCHIVE_DIR
    for run_dir in sorted(archive.iterdir()) if archive.is_dir() else ():
        try:
            ended_at = datetime.strptime(run_dir.name, ARCHIVE_STAMP).replace(tzinfo=UTC)
        except ValueError:
            continue
        if not run_dir.is_dir() or not any(run_dir.glob("turtlequant-history.json*")):
            continue
        runs.append(load_run(
            run_dir,
            run_id=run_dir.name,
            path=f"{RUNS_DIR}/{run_dir.name}/",
            label=f"Run ended {ended_at:%Y-%m-%d}",
            ended_at=ended_at,
            nav_override=None,
        ))
    return runs


def chain_runs(runs: list[Run]) -> Run:
    """Every run's closed trades on one curve, starting from the first run's NAV."""
    trades = sorted((t for run in runs for t in run.trades), key=lambda t: t.closed_at)
    return Run(
        summary=RunSummary(
            run_id="all",
            path=f"{RUNS_DIR}/all/",
            label="All runs, chained",
            started_at=min((r.summary.started_at for r in runs if r.summary.started_at), default=None),
            ended_at=None,
            trade_count=len(trades),
            total_pnl=sum(t.pnl for t in trades),
        ),
        trades=trades,
        open_positions=runs[-1].open_positions,
        starting_nav=runs[0].starting_nav,
    )


def generate(*, state_dir: Path, output: Path, mode: str, nav_override: float | None) -> int:
    current = load_run(
        state_dir, run_id="current", path="", label="Current run", ended_at=None, nav_override=nav_override
    )
    archived = archived_runs(state_dir)
    # Strip order: current run, all runs chained, then archived runs newest first.
    pages: list[tuple[Run, Path, str]] = [(current, output, "")]
    if archived:
        chained = chain_runs([*archived, current])
        for run in [chained, *reversed(archived)]:
            pages.append((run, output.parent / run.summary.path / output.name, "../../"))
    summaries = [run.summary for run, _, _ in pages]
    generated_at = datetime.now(UTC)
    for run, path, root_href in pages:
        write_atomic(path, render_page(
            trades=run.trades,
            open_positions=run.open_positions,
            starting_nav=run.starting_nav,
            generated_at=generated_at,
            mode=mode,
            runs=summaries,
            active_run=run.summary.run_id,
            root_href=root_href,
        ))
    return current.summary.trade_count


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
    print(f"{datetime.now(UTC).isoformat()} wrote {args.output} ({count} closed trades in the current run)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
