"""Crash-safe local journal for broker actions.

Lifecycle: ``pending`` (journaled before sending) -> ``submitted`` (the broker
answered, possibly ambiguously) -> one terminal state:

* ``reconciled`` — the confirmed fill is reflected in local position state.
* ``failed`` — the order never reached the broker (pre-send rejection), or an
  operator confirmed it did not fill.
* ``cancelled`` — an operator confirmed the broker cancelled it unfilled.

Anything non-terminal is outstanding and blocks entries until resolved.
"""

from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass
from pathlib import Path

TERMINAL_STATUSES = ("reconciled", "failed", "cancelled")

_SCHEMA = """CREATE TABLE IF NOT EXISTS order_intent (
    id INTEGER PRIMARY KEY,
    market_id TEXT NOT NULL,
    token_id TEXT NOT NULL,
    side TEXT NOT NULL CHECK(side IN ('BUY', 'SELL')),
    requested REAL NOT NULL CHECK(requested > 0),
    status TEXT NOT NULL CHECK(status IN ('pending', 'submitted', 'reconciled', 'failed', 'cancelled')),
    order_id TEXT NOT NULL DEFAULT '',
    response TEXT NOT NULL DEFAULT '',
    metadata TEXT NOT NULL DEFAULT '{}',
    resolution TEXT NOT NULL DEFAULT ''
)"""


@dataclass(frozen=True)
class OrderIntent:
    id: int
    market_id: str
    token_id: str
    side: str
    requested: float
    status: str
    order_id: str = ""
    metadata: dict[str, object] | None = None


class OrderIntentLedger:
    def __init__(self, path: Path) -> None:
        self.db = sqlite3.connect(path)
        self._migrate()

    def _migrate(self) -> None:
        row = self.db.execute("SELECT sql FROM sqlite_master WHERE type='table' AND name='order_intent'").fetchone()
        if row is None:
            self.db.execute(_SCHEMA)
            return
        columns = {r[1] for r in self.db.execute("PRAGMA table_info(order_intent)")}
        if "'failed'" in row[0] and "resolution" in columns:
            return
        # SQLite cannot alter a CHECK constraint: rebuild the table, keeping rows.
        copied = [c for c in ("id", "market_id", "token_id", "side", "requested", "status",
                              "order_id", "response", "metadata") if c in columns]
        names = ", ".join(copied)
        with self.db:
            self.db.execute("ALTER TABLE order_intent RENAME TO order_intent_old")
            self.db.execute(_SCHEMA)
            self.db.execute(f"INSERT INTO order_intent ({names}) SELECT {names} FROM order_intent_old")
            self.db.execute("DROP TABLE order_intent_old")

    def pending(
        self, market_id: str, token_id: str, side: str, requested: float, metadata: dict[str, object] | None = None
    ) -> int:
        with self.db:
            return int(
                self.db.execute(
                    "INSERT INTO order_intent (market_id, token_id, side, requested, status, metadata) VALUES (?, ?, ?, ?, 'pending', ?)",
                    (market_id, token_id, side, requested, json.dumps(metadata or {}, separators=(",", ":"))),
                ).lastrowid
            )

    def submitted(self, intent_id: int, order_id: str, response: dict) -> None:
        with self.db:
            self.db.execute(
                "UPDATE order_intent SET status='submitted', order_id=?, response=? WHERE id=?",
                (order_id, json.dumps(response, separators=(",", ":")), intent_id),
            )

    def reconcile(self, intent_id: int) -> None:
        with self.db:
            self.db.execute("UPDATE order_intent SET status='reconciled' WHERE id=?", (intent_id,))

    def fail(self, intent_id: int, reason: str) -> None:
        """Close an intent whose order never reached the broker."""
        self.resolve(intent_id, "failed", reason)

    def resolve(self, intent_id: int, status: str, note: str) -> None:
        """Move an outstanding intent to a terminal status (in-process or by an operator)."""
        if status not in TERMINAL_STATUSES:
            raise ValueError(f"status must be one of {TERMINAL_STATUSES}")
        with self.db:
            updated = self.db.execute(
                "UPDATE order_intent SET status=?, resolution=? WHERE id=? AND status IN ('pending', 'submitted')",
                (status, note, intent_id),
            ).rowcount
        if updated != 1:
            raise ValueError(f"intent {intent_id} is not outstanding")

    def outstanding(self, market_id: str | None = None) -> list[OrderIntent]:
        query = (
            "SELECT id, market_id, token_id, side, requested, status, order_id, metadata FROM order_intent "
            "WHERE status IN ('pending', 'submitted')"
        )
        params: tuple[object, ...] = ()
        if market_id is not None:
            query += " AND market_id = ?"
            params = (market_id,)
        rows = self.db.execute(query + " ORDER BY id", params)
        return [OrderIntent(*row[:7], json.loads(row[7])) for row in rows]
