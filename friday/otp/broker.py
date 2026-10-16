"""Obtain one-time codes: TOTP locally, email via Gmail, otherwise ask the owner.

Codes are never stored or logged: they are registered with the redactor, handed
to the caller once, and the caller places them in the vault as a single-use
ephemeral secret.
"""

from __future__ import annotations

import asyncio
import re
import secrets
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any

from friday.audit import AuditLog
from friday.db import Database
from friday.notify import Notifier
from friday.otp.extract import extract_otp
from friday.otp.gmail import GmailClient
from friday.otp.totp import totp
from friday.profiles.models import Profile, Site
from friday.util import iso, utcnow
from friday.vault.masking import Redactor
from friday.vault.store import SecretNotFound, SecretStore

CODE_RE = re.compile(r"^[A-Za-z0-9]{3,10}$")


@dataclass
class OtpRequest:
    id: str
    profile_id: str
    run_id: str | None
    channel: str
    prompt: str
    created_at: datetime
    expires_at: datetime
    status: str = "pending"  # pending | resolved | expired | cancelled
    _future: asyncio.Future[str] = field(default=None, repr=False)  # type: ignore[assignment]

    def public(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "profile_id": self.profile_id,
            "run_id": self.run_id,
            "channel": self.channel,
            "prompt": self.prompt,
            "status": self.status,
            "created_at": iso(self.created_at),
            "expires_at": iso(self.expires_at),
            "seconds_left": max(0, int((self.expires_at - utcnow()).total_seconds())),
        }


@dataclass
class OtpResult:
    code: str | None
    via: str
    detail: str


class OtpBroker:
    def __init__(
        self,
        db: Database,
        audit: AuditLog,
        vault: SecretStore,
        redactor: Redactor,
        notifier: Notifier | None,
        gmail: GmailClient | None,
        *,
        owner_timeout: int = 300,
        email_timeout: int = 90,
        portal_url: str = "",
    ):
        self._db = db
        self._audit = audit
        self._vault = vault
        self._redactor = redactor
        self.notifier = notifier
        self.gmail = gmail
        self.owner_timeout = owner_timeout
        self.email_timeout = email_timeout
        self.portal_url = portal_url.rstrip("/")
        self._pending: dict[str, OtpRequest] = {}

    # ---- channels -------------------------------------------------------

    async def obtain(
        self, *, profile: Profile, site: Site, channel: str, since: datetime, run_id: str | None = None
    ) -> OtpResult:
        if channel == "totp":
            try:
                seed = self._vault.reveal(f"{profile.secret_prefix}totp-seed", purpose="totp", actor="system")
            except SecretNotFound:
                return await self.ask_owner(profile, site, "totp", run_id, note="No TOTP seed is stored for this account.")
            code, remaining = totp(seed)
            if remaining < 5:  # about to roll over; use the next window's code
                await asyncio.sleep(remaining + 1)
                code, _ = totp(seed)
            self._register(code)
            self._audit.record("otp.obtained", target=profile.id, channel="totp", via="totp", run=run_id)
            return OtpResult(code, "totp", "generated locally")
        if channel == "email" and self.gmail and self.gmail.configured:
            code = await self.gmail.wait_for_code(
                senders=site.otp_senders, since=since, timeout=self.email_timeout, fallback_terms=[site.name]
            )
            if code:
                self._register(code)
                self._audit.record("otp.obtained", target=profile.id, channel="email", via="gmail", run=run_id)
                return OtpResult(code, "gmail", "read from Gmail")
            return await self.ask_owner(profile, site, "email", run_id, note="No code arrived in Gmail in time.")
        return await self.ask_owner(profile, site, channel, run_id)

    async def ask_owner(
        self, profile: Profile, site: Site, channel: str, run_id: str | None, *, note: str = ""
    ) -> OtpResult:
        what = {"sms": "SMS code", "email": "emailed code", "totp": "authenticator code"}.get(channel, "one-time code")
        prompt = f"Enter the {what} for {site.name} ({profile.account})."
        req = self._create(profile.id, run_id, channel, prompt)
        if self.notifier:
            url = f"{self.portal_url}/#otp" if self.portal_url else None
            await self.notifier.send(f"{prompt} {note} Reply with just the code within {self.owner_timeout // 60} min.".strip(), url)
        try:
            code = await asyncio.wait_for(req._future, timeout=self.owner_timeout)
        except TimeoutError:
            self._close(req, "expired")
            return OtpResult(None, "owner", "no code received before it expired")
        return OtpResult(code, "owner", "entered by owner")

    # ---- owner requests ---------------------------------------------------

    def _create(self, profile_id: str, run_id: str | None, channel: str, prompt: str) -> OtpRequest:
        now = utcnow()
        req = OtpRequest(
            secrets.token_urlsafe(9), profile_id, run_id, channel, prompt, now, now + timedelta(seconds=self.owner_timeout),
            _future=asyncio.get_running_loop().create_future(),
        )  # fmt: skip
        self._pending[req.id] = req
        self._db.execute(
            "INSERT INTO otp_requests (id, run_id, profile_id, channel, prompt, status, created_at, expires_at)"
            " VALUES (?, ?, ?, ?, ?, 'pending', ?, ?)",
            (req.id, run_id, profile_id, channel, prompt, iso(now), iso(req.expires_at)),
        )
        self._audit.record("otp.requested", target=profile_id, channel=channel, request=req.id, run=run_id)
        return req

    def _close(self, req: OtpRequest, status: str, via: str | None = None) -> None:
        req.status = status
        self._pending.pop(req.id, None)
        self._db.execute(
            "UPDATE otp_requests SET status = ?, resolved_at = ?, resolved_via = ? WHERE id = ?",
            (status, iso(), via, req.id),
        )
        self._audit.record(f"otp.{status}", target=req.profile_id, request=req.id, via=via)

    def _register(self, code: str) -> None:
        self._redactor.register("otp/code", code)

    def pending(self) -> list[OtpRequest]:
        now = utcnow()
        for r in list(self._pending.values()):
            if r.expires_at <= now and not r._future.done():
                r._future.cancel()
                self._close(r, "expired")
        return list(self._pending.values())

    def submit(self, request_id: str | None, code: str, *, via: str) -> bool:
        code = code.strip().replace(" ", "")
        if not CODE_RE.match(code):
            return False
        pending = self.pending()
        req = self._pending.get(request_id) if request_id else (pending[-1] if pending else None)
        if req is None or req._future.done():
            return False
        self._register(code)
        req.status = "resolved"
        self._close(req, "resolved", via)
        req._future.set_result(code)
        return True

    async def handle_reply(self, text: str, via: str) -> bool:
        """Free-text reply from a chat channel or SMS forwarder: resolve the newest pending request."""
        if not self.pending():
            return False
        stripped = text.strip()
        code = stripped if CODE_RE.match(stripped) and any(c.isdigit() for c in stripped) else extract_otp(text)
        return bool(code) and self.submit(None, code, via=via)

    def cancel_for_run(self, run_id: str) -> None:
        for r in list(self._pending.values()):
            if r.run_id == run_id and not r._future.done():
                r._future.cancel()
                self._close(r, "cancelled")
