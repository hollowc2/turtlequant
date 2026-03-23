#!/usr/bin/env python3
# /// script
# dependencies = ["rich>=13.0"]
# ///
"""Live dashboard for the SlowQuant paper/live bot.

Shows open positions, regime state, recent open/close events, and log tail.

Usage:
    uv run --script scripts/monitor_slowquant.py
    uv run --script scripts/monitor_slowquant.py --live
    uv run --script scripts/monitor_slowquant.py --live --interval 10
    uv run --script scripts/monitor_slowquant.py --state-dir /opt/turtlequant/state/slowquant
"""

from __future__ import annotations

import argparse
import json
import os
import time
from datetime import UTC, datetime
from pathlib import Path

from rich.console import Console, Group  # type: ignore[import-untyped]
from rich.live import Live  # type: ignore[import-untyped]
from rich.panel import Panel  # type: ignore[import-untyped]
from rich.table import Table  # type: ignore[import-untyped]
from rich.text import Text  # type: ignore[import-untyped]

_DEFAULT_STATE_DIR = Path(os.getenv("STATE_DIR", "/opt/turtlequant/state/slowquant"))
PAPER_TRADE = os.getenv("PAPER_TRADE", "true").lower() == "true"


# ── Data loading ──────────────────────────────────────────────────────────────


def load_positions(state_dir: Path) -> tuple[float, list[dict]]:
    path = state_dir / "slowquant-positions.json"
    if not path.exists():
        return 1000.0, []
    try:
        data = json.loads(path.read_text())
        return float(data.get("nav", 1000.0)), data.get("positions", [])
    except Exception:
        return 1000.0, []


def load_history(state_dir: Path) -> list[dict]:
    path = state_dir / "slowquant-history.json"
    if not path.exists():
        return []
    try:
        return json.loads(path.read_text())
    except Exception:
        return []


def tail_log(state_dir: Path, n: int = 6) -> list[str]:
    log_file = os.getenv("LOG_FILE", "")
    candidates = [Path(log_file)] if log_file else []
    candidates += [
        state_dir / "slowquant-bot.log",
        state_dir.parent / "slowquant-bot.log",
    ]
    for p in candidates:
        if p.exists():
            return p.read_text().splitlines()[-n:]
    return []


# ── Regime helpers ────────────────────────────────────────────────────────────


def _regime_style(regime: str) -> str:
    return {"normal": "green", "elevated": "yellow", "spike": "bold red"}.get(regime.lower(), "dim")


# ── Dashboard ─────────────────────────────────────────────────────────────────


def build_dashboard(state_dir: Path) -> Group:
    nav, positions = load_positions(state_dir)
    history = load_history(state_dir)
    now_str = datetime.now(UTC).strftime("%Y-%m-%d %H:%M:%S UTC")

    opens = [e for e in history if e.get("event") == "open"]
    closes = [e for e in history if e.get("event") == "close"]

    # Infer last regime from most recent history entry that has it
    last_regime = "—"
    for e in reversed(history):
        if e.get("regime"):
            last_regime = e["regime"]
            break

    mode = "PAPER" if PAPER_TRADE else "LIVE"
    mode_style = "bold yellow" if PAPER_TRADE else "bold red"

    # ── Header ────────────────────────────────────────────────────────────────
    hdr = Text()
    hdr.append("SlowQuant  │  ", style="bold magenta")
    hdr.append(f"Mode: {mode}", style=mode_style)
    hdr.append(f"  │  NAV: ${nav:.2f}", style="bold white")
    hdr.append(f"  │  Open: {len(positions)}  Entries: {len(opens)}  Exits: {len(closes)}", style="dim")
    if last_regime != "—":
        hdr.append(f"  │  Regime: {last_regime.upper()}", style=_regime_style(last_regime))
    hdr.append(f"  │  {now_str}", style="dim")
    header_panel = Panel(hdr, title="[bold blue]SlowQuant Monitor[/bold blue]", border_style="blue")

    # ── Open positions ─────────────────────────────────────────────────────────
    pos_table = Table(
        show_header=True,
        header_style="bold dim",
        border_style="dim",
        show_edge=False,
        expand=True,
    )
    pos_table.add_column("Question", min_width=42)
    pos_table.add_column("Type", min_width=12)
    pos_table.add_column("Strike", justify="right", min_width=9)
    pos_table.add_column("Expiry", min_width=17)
    pos_table.add_column("Entry P", justify="right", min_width=8)
    pos_table.add_column("MC Prob", justify="right", min_width=8)
    pos_table.add_column("Edge@Entry", justify="right", min_width=11)
    pos_table.add_column("Size $", justify="right", min_width=7)
    pos_table.add_column("Hrs Left", justify="right", min_width=9)

    if not positions:
        pos_table.add_row(Text("(no open positions)", style="dim"), *[""] * 8)
    else:
        for p in positions:
            edge = p.get("edge_at_entry", 0.0)
            edge_style = "bold green" if edge > 0.04 else ("yellow" if edge > 0 else "red")
            exp_iso = p.get("expiry_iso", "")
            exp_str = exp_iso[:16].replace("T", " ")
            hrs_left = "—"
            try:
                exp_dt = datetime.fromisoformat(exp_iso)
                hl = (exp_dt - datetime.now(UTC)).total_seconds() / 3600
                hrs_left = f"{hl:.1f}h" if hl >= 0 else Text("expired", style="red")
            except Exception:
                pass

            pos_table.add_row(
                Text(p.get("question", "")[:55], style="white"),
                Text(p.get("option_type", ""), style="cyan"),
                Text(f"${p.get('strike', 0):,.0f}"),
                Text(exp_str, style="dim"),
                Text(f"{p.get('entry_price', 0):.3f}"),
                Text(f"{p.get('model_prob_at_entry', 0):.3f}"),
                Text(f"{edge:+.3f}", style=edge_style),
                Text(f"${p.get('size_usd', 0):.2f}"),
                Text(str(hrs_left), style="dim"),
            )

    pos_panel = Panel(pos_table, title=f"[dim]OPEN POSITIONS ({len(positions)})[/dim]", border_style="dim")

    # ── Stats summary ──────────────────────────────────────────────────────────
    total_open_usd = sum(p.get("size_usd", 0) for p in positions)
    exposure_pct = (total_open_usd / nav * 100) if nav > 0 else 0

    stats_grid = Table.grid(padding=(0, 2))
    stats_grid.add_row(
        Text(f"Entries: {len(opens)}", style="dim"),
        Text(f"Exits: {len(closes)}", style="dim"),
        Text(f"Exposure: ${total_open_usd:.2f} ({exposure_pct:.1f}%)", style="cyan"),
        Text(f"Regime: {last_regime.upper()}", style=_regime_style(last_regime)),
    )
    stats_panel = Panel(stats_grid, title="[dim]SUMMARY[/dim]", border_style="dim")

    # ── Recent events ──────────────────────────────────────────────────────────
    ev_table = Table(
        show_header=True,
        header_style="bold dim",
        border_style="dim",
        show_edge=False,
        expand=True,
    )
    ev_table.add_column("Time (UTC)", style="dim", min_width=17)
    ev_table.add_column("Event", min_width=6)
    ev_table.add_column("Asset", min_width=5)
    ev_table.add_column("Type", min_width=12)
    ev_table.add_column("Strike", justify="right", min_width=9)
    ev_table.add_column("MC Prob", justify="right", min_width=8)
    ev_table.add_column("Mkt P", justify="right", min_width=7)
    ev_table.add_column("Edge", justify="right", min_width=7)
    ev_table.add_column("Regime", min_width=8)

    recent = list(reversed(history[-12:]))
    if not recent:
        ev_table.add_row(Text("(no events yet)", style="dim"), *[""] * 8)
    else:
        for e in recent:
            ev = e.get("event", "?")
            ev_text = Text("OPEN", style="bold green") if ev == "open" else Text("CLOSE", style="bold red")
            ts = e.get("ts", "")
            try:
                ts_str = datetime.fromisoformat(ts).strftime("%m-%d %H:%M:%S")
            except Exception:
                ts_str = ts[:16]

            edge = e.get("edge", 0.0)
            regime = e.get("regime", "—")
            ev_table.add_row(
                Text(ts_str, style="dim"),
                ev_text,
                Text(e.get("asset", "").upper(), style="cyan"),
                Text(e.get("option_type", ""), style="dim"),
                Text(f"${e.get('strike', 0):,.0f}"),
                Text(f"{e.get('mc_prob', e.get('model_prob', 0)):.3f}"),
                Text(f"{e.get('yes_price', 0):.3f}"),
                Text(f"{edge:+.3f}", style="green" if edge > 0 else "red"),
                Text(regime, style=_regime_style(regime)),
            )

    ev_panel = Panel(ev_table, title="[dim]RECENT EVENTS (last 12)[/dim]", border_style="dim")

    # ── Log tail ───────────────────────────────────────────────────────────────
    renderables: list = [header_panel, stats_panel, pos_panel, ev_panel]
    log_lines = tail_log(state_dir)
    if log_lines:
        log_text = Text("\n".join(log_lines), style="dim", overflow="fold")
        renderables.append(Panel(log_text, title="[dim]BOT LOG (last lines)[/dim]", border_style="dim"))

    return Group(*renderables)


# ── Entry point ────────────────────────────────────────────────────────────────


def main() -> None:
    parser = argparse.ArgumentParser(description="SlowQuant bot monitor")
    parser.add_argument("--live", action="store_true", help="Auto-refresh (Ctrl-C to exit)")
    parser.add_argument("--interval", type=int, default=10, metavar="SEC", help="Refresh interval (default: 10)")
    parser.add_argument(
        "--state-dir",
        type=Path,
        default=_DEFAULT_STATE_DIR,
        help=f"State directory (default: {_DEFAULT_STATE_DIR})",
    )
    args = parser.parse_args()

    if args.live:
        with Live(build_dashboard(args.state_dir), refresh_per_second=0.1, screen=True) as live:
            try:
                while True:
                    time.sleep(args.interval)
                    live.update(build_dashboard(args.state_dir))
            except KeyboardInterrupt:
                pass
    else:
        Console().print(build_dashboard(args.state_dir))


if __name__ == "__main__":
    main()
