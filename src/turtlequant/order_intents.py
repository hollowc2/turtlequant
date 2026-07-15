"""Crash-safe local journal for broker actions."""

from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass
from pathlib import Path


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
        self.db.execute(
            """CREATE TABLE IF NOT EXISTS order_intent (
                id INTEGER PRIMARY KEY,
                market_id TEXT NOT NULL,
                token_id TEXT NOT NULL,
                side TEXT NOT NULL CHECK(side IN ('BUY', 'SELL')),
                requested REAL NOT NULL CHECK(requested > 0),
                status TEXT NOT NULL CHECK(status IN ('pending', 'submitted', 'reconciled')),
                order_id TEXT NOT NULL DEFAULT '',
                response TEXT NOT NULL DEFAULT '',
                metadata TEXT NOT NULL DEFAULT '{}'
            )"""
        )
        columns = {row[1] for row in self.db.execute("PRAGMA table_info(order_intent)")}
        if "metadata" not in columns:
            with self.db:
                self.db.execute("ALTER TABLE order_intent ADD COLUMN metadata TEXT NOT NULL DEFAULT '{}'")

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

    def outstanding(self) -> list[OrderIntent]:
        rows = self.db.execute(
            "SELECT id, market_id, token_id, side, requested, status, order_id, metadata FROM order_intent WHERE status != 'reconciled' ORDER BY id"
        )
        return [OrderIntent(*row[:7], json.loads(row[7])) for row in rows]
