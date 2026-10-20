"""Small owner preferences that outlive a conversation (e.g. which sheet tab expenses go to)."""

from __future__ import annotations

import json
from typing import Any

from friday.db import Database
from friday.util import iso


class Prefs:
    def __init__(self, db: Database):
        self._db = db

    def get(self, key: str, default: Any = None) -> Any:
        row = self._db.query_one("SELECT value_json FROM prefs WHERE key = ?", (key,))
        return json.loads(row["value_json"]) if row else default

    def set(self, key: str, value: Any) -> None:
        self._db.execute(
            "INSERT INTO prefs (key, value_json, updated_at) VALUES (?, ?, ?)"
            " ON CONFLICT(key) DO UPDATE SET value_json = excluded.value_json, updated_at = excluded.updated_at",
            (key, json.dumps(value), iso()),
        )
