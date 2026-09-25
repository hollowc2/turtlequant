#!/usr/bin/env python3
"""One-off: move diagnostics out of an existing turtlequant-history.jsonl.

Before the split, every scan appended scan_summary / signal_evaluation /
shadow_quote events (with book depth) to the trade history, so the file grows
without bound. The bot now writes those to the rotated
turtlequant-diagnostics.jsonl; this script cleans up history written before.

Trade events stay in turtlequant-history.jsonl (same order). Old diagnostics
are gzipped to turtlequant-diagnostics-archive-<ts>.jsonl.gz (or dropped with
--drop-diagnostics). The original file is kept as a hard link
turtlequant-history.jsonl.bak-<ts> (no extra disk) until you delete it.

Stop the bot first: it appends to the history file.

Usage:
    python scripts/split_history.py --state-dir /opt/turtlequant/state --dry-run
    python scripts/split_history.py --state-dir /opt/turtlequant/state
"""

from __future__ import annotations

import argparse
import gzip
import json
import os
import sys
import time
from datetime import UTC, datetime
from pathlib import Path

from turtlequant.history import DIAGNOSTIC_EVENTS, HISTORY_JSONL

BOT_LOG = "turtlequant-bot.log"


def split(state_dir: Path, *, dry_run: bool, drop_diagnostics: bool) -> dict[str, int]:
    source = state_dir / HISTORY_JSONL
    stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    tmp = source.with_name(f".{source.name}.split.tmp")
    archive_path = state_dir / f"turtlequant-diagnostics-archive-{stamp}.jsonl.gz"
    counts = {"trade": 0, "diagnostic": 0, "unparseable": 0}

    trades = None if dry_run else tmp.open("w")
    archive = None if dry_run or drop_diagnostics else gzip.open(archive_path, "wt")
    try:
        with source.open() as history:
            for line in history:
                if not line.strip():
                    continue
                try:
                    event = json.loads(line)
                except json.JSONDecodeError:
                    event = None
                if isinstance(event, dict) and event.get("event") in DIAGNOSTIC_EVENTS:
                    counts["diagnostic"] += 1
                    if archive is not None:
                        archive.write(line if line.endswith("\n") else line + "\n")
                    continue
                if not isinstance(event, dict):
                    counts["unparseable"] += 1  # kept: never silently drop ledger lines
                counts["trade"] += 1
                if trades is not None:
                    trades.write(line if line.endswith("\n") else line + "\n")
        if trades is not None:
            trades.flush()
            os.fsync(trades.fileno())
    finally:
        if trades is not None:
            trades.close()
        if archive is not None:
            archive.close()

    if not dry_run:
        os.link(source, source.with_name(f"{source.name}.bak-{stamp}"))
        os.replace(tmp, source)
    return counts


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--state-dir", type=Path, required=True)
    parser.add_argument("--dry-run", action="store_true", help="Only count events")
    parser.add_argument("--drop-diagnostics", action="store_true", help="Do not archive old diagnostics")
    parser.add_argument("--force", action="store_true", help="Run even if the bot log looks active")
    args = parser.parse_args(argv)

    source = args.state_dir / HISTORY_JSONL
    if not source.exists():
        print(f"no {source}", file=sys.stderr)
        return 1
    log = args.state_dir / BOT_LOG
    if not args.dry_run and not args.force and log.exists() and time.time() - log.stat().st_mtime < 300:
        print(f"{log} was written in the last 5 minutes; stop the bot or pass --force", file=sys.stderr)
        return 1
    counts = split(args.state_dir, dry_run=args.dry_run, drop_diagnostics=args.drop_diagnostics)
    verb = "would keep" if args.dry_run else "kept"
    print(
        f"{verb} {counts['trade']} trade events ({counts['unparseable']} unparseable lines kept), "
        f"moved {counts['diagnostic']} diagnostic events"
        + ("" if args.dry_run else " — original kept as a .bak hard link")
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
