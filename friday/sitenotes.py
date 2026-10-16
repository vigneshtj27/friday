"""Site memory: short notes the agent writes after runs and reads at the start of the next.

Notes are hints for the model, never executable steps. A note is marked stale
when a run contradicts it, and stale notes are shown as such (not hidden).
"""

from __future__ import annotations

import re
import secrets
from dataclasses import dataclass
from typing import Any

from friday.db import Database
from friday.util import iso
from friday.vault.masking import Redactor

KINDS = {"login_url", "login_flow", "otp", "timing", "statement_path", "official_export", "quirk"}
MAX_NOTE_CHARS = 400


@dataclass(frozen=True)
class SiteNote:
    id: str
    site_id: str
    kind: str
    text: str
    stale: bool
    source_run: str | None
    created_at: str
    confirmed_at: str


def _norm(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", " ", text.lower()).strip()


class SiteNotes:
    def __init__(self, db: Database, redactor: Redactor):
        self._db = db
        self._redactor = redactor

    def list(self, site_id: str, *, include_stale: bool = True) -> list[SiteNote]:
        rows = self._db.query("SELECT * FROM site_notes WHERE site_id = ? ORDER BY kind, created_at", (site_id,))
        notes = [SiteNote(**{**dict(r), "stale": bool(r["stale"])}) for r in rows]
        return [n for n in notes if include_stale or not n.stale]

    def add(self, site_id: str, kind: str, text: str, *, run_id: str | None = None) -> SiteNote | None:
        kind = kind if kind in KINDS else "quirk"
        # Notes are data the model reads later: strip secrets and keep them short.
        text = self._redactor.redact(" ".join(text.split()))[:MAX_NOTE_CHARS]
        if not text:
            return None
        for n in self.list(site_id):
            if n.kind == kind and _norm(n.text) == _norm(text):
                self._db.execute(
                    "UPDATE site_notes SET confirmed_at = ?, stale = 0, source_run = ? WHERE id = ?", (iso(), run_id, n.id)
                )
                return n
        now = iso()
        note = SiteNote(secrets.token_hex(4), site_id, kind, text, False, run_id, now, now)
        self._db.execute(
            "INSERT INTO site_notes (id, site_id, kind, text, stale, source_run, created_at, confirmed_at)"
            " VALUES (?, ?, ?, ?, 0, ?, ?, ?)",
            (note.id, site_id, kind, text, run_id, now, now),
        )
        return note

    def mark_stale(self, site_id: str, note_ids: list[str]) -> int:
        n = 0
        for nid in note_ids:
            n += self._db.execute("UPDATE site_notes SET stale = 1 WHERE site_id = ? AND id = ?", (site_id, nid))
        return n

    def set_singleton(self, site_id: str, kind: str, text: str, *, run_id: str | None = None) -> None:
        """For facts with one true value (login URL): a different value makes the old note stale."""
        for n in self.list(site_id, include_stale=False):
            if n.kind == kind and _norm(n.text) != _norm(text):
                self.mark_stale(site_id, [n.id])
        self.add(site_id, kind, text, run_id=run_id)

    def delete(self, site_id: str, note_id: str) -> bool:
        return bool(self._db.execute("DELETE FROM site_notes WHERE site_id = ? AND id = ?", (site_id, note_id)))

    def render(self, site_id: str) -> str:
        notes = self.list(site_id)
        if not notes:
            return "(no notes yet for this site)"
        return "\n".join(
            f"- [{n.id}] ({n.kind}{', STALE - a later run contradicted this' if n.stale else ''}) {n.text}" for n in notes
        )

    def as_dicts(self, site_id: str) -> list[dict[str, Any]]:
        return [n.__dict__ for n in self.list(site_id)]
