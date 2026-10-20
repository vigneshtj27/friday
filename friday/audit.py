"""Append-only, hash-chained audit log. Secrets appear by name only."""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from typing import Any

from friday.db import Database
from friday.vault.masking import Redactor
from friday.util import canonical_json, iso

GENESIS = "0" * 64


@dataclass(frozen=True)
class AuditEntry:
    id: int
    ts: str
    actor: str
    action: str
    target: str | None
    details: dict[str, Any]


def _entry_hash(prev: str, ts: str, actor: str, action: str, target: str | None, details_json: str) -> str:
    payload = canonical_json([prev, ts, actor, action, target, details_json])
    return hashlib.sha256(payload.encode()).hexdigest()


class AuditLog:
    def __init__(self, db: Database, redactor: Redactor):
        self._db = db
        self._redactor = redactor

    def record(self, action: str, *, actor: str = "system", target: str | None = None, **details: Any) -> None:
        with self._db.tx() as c:
            self.record_c(c, action, actor=actor, target=target, **details)

    def record_c(self, c: Any, action: str, *, actor: str = "system", target: str | None = None, **details: Any) -> None:
        """Append an entry using a connection already inside a transaction, so a caller can make
        some other change and the audit of it atomic (e.g. consuming a one-use grant)."""
        details_json = canonical_json(self._redactor.redact_obj(details))
        target = self._redactor.redact(target) if target else target
        ts = iso()
        row = c.execute("SELECT hash FROM audit_log ORDER BY id DESC LIMIT 1").fetchone()
        prev = row[0] if row else GENESIS
        h = _entry_hash(prev, ts, actor, action, target, details_json)
        c.execute(
            "INSERT INTO audit_log (ts, actor, action, target, details_json, prev_hash, hash)"
            " VALUES (?, ?, ?, ?, ?, ?, ?)",
            (ts, actor, action, target, details_json, prev, h),
        )

    def list(self, *, limit: int = 200, action_prefix: str | None = None) -> list[AuditEntry]:
        sql = "SELECT * FROM audit_log"
        params: list[Any] = []
        if action_prefix:
            sql += " WHERE action LIKE ?"
            params.append(action_prefix + "%")
        sql += " ORDER BY id DESC LIMIT ?"
        params.append(limit)
        return [
            AuditEntry(r["id"], r["ts"], r["actor"], r["action"], r["target"], json.loads(r["details_json"]))
            for r in self._db.query(sql, params)
        ]

    def verify_chain(self) -> bool:
        prev = GENESIS
        for r in self._db.query("SELECT * FROM audit_log ORDER BY id"):
            if r["prev_hash"] != prev:
                return False
            expected = _entry_hash(prev, r["ts"], r["actor"], r["action"], r["target"], r["details_json"])
            if r["hash"] != expected:
                return False
            prev = r["hash"]
        return True
