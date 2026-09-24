"""Append-only TurtleQuant history with legacy JSON compatibility."""

from __future__ import annotations

import json
import os
from collections.abc import Iterator
from pathlib import Path
from typing import Any

HISTORY_JSON = "turtlequant-history.json"
HISTORY_JSONL = "turtlequant-history.jsonl"


def append_history(state_dir: Path, entry: dict[str, Any]) -> None:
    """Durably append one event to the JSONL journal."""
    state_dir.mkdir(parents=True, exist_ok=True)
    with (state_dir / HISTORY_JSONL).open("a") as history:
        history.write(json.dumps(entry, separators=(",", ":")) + "\n")
        history.flush()
        os.fsync(history.fileno())


def active_history_path(state_dir: Path) -> Path:
    """Return the current journal when present, otherwise the legacy ledger."""
    journal = state_dir / HISTORY_JSONL
    return journal if journal.exists() else state_dir / HISTORY_JSON


def read_legacy_events(path: Path) -> list[dict[str, Any]]:
    """Parse a legacy JSON-array history file. Returns [] if it doesn't exist."""
    if not path.exists():
        return []
    data = json.loads(path.read_text())
    if not isinstance(data, list) or not all(isinstance(event, dict) for event in data):
        raise ValueError(f"history must be a JSON array of objects: {path}")
    return data


def _journal_events(path: Path) -> Iterator[dict[str, Any]]:
    if not path.exists():
        return
    with path.open() as history:
        for line_number, line in enumerate(history, 1):
            if not line.strip():
                continue
            event = json.loads(line)
            if not isinstance(event, dict):
                raise ValueError(f"history line {line_number} must be an object: {path}")
            yield event


def load_history(state_dir: Path) -> list[dict[str, Any]]:
    """Read legacy events followed by the append-only journal, if either exists."""
    return [
        *read_legacy_events(state_dir / HISTORY_JSON),
        *_journal_events(state_dir / HISTORY_JSONL),
    ]


# Legacy history rows recorded flat closes as zero P&L before fee-adjusted P&L
# was persisted. Those rows predate the current crypto fee schedule, so they are
# normalised with the flat taker rate that applied at the time.
LEGACY_TAKER_FEE_RATE = 0.003


def _float(value: object, default: float = 0.0) -> float:
    try:
        return float(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return default


def effective_close_pnl(open_event: dict[str, Any] | None, close_event: dict[str, Any]) -> float:
    """Realised P&L of a close event, fee-adjusting legacy zero-P&L rows."""
    recorded = _float(close_event.get("pnl"))
    if open_event is None or recorded != 0.0:
        return recorded
    entry_price = _float(open_event.get("yes_price"))
    exit_price = _float(close_event.get("yes_price", close_event.get("exit_price")))
    size_usd = _float(open_event.get("size_usd"))
    if entry_price <= 0 or exit_price < 0 or size_usd <= 0:
        return recorded
    tokens = size_usd / entry_price
    entry_fee = size_usd * LEGACY_TAKER_FEE_RATE
    exit_fee = tokens * exit_price * LEGACY_TAKER_FEE_RATE
    return (exit_price - entry_price) * tokens - entry_fee - exit_fee
