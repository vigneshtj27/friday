"""Per-action owner approval. A pending approval blocks the run until the owner
approves or denies it in the portal, or it expires (which counts as a denial)."""

from __future__ import annotations

import asyncio
import json
import secrets
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from enum import StrEnum
from typing import Any

from friday.audit import AuditLog
from friday.db import Database
from friday.util import iso, utcnow
from friday.vault.masking import Redactor


class ApprovalStatus(StrEnum):
    PENDING = "pending"
    APPROVED = "approved"
    DENIED = "denied"
    EXPIRED = "expired"


@dataclass
class Approval:
    id: str
    run_id: str | None
    profile_id: str
    action: str  # one-line, exactly what will happen
    details: dict[str, Any]
    created_at: datetime
    expires_at: datetime
    status: ApprovalStatus = ApprovalStatus.PENDING
    _event: asyncio.Event = field(default_factory=asyncio.Event, repr=False)

    def public(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "run_id": self.run_id,
            "profile_id": self.profile_id,
            "action": self.action,
            "details": self.details,
            "status": self.status.value,
            "created_at": iso(self.created_at),
            "expires_at": iso(self.expires_at),
        }


Notify = Callable[[str, str | None], Awaitable[None]]


class ApprovalGate:
    def __init__(self, db: Database, audit: AuditLog, redactor: Redactor, *, ttl_seconds: int = 600):
        self._db = db
        self._audit = audit
        self._redactor = redactor
        self._ttl = ttl_seconds
        self._live: dict[str, Approval] = {}
        self.notify: Notify | None = None
        self.link: Callable[[Approval], str] | None = None

    async def request(
        self, *, profile_id: str, run_id: str | None, action: str, details: dict[str, Any], ttl_seconds: int | None = None
    ) -> bool:
        """Ask the owner. Returns True only on an explicit approval."""
        details = self._redactor.redact_obj(details)
        action = self._redactor.redact(action)
        now = utcnow()
        a = Approval(
            secrets.token_urlsafe(12), run_id, profile_id, action, details, now,
            now + timedelta(seconds=self._ttl if ttl_seconds is None else ttl_seconds),
        )  # fmt: skip
        self._live[a.id] = a
        self._db.execute(
            "INSERT INTO approvals (id, run_id, profile_id, action, details_json, status, created_at, expires_at)"
            " VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (a.id, run_id, profile_id, action, json.dumps(details), a.status.value, iso(now), iso(a.expires_at)),
        )
        self._audit.record("approval.request", target=profile_id, approval=a.id, run=run_id, what=action)
        if self.notify:
            try:
                await self.notify(f"Approval needed for {profile_id}: {action}", self.link(a) if self.link else None)
            except Exception:
                pass
        try:
            await asyncio.wait_for(a._event.wait(), max((a.expires_at - utcnow()).total_seconds(), 0))
        except TimeoutError:
            self._decide(a, ApprovalStatus.EXPIRED, actor="system")
        finally:
            self._live.pop(a.id, None)
        return a.status == ApprovalStatus.APPROVED

    def decide(self, approval_id: str, approve: bool, *, actor: str = "admin") -> bool:
        a = self._live.get(approval_id)
        if a is None or a.status != ApprovalStatus.PENDING:
            return False
        self._decide(a, ApprovalStatus.APPROVED if approve else ApprovalStatus.DENIED, actor=actor)
        return True

    def _decide(self, a: Approval, status: ApprovalStatus, *, actor: str) -> None:
        a.status = status
        self._db.execute("UPDATE approvals SET status = ?, decided_at = ? WHERE id = ?", (status.value, iso(), a.id))
        self._audit.record(f"approval.{status.value}", actor=actor, target=a.profile_id, approval=a.id, what=a.action)
        a._event.set()

    def pending(self) -> list[Approval]:
        return [a for a in self._live.values() if a.status == ApprovalStatus.PENDING]

    def history(self, limit: int = 100) -> list[dict[str, Any]]:
        return [dict(r) for r in self._db.query("SELECT * FROM approvals ORDER BY created_at DESC LIMIT ?", (limit,))]
