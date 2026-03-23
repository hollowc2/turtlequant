#!/usr/bin/env python3
# /// script
# dependencies = ["textual>=0.70", "rich>=13.0"]
# ///
"""QuantDash — unified Textual TUI monitor for all TurtleQuant family strategies.

Two-pane layout:
  Top  — one card per strategy with heavy metrics; click or arrow keys to select
  Bottom — positions table + trade history + log tail for the selected strategy

Adding a new strategy: add one StrategyMeta entry to build_registry().

Usage:
    uv run python scripts/monitor_all.py
    uv run python scripts/monitor_all.py --refresh 10
    uv run python scripts/monitor_all.py --state-root /opt/turtlequant/state
"""

from __future__ import annotations

import argparse
import json
import os
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from statistics import mean
from typing import Any

from textual import events
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, ScrollableContainer, Vertical
from textual.message import Message
from textual.reactive import reactive
from textual.widgets import DataTable, Footer, Header, Label, Static


# ── Strategy registry ─────────────────────────────────────────────────────────


@dataclass
class StrategyMeta:
    """Describes one strategy — where its state lives and how to display it."""

    name: str           # display name
    key: str            # slug used in widget IDs and file names
    state_dir: Path     # host-side state directory
    positions_file: str
    history_file: str
    log_file: str
    model_label: str = "Model P"   # column header for the probability field
    has_regime: bool = False        # show regime column/metric
    has_bs_mc: bool = False         # show BS vs MC breakdown
    color: str = "cyan"             # card accent color
    # NAV limits shown in card (informational)
    max_per_market_pct: float = 0.10
    max_total_exposure_pct: float = 0.40


def build_registry(state_root: Path) -> list[StrategyMeta]:
    """All known strategies. Add a new StrategyMeta here to include it in the TUI."""
    return [
        StrategyMeta(
            name="TurtleQuant",
            key="turtlequant",
            state_dir=state_root,
            positions_file="turtlequant-positions.json",
            history_file="turtlequant-history.json",
            log_file="turtlequant-bot.log",
            model_label="Model P",
            has_regime=False,
            has_bs_mc=False,
            color="cyan",
            max_per_market_pct=0.10,
            max_total_exposure_pct=0.40,
        ),
        StrategyMeta(
            name="SlowQuant",
            key="slowquant",
            state_dir=state_root / "slowquant",
            positions_file="slowquant-positions.json",
            history_file="slowquant-history.json",
            log_file="slowquant-bot.log",
            model_label="MC Prob",
            has_regime=True,
            has_bs_mc=True,
            color="magenta",
            max_per_market_pct=0.015,
            max_total_exposure_pct=0.20,
        ),
    ]


# ── Data model ────────────────────────────────────────────────────────────────


@dataclass
class StrategyState:
    """Loaded and derived state for one strategy."""

    meta: StrategyMeta
    nav: float = 1000.0
    total_pnl: float = 0.0
    positions: list[dict] = field(default_factory=list)
    history: list[dict] = field(default_factory=list)
    log_lines: list[str] = field(default_factory=list)
    updated_at: str = ""
    # derived
    opens: list[dict] = field(default_factory=list)
    closes: list[dict] = field(default_factory=list)
    wins: list[dict] = field(default_factory=list)
    losses: list[dict] = field(default_factory=list)

    def refresh(self) -> None:
        m = self.meta
        pf = m.state_dir / m.positions_file
        if pf.exists():
            try:
                d = json.loads(pf.read_text())
                self.nav = float(d.get("nav", 1000.0))
                self.total_pnl = float(d.get("total_pnl", 0.0))
                self.positions = d.get("positions", [])
                self.updated_at = d.get("updated_at", "")
            except Exception:
                pass

        hf = m.state_dir / m.history_file
        if hf.exists():
            try:
                self.history = json.loads(hf.read_text())
            except Exception:
                pass

        lf = m.state_dir / m.log_file
        if lf.exists():
            try:
                self.log_lines = lf.read_text().splitlines()[-20:]
            except Exception:
                pass

        self.opens = [e for e in self.history if e.get("event") == "open"]
        self.closes = [e for e in self.history if e.get("event") == "close"]
        self.wins = [e for e in self.closes if e.get("pnl", 0) > 0]
        self.losses = [e for e in self.closes if e.get("pnl", 0) <= 0]

    # ── Derived metrics ───────────────────────────────────────────────────────

    @property
    def win_rate(self) -> float | None:
        return len(self.wins) / len(self.closes) if self.closes else None

    @property
    def avg_edge(self) -> float | None:
        vals = [e["edge"] for e in self.opens if "edge" in e]
        return mean(vals) if vals else None

    @property
    def avg_sigma(self) -> float | None:
        vals = [e["sigma"] for e in self.opens if "sigma" in e]
        return mean(vals) if vals else None

    @property
    def avg_model_prob(self) -> float | None:
        vals = [e.get("mc_prob", e.get("model_prob")) for e in self.opens]
        vals = [v for v in vals if v is not None]
        return mean(vals) if vals else None

    @property
    def avg_entry_price(self) -> float | None:
        vals = [e["yes_price"] for e in self.opens if "yes_price" in e]
        return mean(vals) if vals else None

    @property
    def avg_bs_prob(self) -> float | None:
        vals = [e["bs_prob"] for e in self.opens if "bs_prob" in e]
        return mean(vals) if vals else None

    @property
    def jump_premium(self) -> float | None:
        vals = [e["mc_prob"] - e["bs_prob"] for e in self.opens if "mc_prob" in e and "bs_prob" in e]
        return mean(vals) if vals else None

    @property
    def avg_score(self) -> float | None:
        vals = [e["score"] for e in self.opens if "score" in e]
        return mean(vals) if vals else None

    @property
    def total_volume(self) -> float:
        return sum(e.get("size_usd", 0) for e in self.opens)

    @property
    def avg_pnl_per_trade(self) -> float | None:
        if not self.closes:
            return None
        return sum(e.get("pnl", 0) for e in self.closes) / len(self.closes)

    @property
    def avg_hold_hours(self) -> float | None:
        open_ts: dict[str, datetime] = {}
        holds: list[float] = []
        for e in self.history:
            mid = e.get("market_id", "")
            try:
                ts = datetime.fromisoformat(e["ts"])
            except Exception:
                continue
            if e.get("event") == "open":
                open_ts[mid] = ts
            elif e.get("event") == "close" and mid in open_ts:
                holds.append((ts - open_ts[mid]).total_seconds() / 3600)
        return mean(holds) if holds else None

    @property
    def best_trade_pnl(self) -> float | None:
        vals = [e.get("pnl", 0) for e in self.closes]
        return max(vals) if vals else None

    @property
    def worst_trade_pnl(self) -> float | None:
        vals = [e.get("pnl", 0) for e in self.closes]
        return min(vals) if vals else None

    @property
    def current_exposure_usd(self) -> float:
        return sum(p.get("size_usd", 0) for p in self.positions)

    @property
    def current_exposure_pct(self) -> float:
        return (self.current_exposure_usd / self.nav * 100) if self.nav > 0 else 0.0

    @property
    def pnl_pct(self) -> float:
        starting = self.nav - self.total_pnl
        return (self.total_pnl / starting * 100) if starting > 0 else 0.0

    @property
    def last_regime(self) -> str:
        for e in reversed(self.history):
            if e.get("regime"):
                return e["regime"].upper()
        return "—"

    @property
    def last_event_ts(self) -> str:
        for e in reversed(self.history):
            try:
                return datetime.fromisoformat(e["ts"]).strftime("%m-%d %H:%M")
            except Exception:
                pass
        return "—"


# ── Helpers ───────────────────────────────────────────────────────────────────


def _v(v: float | None, fmt: str = ".3f", prefix: str = "", suffix: str = "") -> str:
    return "—" if v is None else f"{prefix}{v:{fmt}}{suffix}"


def _pnl(v: float | None) -> str:
    return "—" if v is None else f"{'+' if v >= 0 else ''}{v:.2f}"


def _pct(v: float | None) -> str:
    return "—" if v is None else f"{v:.1f}%"


def _time_left(exp_iso: str) -> str:
    try:
        exp_dt = datetime.fromisoformat(exp_iso)
        delta = exp_dt - datetime.now(UTC)
        total_h = delta.total_seconds() / 3600
        if total_h < 0:
            return "EXPIRED"
        if total_h < 24:
            return f"{total_h:.1f}h"
        return f"{delta.days}d {int(total_h % 24)}h"
    except Exception:
        return "—"


# ── Strategy Card widget ──────────────────────────────────────────────────────


class StrategyCard(Static):
    """Metric card for one strategy in the top pane. Click to select."""

    class Selected(Message):
        def __init__(self, index: int) -> None:
            self.index = index
            super().__init__()

    DEFAULT_CSS = """
    StrategyCard {
        width: 1fr;
        height: 100%;
        border: round $primary-darken-2;
        padding: 1 2;
        background: $surface;
    }
    StrategyCard.selected {
        border: double $accent;
        background: $surface-lighten-1;
    }
    StrategyCard > Label {
        width: 100%;
    }
    """

    def __init__(self, state: StrategyState, index: int, **kwargs: Any) -> None:
        super().__init__(**kwargs)
        self._state = state
        self._index = index

    def compose(self) -> ComposeResult:
        yield Label("", id=f"ct-{self._state.meta.key}")
        yield Label("", id=f"cb-{self._state.meta.key}")

    def on_mount(self) -> None:
        self._update_labels()

    def on_click(self, _event: events.Click) -> None:
        self.post_message(self.Selected(self._index))

    def refresh_state(self, state: StrategyState, selected: bool) -> None:
        self._state = state
        if selected:
            self.add_class("selected")
        else:
            self.remove_class("selected")
        self._update_labels()

    def _update_labels(self) -> None:
        s = self._state
        m = s.meta

        pnl_color = "green" if s.total_pnl >= 0 else "red"
        wr = s.win_rate
        wr_color = "green" if wr and wr >= 0.5 else ("yellow" if wr is not None else "dim")
        exp_color = "yellow" if s.current_exposure_pct > m.max_total_exposure_pct * 80 else "white"

        title = (
            f"[bold {m.color}]{m.name}[/bold {m.color}]"
            f"  [bold yellow]PAPER[/bold yellow]"
            f"  [dim]│[/dim]  [{pnl_color}]PnL: {_pnl(s.total_pnl)} ({_pct(s.pnl_pct)})[/{pnl_color}]"
            f"  [bold white]NAV: ${s.nav:.2f}[/bold white]"
        )

        lines: list[str] = [
            f"[dim]{'─'*60}[/dim]",
            # Position stats
            f"Open: [bold]{len(s.positions)}[/bold]"
            f"  Entries: {len(s.opens)}"
            f"  Exits: {len(s.closes)}"
            f"  Wins: [green]{len(s.wins)}[/green]"
            f"  Losses: [red]{len(s.losses)}[/red]",
            # Performance
            f"Win Rate: [{wr_color}]{_pct(wr)}[/{wr_color}]"
            f"  Avg PnL/Trade: {_pnl(s.avg_pnl_per_trade)}"
            f"  Best: [green]{_pnl(s.best_trade_pnl)}[/green]"
            f"  Worst: [red]{_pnl(s.worst_trade_pnl)}[/red]",
            # Model quality
            f"Avg Edge: [bold]{_v(s.avg_edge, '+.3f')}[/bold]"
            f"  Avg σ: {_v(s.avg_sigma, '.3f')}"
            f"  Avg {m.model_label}: {_v(s.avg_model_prob, '.3f')}"
            f"  Avg Entry P: {_v(s.avg_entry_price, '.3f')}",
        ]

        if m.has_bs_mc:
            lines.append(
                f"Avg BS Prob: {_v(s.avg_bs_prob, '.3f')}"
                f"  Jump Premium: [bold]{_v(s.jump_premium, '+.4f')}[/bold]"
                f"  Avg Score: {_v(s.avg_score, '.4f')}"
                f"  Regime: [bold]{s.last_regime}[/bold]"
            )

        lines += [
            # Risk / exposure
            f"Exposure: [{exp_color}]${s.current_exposure_usd:.2f} ({_pct(s.current_exposure_pct)})[/{exp_color}]"
            f"  Cap: {m.max_total_exposure_pct*100:.0f}%"
            f"  Per-Market Cap: {m.max_per_market_pct*100:.0f}%",
            # Timing
            f"Avg Hold: {_v(s.avg_hold_hours, '.1f', suffix='h')}"
            f"  Vol Traded: ${s.total_volume:.2f}"
            f"  Last Event: {s.last_event_ts}",
        ]

        if s.updated_at:
            try:
                ts = datetime.fromisoformat(s.updated_at).strftime("%H:%M:%S UTC")
                lines.append(f"[dim]State updated: {ts}[/dim]")
            except Exception:
                pass

        self.query_one(f"#ct-{m.key}", Label).update(title)
        self.query_one(f"#cb-{m.key}", Label).update("\n".join(lines))


# ── Bottom pane helpers ───────────────────────────────────────────────────────


def _populate_positions(table: DataTable, state: StrategyState) -> None:
    table.clear(columns=True)
    m = state.meta
    table.add_columns(
        "Question", "Asset", "Type", "Strike", "Expiry",
        "Entry P", m.model_label, "Edge@Entry", "Size $",
        "Exposure%", "Time Left", "Fill",
    )
    now = datetime.now(UTC)
    for p in state.positions:
        edge = p.get("edge_at_entry", 0.0)
        size = p.get("size_usd", 0.0)
        exp_pct = f"{size / state.nav * 100:.1f}%" if state.nav > 0 else "—"
        fill = "✓" if p.get("fill_confirmed") else "pending"
        table.add_row(
            p.get("question", "")[:48],
            p.get("asset", "").upper(),
            p.get("option_type", ""),
            f"${p.get('strike', 0):,.0f}",
            p.get("expiry_iso", "")[:10],
            f"{p.get('entry_price', 0):.3f}",
            f"{p.get('model_prob_at_entry', 0):.3f}",
            f"{edge:+.3f}",
            f"${size:.2f}",
            exp_pct,
            _time_left(p.get("expiry_iso", "")),
            fill,
        )
    if not state.positions:
        table.add_row("(no open positions)", *[""] * 11)


def _populate_history(table: DataTable, state: StrategyState) -> None:
    table.clear(columns=True)
    m = state.meta

    if m.has_bs_mc:
        table.add_columns(
            "Time (UTC)", "Event", "Asset", "Type", "Strike",
            "BS Prob", "MC Prob", "Mkt P", "Edge", "Score",
            "Sigma", "Size $", "PnL", "Reason",
        )
    else:
        table.add_columns(
            "Time (UTC)", "Event", "Asset", "Type", "Strike",
            m.model_label, "Mkt P", "Edge", "Sigma",
            "Size $", "PnL", "Reason",
        )

    recent = list(reversed(state.history[-40:]))
    for e in recent:
        ev = e.get("event", "?")
        try:
            ts_str = datetime.fromisoformat(e["ts"]).strftime("%m-%d %H:%M:%S")
        except Exception:
            ts_str = e.get("ts", "")[:16]

        edge = e.get("edge", 0.0)
        pnl = e.get("pnl")
        pnl_str = _pnl(pnl) if pnl is not None else "—"
        size_str = f"${e.get('size_usd', 0):.2f}" if ev == "open" else "—"
        sigma_str = f"{e.get('sigma', 0):.4f}" if "sigma" in e else "—"

        if m.has_bs_mc:
            table.add_row(
                ts_str,
                ev.upper(),
                e.get("asset", "").upper(),
                e.get("option_type", ""),
                f"${e.get('strike', 0):,.0f}",
                f"{e['bs_prob']:.3f}" if "bs_prob" in e else "—",
                f"{e.get('mc_prob', e.get('model_prob', 0)):.3f}",
                f"{e.get('yes_price', e.get('entry_price', 0)):.3f}",
                f"{edge:+.3f}",
                f"{e['score']:.4f}" if "score" in e else "—",
                sigma_str,
                size_str,
                pnl_str,
                e.get("reason", ""),
            )
        else:
            table.add_row(
                ts_str,
                ev.upper(),
                e.get("asset", "").upper(),
                e.get("option_type", ""),
                f"${e.get('strike', 0):,.0f}",
                f"{e.get('model_prob', e.get('mc_prob', 0)):.3f}",
                f"{e.get('yes_price', e.get('entry_price', 0)):.3f}",
                f"{edge:+.3f}",
                sigma_str,
                size_str,
                pnl_str,
                e.get("reason", ""),
            )

    if not state.history:
        ncols = 14 if m.has_bs_mc else 12
        table.add_row("(no events yet)", *[""] * (ncols - 1))


# ── Main app ──────────────────────────────────────────────────────────────────


class QuantDash(App[None]):
    """Unified TUI monitor for all TurtleQuant family strategies."""

    TITLE = "QuantDash"
    CSS = """
    Screen {
        background: $background;
    }
    #top-pane {
        height: 22;
        border-bottom: solid $primary-darken-2;
    }
    #bottom-pane {
        height: 1fr;
    }
    #section-pos {
        background: $primary-darken-3;
        color: $text;
        padding: 0 2;
        height: 1;
        text-style: bold;
    }
    #section-hist {
        background: $primary-darken-3;
        color: $text;
        padding: 0 2;
        height: 1;
        text-style: bold;
    }
    #section-log {
        background: $primary-darken-3;
        color: $text;
        padding: 0 2;
        height: 1;
        text-style: bold;
    }
    #pos-scroll {
        height: 9;
        border-bottom: solid $primary-darken-2;
    }
    #hist-scroll {
        height: 1fr;
        border-bottom: solid $primary-darken-2;
    }
    #log-scroll {
        height: 8;
    }
    #pos-table {
        height: auto;
    }
    #hist-table {
        height: auto;
    }
    #log-content {
        padding: 0 2;
        color: $text-muted;
    }
    """

    BINDINGS = [
        Binding("q", "quit", "Quit"),
        Binding("left,h", "prev_strategy", "← Strategy", show=True),
        Binding("right,l", "next_strategy", "→ Strategy", show=True),
        Binding("tab", "next_strategy", "Next", show=False),
        Binding("r", "manual_refresh", "Refresh"),
    ]

    selected_index: reactive[int] = reactive(0)

    def __init__(self, registry: list[StrategyMeta], refresh_secs: int = 15, **kwargs: Any) -> None:
        super().__init__(**kwargs)
        self._refresh_secs = refresh_secs
        self._states = [StrategyState(meta=m) for m in registry]
        for s in self._states:
            s.refresh()

    def compose(self) -> ComposeResult:
        yield Header()
        with Horizontal(id="top-pane"):
            for i, state in enumerate(self._states):
                yield StrategyCard(state, index=i, id=f"card-{state.meta.key}")
        with Vertical(id="bottom-pane"):
            yield Label("▶ OPEN POSITIONS", id="section-pos")
            with ScrollableContainer(id="pos-scroll"):
                yield DataTable(id="pos-table", zebra_stripes=True, cursor_type="row")
            yield Label("▶ TRADE HISTORY  (last 40)", id="section-hist")
            with ScrollableContainer(id="hist-scroll"):
                yield DataTable(id="hist-table", zebra_stripes=True, cursor_type="row")
            yield Label("▶ BOT LOG", id="section-log")
            with ScrollableContainer(id="log-scroll"):
                yield Static("", id="log-content")
        yield Footer()

    def on_mount(self) -> None:
        self._refresh_bottom()
        self._refresh_cards()
        self.set_interval(self._refresh_secs, self._auto_refresh)

    # ── Event handlers ────────────────────────────────────────────────────────

    def on_strategy_card_selected(self, event: StrategyCard.Selected) -> None:
        self.selected_index = event.index

    def action_next_strategy(self) -> None:
        self.selected_index = (self.selected_index + 1) % len(self._states)

    def action_prev_strategy(self) -> None:
        self.selected_index = (self.selected_index - 1) % len(self._states)

    def action_manual_refresh(self) -> None:
        self._auto_refresh()

    # ── Reactive watcher ──────────────────────────────────────────────────────

    def watch_selected_index(self, _old: int, _new: int) -> None:
        self._refresh_cards()
        self._refresh_bottom()

    # ── Refresh helpers ───────────────────────────────────────────────────────

    def _auto_refresh(self) -> None:
        for s in self._states:
            s.refresh()
        self._refresh_cards()
        self._refresh_bottom()

    def _refresh_cards(self) -> None:
        for i, state in enumerate(self._states):
            card = self.query_one(f"#card-{state.meta.key}", StrategyCard)
            card.refresh_state(state, selected=(i == self.selected_index))

    def _refresh_bottom(self) -> None:
        state = self._states[self.selected_index]
        m = state.meta

        # Section labels
        self.query_one("#section-pos", Label).update(
            f"▶ OPEN POSITIONS ({len(state.positions)})  [{m.color}]{m.name}[/{m.color}]"
        )
        self.query_one("#section-hist", Label).update(
            f"▶ TRADE HISTORY  [{m.color}]{m.name}[/{m.color}]"
            f"  Opens: {len(state.opens)}  Closes: {len(state.closes)}"
            f"  Total PnL: {_pnl(state.total_pnl)}"
        )

        # Tables
        _populate_positions(self.query_one("#pos-table", DataTable), state)
        _populate_history(self.query_one("#hist-table", DataTable), state)

        # Log
        log_text = "\n".join(state.log_lines[-16:]) if state.log_lines else "(no log yet)"
        self.query_one("#log-content", Static).update(log_text)

        # Update subtitle with last refresh time
        now = datetime.now(UTC).strftime("%H:%M:%S UTC")
        self.sub_title = f"Auto-refresh: {self._refresh_secs}s  │  Last: {now}"


# ── Entry point ───────────────────────────────────────────────────────────────


def main() -> None:
    parser = argparse.ArgumentParser(description="QuantDash — unified TurtleQuant strategy monitor")
    parser.add_argument(
        "--refresh", type=int, default=15, metavar="SEC",
        help="Auto-refresh interval in seconds (default: 15)",
    )
    parser.add_argument(
        "--state-root", type=Path,
        default=Path(os.getenv("STATE_ROOT", "/opt/turtlequant/state")),
        help="Root state directory (default: /opt/turtlequant/state)",
    )
    args = parser.parse_args()

    registry = build_registry(args.state_root)
    QuantDash(registry=registry, refresh_secs=args.refresh).run()


if __name__ == "__main__":
    main()
