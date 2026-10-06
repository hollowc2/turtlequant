#!/usr/bin/env python3
"""Run the strategy checkpoint and post its summary to Discord.

Runs evaluate_models.py over all tenors and over the deployed entry window,
plus exit_counterfactual.py, saves the full output under ``--out-dir`` and
posts the summary tables to the TurtleQuant Discord webhook. The decision
rules these numbers feed are in the commit that added this script.

The decision tables score only DECISION_TYPES, the market types the bot
priced when the rules were fixed. "less than" and "between" markets, scored
from 2026-10-05, are reported in a separate section of the saved report.

Usage (cron on the VPS, from /opt/turtlequant-app):
    uv run python scripts/checkpoint_report.py --state-dir /opt/turtlequant/state
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import urllib.request
from datetime import UTC, datetime
from pathlib import Path

SCRIPTS = Path(__file__).resolve().parent
DEFAULT_WEBHOOK_FILE = "/opt/monitoring/alertmanager/secrets/turtlequant-webhook"
DISCORD_LIMIT = 2000
DECISION_TYPES = "european,barrier,barrier_down"
SCORE_ONLY_TYPES = "european_put,range"


def _run(script: str, *args: str) -> str:
    result = subprocess.run(
        [sys.executable, str(SCRIPTS / script), *args], capture_output=True, text=True, timeout=3600
    )
    return result.stdout + (f"\n[exit {result.returncode}] {result.stderr[-500:]}" if result.returncode else "")


def _summary(output: str) -> str:
    """The header line and model table of evaluate_models output (no reliability tables)."""
    return output.split("\nreliability", 1)[0].strip()


def _post(webhook: str, content: str) -> None:
    request = urllib.request.Request(
        webhook,
        data=json.dumps({"content": content[:DISCORD_LIMIT]}).encode(),
        headers={"Content-Type": "application/json", "User-Agent": "turtlequant-checkpoint"},
    )
    urllib.request.urlopen(request, timeout=20).close()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="TurtleQuant strategy checkpoint")
    parser.add_argument("--state-dir", required=True)
    parser.add_argument("--out-dir", type=Path, default=Path("checkpoints"))
    parser.add_argument("--min-entry-hours", default="168")
    parser.add_argument("--max-entry-hours", default="2160")
    parser.add_argument("--no-post", action="store_true")
    args = parser.parse_args(argv)

    state = ["--state-dir", args.state_dir]
    all_tenors = _run("evaluate_models.py", *state, "--option-types", DECISION_TYPES)
    window = _run(
        "evaluate_models.py", *state, "--option-types", DECISION_TYPES,
        "--min-entry-hours", args.min_entry_hours, "--max-entry-hours", args.max_entry_hours,
    )
    score_only = _run("evaluate_models.py", *state, "--option-types", SCORE_ONLY_TYPES)
    exits = _run("exit_counterfactual.py", *state)

    stamp = datetime.now(UTC).strftime("%Y-%m-%d")
    args.out_dir.mkdir(parents=True, exist_ok=True)
    report = args.out_dir / f"checkpoint-{stamp}.txt"
    report.write_text(
        f"== all tenors ==\n{all_tenors}\n== entry window {args.min_entry_hours}-{args.max_entry_hours}h ==\n"
        f"{window}\n== exit counterfactual ==\n{exits}\n"
        f"== less than / between markets (scored only, not in the decision rules) ==\n{score_only}\n"
    )
    print(report.read_text())

    message = (
        f"**TurtleQuant checkpoint {stamp}**\n"
        f"Entry window {args.min_entry_hours}-{args.max_entry_hours}h:\n```\n{_summary(window)}\n```"
        f"All tenors:\n```\n{_summary(all_tenors)}\n```"
        f"Exits:\n```\n{exits.strip()[:400]}\n```"
        f"Full report: {report.resolve()}"
    )
    if args.no_post:
        return 0
    webhook_file = Path(os.getenv("DISCORD_WEBHOOK_FILE", DEFAULT_WEBHOOK_FILE))
    if not webhook_file.exists():
        print(f"no webhook at {webhook_file}; report saved only", file=sys.stderr)
        return 1
    _post(webhook_file.read_text().strip(), message)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
