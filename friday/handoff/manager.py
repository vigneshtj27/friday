"""Human handoff: park a live browser session until the owner says "Done"."""

from __future__ import annotations

import asyncio
import secrets
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from enum import StrEnum

from friday.audit import AuditLog
from friday.db import Database
from friday.profiles.display import VncTarget
from friday.util import iso, utcnow


class HandoffState(StrEnum):
    PENDING = "pending"
    DONE = "done"
    CANCELLED = "cancelled"
    EXPIRED = "expired"


@dataclass
class Handoff:
    id: str
    profile_id: str
    reason: str
    created_at: datetime
    expires_at: datetime
    vnc: VncTarget | None
    state: HandoffState = HandoffState.PENDING
    _event: asyncio.Event = field(default_factory=asyncio.Event, repr=False)

    @property
    def is_open(self) -> bool:
        return self.state == HandoffState.PENDING and utcnow() < self.expires_at


class HandoffManager:
    def __init__(self, db: Database, audit: AuditLog, *, base_url: str, ttl_seconds: int):
        self._db = db
        self._audit = audit
        self._base_url = base_url.rstrip("/")
        self._ttl = ttl_seconds
        self._live: dict[str, Handoff] = {}

    def create(self, profile_id: str, reason: str, vnc: VncTarget | None, *, ttl_seconds: int | None = None) -> Handoff:
        now = utcnow()
        h = Handoff(
            id=secrets.token_urlsafe(18),
            profile_id=profile_id,
            reason=reason,
            created_at=now,
            expires_at=now + timedelta(seconds=self._ttl if ttl_seconds is None else ttl_seconds),
            vnc=vnc,
        )
        self._live[h.id] = h
        self._db.execute(
            "INSERT INTO handoffs (id, profile_id, reason, state, created_at, expires_at) VALUES (?, ?, ?, ?, ?, ?)",
            (h.id, profile_id, reason, h.state.value, iso(h.created_at), iso(h.expires_at)),
        )
        self._audit.record("handoff.create", target=profile_id, handoff=h.id, reason=reason)
        return h

    def link(self, h: Handoff) -> str:
        return f"{self._base_url}/handoff/{h.id}"

    def get(self, handoff_id: str) -> Handoff | None:
        h = self._live.get(handoff_id)
        if h and h.state == HandoffState.PENDING and utcnow() >= h.expires_at:
            self._finish(h, HandoffState.EXPIRED, actor="system")
        return h

    def list_open(self) -> list[Handoff]:
        return [h for h in list(self._live.values()) if (self.get(h.id) or h).is_open]

    def open_for(self, profile_id: str) -> Handoff | None:
        return next((h for h in self.list_open() if h.profile_id == profile_id), None)

    def extend(self, handoff_id: str, seconds: int | None = None, *, cap_seconds: int = 4 * 3600) -> bool:
        """Keep an open handoff alive (the remote view is connected): expiry moves to now + ttl, capped from creation."""
        h = self.get(handoff_id)
        if h is None or not h.is_open:
            return False
        until = min(utcnow() + timedelta(seconds=self._ttl if seconds is None else seconds), h.created_at + timedelta(seconds=cap_seconds))
        if until > h.expires_at:
            h.expires_at = until
            self._db.execute("UPDATE handoffs SET expires_at = ? WHERE id = ?", (iso(until), h.id))
        return True

    def complete(self, handoff_id: str, *, actor: str = "admin") -> bool:
        h = self.get(handoff_id)
        if h is None or not h.is_open:
            return False
        self._finish(h, HandoffState.DONE, actor=actor)
        return True

    def cancel(self, handoff_id: str, *, actor: str = "admin") -> bool:
        h = self.get(handoff_id)
        if h is None or not h.is_open:
            return False
        self._finish(h, HandoffState.CANCELLED, actor=actor)
        return True

    def _finish(self, h: Handoff, state: HandoffState, *, actor: str) -> None:
        h.state = state
        h.vnc = None
        self._db.execute(
            "UPDATE handoffs SET state = ?, completed_at = ? WHERE id = ?", (state.value, iso(), h.id)
        )
        self._audit.record(f"handoff.{state.value}", actor=actor, target=h.profile_id, handoff=h.id)
        h._event.set()

    async def wait(self, handoff_id: str) -> HandoffState:
        h = self._live[handoff_id]
        while not h._event.is_set():
            remaining = (h.expires_at - utcnow()).total_seconds()  # re-read: extend() may have moved it
            if remaining <= 0:
                self.get(handoff_id)  # marks expired
                break
            try:
                await asyncio.wait_for(h._event.wait(), remaining)
            except TimeoutError:
                pass
        self._live.pop(handoff_id, None)
        return h.state
