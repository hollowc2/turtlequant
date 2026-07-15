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


def _legacy_events(path: Path) -> list[dict[str, Any]]:
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
    return [*_legacy_events(state_dir / HISTORY_JSON), *_journal_events(state_dir / HISTORY_JSONL)]
