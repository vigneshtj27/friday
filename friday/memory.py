"""Long-term memory: durable facts the owner states, decisions, and outcomes of past tasks.

SQLite like everything else. Facts are written only from the owner's own words (the memory tool
requires an `owner_quote` the registry checks against the owner's message) or by friday's own
code (task outcomes). Text from web pages, mail or sheets can't plant a memory. Per-site browsing
know-how stays in site notes (friday/sitenotes.py); this is about the owner and their tasks.
"""

from __future__ import annotations

import re
from typing import Any

from friday.db import Database
from friday.util import iso
from friday.vault.masking import Redactor

KINDS = ("fact", "decision", "outcome")
_STOP = {"the", "a", "an", "my", "is", "are", "of", "to", "and", "in", "on", "for", "what", "do", "you", "about", "remember", "me", "i"}


def _words(text: str) -> set[str]:
    return {w for w in re.findall(r"[a-z0-9]+", text.lower()) if w not in _STOP and len(w) > 1}


class Memory:
    def __init__(self, db: Database, redactor: Redactor):
        self._db = db
        self._redactor = redactor

    def remember(self, text: str, *, kind: str = "fact", subject: str | None = None, source: str = "owner") -> dict[str, Any]:
        if kind not in KINDS:
            raise ValueError(f"kind must be one of {KINDS}")
        text = self._redactor.redact(" ".join(text.split()))[:1000]
        if not text:
            raise ValueError("nothing to remember")
        same = self._db.query_one("SELECT id FROM memory WHERE text = ? AND forgotten_at IS NULL", (text,))
        if same:
            return self.get(same["id"])  # type: ignore[return-value]
        # A newer fact about the same subject replaces the older one (e.g. a different expense sheet).
        if subject and kind == "fact":
            self._db.execute("UPDATE memory SET forgotten_at = ? WHERE subject = ? AND kind = 'fact' AND forgotten_at IS NULL",
                             (iso(), subject.lower()))  # fmt: skip
        with self._db.tx() as c:
            cur = c.execute("INSERT INTO memory (kind, subject, text, source, created_at) VALUES (?, ?, ?, ?, ?)",
                            (kind, subject.lower() if subject else None, text, source, iso()))  # fmt: skip
            mid = cur.lastrowid
        return self.get(mid)  # type: ignore[arg-type,return-value]

    def get(self, mid: int) -> dict[str, Any] | None:
        r = self._db.query_one("SELECT * FROM memory WHERE id = ?", (mid,))
        return dict(r) if r else None

    def all(self, *, kinds: tuple[str, ...] = KINDS, limit: int = 200) -> list[dict[str, Any]]:
        marks = ",".join("?" * len(kinds))
        rows = self._db.query(f"SELECT * FROM memory WHERE forgotten_at IS NULL AND kind IN ({marks}) ORDER BY id DESC LIMIT ?",
                              (*kinds, limit))  # fmt: skip
        return [dict(r) for r in rows]

    def search(self, query: str, limit: int = 10) -> list[dict[str, Any]]:
        q = _words(query)
        scored = []
        for m in self.all(limit=1000):
            w = _words(m["text"]) | _words(m["subject"] or "")
            score = len(q & w)
            if score or not q:
                scored.append((score, m["id"], m))
        scored.sort(key=lambda x: (-x[0], -x[1]))
        return [m for _, _, m in scored[:limit]]

    def forget(self, query: str) -> list[dict[str, Any]]:
        """Forget memories matching `query` (an id, or words). Returns what was forgotten."""
        if query.strip().isdigit():
            hits = [m for m in [self.get(int(query))] if m and not m["forgotten_at"]]
        else:
            q = _words(query)
            hits = [m for m in self.all(limit=1000) if q and q <= (_words(m["text"]) | _words(m["subject"] or ""))]
        for m in hits:
            self._db.execute("UPDATE memory SET forgotten_at = ? WHERE id = ?", (iso(), m["id"]))
        return hits

    def context(self, request: str, *, facts: int = 25, outcomes: int = 5) -> str:
        """Compact lines for the planner: all recent facts/decisions, plus outcomes relevant to the request."""
        lines = [f"- [{m['kind']} #{m['id']}] {m['text']}" for m in self.all(kinds=("fact", "decision"), limit=facts)]
        rel = [m for m in self.search(request, limit=outcomes * 3) if m["kind"] == "outcome"][:outcomes]
        lines += [f"- [outcome #{m['id']}, {m['created_at'][:10]}] {m['text']}" for m in rel]
        return "\n".join(lines) or "(nothing remembered yet)"
