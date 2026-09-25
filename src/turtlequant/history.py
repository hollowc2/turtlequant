"""Append-only TurtleQuant history with legacy JSON compatibility.

Two journals:

* ``turtlequant-history.jsonl`` — trade and ops events (open, close, order,
  failed_order, entry_gate, ...). Small, fsynced, never rotated: it is the
  track record the exporter and performance page are built from.
* ``turtlequant-diagnostics.jsonl`` — per-scan diagnostics (scan_summary,
  signal_evaluation, shadow_quote with book depth). High volume, best-effort
  (no fsync) and size-rotated to ``.1`` … ``.N`` so disk use is bounded.
"""

from __future__ import annotations

import json
import os
from collections.abc import Iterator
from pathlib import Path
from typing import Any

HISTORY_JSON = "turtlequant-history.json"
HISTORY_JSONL = "turtlequant-history.jsonl"
DIAGNOSTICS_JSONL = "turtlequant-diagnostics.jsonl"
DIAGNOSTIC_EVENTS = frozenset({"scan_summary", "signal_evaluation", "shadow_quote"})
DIAGNOSTICS_MAX_BYTES = int(os.getenv("DIAGNOSTICS_MAX_BYTES", str(64 * 1024 * 1024)))
DIAGNOSTICS_BACKUPS = int(os.getenv("DIAGNOSTICS_BACKUP_COUNT", "3"))


def append_history(state_dir: Path, entry: dict[str, Any]) -> None:
    """Append one event: trade events durably, diagnostics to the rotated file."""
    state_dir.mkdir(parents=True, exist_ok=True)
    line = json.dumps(entry, separators=(",", ":")) + "\n"
    if entry.get("event") in DIAGNOSTIC_EVENTS:
        _append_diagnostic(state_dir / DIAGNOSTICS_JSONL, line)
        return
    with (state_dir / HISTORY_JSONL).open("a") as history:
        history.write(line)
        history.flush()
        os.fsync(history.fileno())


def _append_diagnostic(
    path: Path, line: str, max_bytes: int = DIAGNOSTICS_MAX_BYTES, backups: int = DIAGNOSTICS_BACKUPS
) -> None:
    try:
        size = path.stat().st_size
    except FileNotFoundError:
        size = 0
    if size and size + len(line) > max_bytes:
        rotate(path, backups)
    with path.open("a") as diagnostics:
        diagnostics.write(line)


def rotate(path: Path, backups: int) -> None:
    """Shift ``path`` -> ``path.1`` -> … -> ``path.<backups>``; the oldest is dropped."""
    if backups < 1:
        path.unlink(missing_ok=True)
        return
    for index in range(backups - 1, 0, -1):
        older = path.with_name(f"{path.name}.{index}")
        if older.exists():
            os.replace(older, path.with_name(f"{path.name}.{index + 1}"))
    os.replace(path, path.with_name(f"{path.name}.1"))


def diagnostics_paths(state_dir: Path) -> list[Path]:
    """Existing diagnostics files, oldest first, ending with the live file."""
    live = state_dir / DIAGNOSTICS_JSONL
    rotated = sorted(
        (p for p in state_dir.glob(f"{DIAGNOSTICS_JSONL}.*") if p.suffix[1:].isdigit()),
        key=lambda p: int(p.suffix[1:]),
        reverse=True,
    )
    return [*rotated, *([live] if live.exists() else [])]


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
            if not line.endswith("\n"):
                # A crash mid-append can leave an unterminated last line; it was
                # never a complete event. Any other bad line is still an error.
                try:
                    event = json.loads(line)
                except json.JSONDecodeError:
                    return
            else:
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
