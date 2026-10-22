"""In-chat vault-request cards: collect ANY missing secret straight into the vault.

Whenever friday needs a credential it does not have - a site login, egress-proxy credentials, an
integration's API key - it posts a secure card in the chat with a small form. The owner types the
secret into that form, which POSTs straight to /api/vault-request/<id>: the value goes to the
encrypted vault and nowhere else. It never passes through the assistant, the chat history or the
logs; the chat records only which field labels were saved.

The field list and each field's vault name are chosen in code, from a FIXED registry of request
types keyed by request_id. The model picks only the request_id (one of the fixed set) and, where
relevant, an existing account; it never names a vault path and the owner never types one. `system/*`
names are a reserved namespace no site profile can reach.

Same guarantees as the account setup card (friday/onboarding.py): password inputs, 30-min TTL,
cancel, hand-parsed submit, values cleared in a finally, no echo in errors. A card may carry a
`resume` job that starts on submit, so the owner never repeats the request.
"""

from __future__ import annotations

import json
import logging
import secrets
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any, Callable

from friday.util import iso, utcnow
from friday.vault.store import SecretKind, validate_name

log = logging.getLogger(__name__)

REQUEST_TTL = timedelta(minutes=30)
MAX_VALUE = 1024
MAX_FIELDS = 8


class VaultRequestError(ValueError):
    pass


@dataclass(frozen=True)
class VaultField:
    label: str
    secret_name: str  # the full vault key, chosen in code
    kind: SecretKind
    input_type: str = "password"  # password | text
    optional: bool = False

    def public(self) -> dict[str, Any]:
        return {"label": self.label, "input": self.input_type, "optional": self.optional,
                "secret_name": self.secret_name}  # fmt: skip


@dataclass(frozen=True)
class RequestBuild:
    title: str
    purpose: str
    fields: list[VaultField]


# ---- the fixed registry ----------------------------------------------------
#
# Each builder returns the card's title, purpose and fields. Secret names are literal here (or
# derived from an existing profile), so neither the model nor owner free text can choose a path.


def _egress_proxy(ctx: Any, account: str | None) -> RequestBuild:
    return RequestBuild(
        "Home egress proxy credentials",
        "friday needs the username and password for your home egress proxy to route a profile's browser through it.",
        [VaultField("Proxy username", "system/proxy/username", SecretKind.PROXY, "text"),
         VaultField("Proxy password", "system/proxy/password", SecretKind.PROXY, "password")],
    )  # fmt: skip


def _site_credentials(ctx: Any, account: str | None) -> RequestBuild:
    if not account:
        raise VaultRequestError("site_credentials needs an account (e.g. amex.me)")
    profile = ctx.profiles.get(account)  # raises if unknown; the path comes from the real profile
    prefix = profile.secret_prefix
    return RequestBuild(
        f"Credentials for {profile.id}",
        f"friday needs the sign-in credentials for {profile.id}.",
        [VaultField("Username / customer ID / email", f"{prefix}username", SecretKind.USERNAME, "text"),
         VaultField("Password", f"{prefix}password", SecretKind.PASSWORD, "password"),
         VaultField("Authenticator secret (optional)", f"{prefix}totp-seed", SecretKind.TOTP_SEED, "password", optional=True)],
    )  # fmt: skip


def _anthropic_api_key(ctx: Any, account: str | None) -> RequestBuild:
    return RequestBuild(
        "Anthropic API key",
        "friday needs an Anthropic API key for the model.",
        [VaultField("API key (sk-ant-...)", "system/integrations/anthropic/api-key", SecretKind.OTHER, "password")],
    )


REQUESTS: dict[str, Callable[[Any, str | None], RequestBuild]] = {
    "egress_proxy": _egress_proxy,
    "site_credentials": _site_credentials,
    "anthropic_api_key": _anthropic_api_key,
}
RESUME_KINDS = {"fetch_statement", "fetch_activity", "task", "login"}


class VaultRequests:
    def __init__(self, ctx: Any):
        self.ctx = ctx

    def _row(self, request_row_id: str) -> dict[str, Any] | None:
        r = self.ctx.db.query_one("SELECT * FROM vault_requests WHERE id = ?", (request_row_id,))
        if r is None:
            return None
        d = dict(r)
        d["fields"] = [VaultField(f["label"], f["secret_name"], SecretKind(f["kind"]), f["input"], f["optional"])
                       for f in json.loads(d.pop("fields_json"))]  # fmt: skip
        d["resume"] = json.loads(d["resume_json"]) if d.get("resume_json") else None
        if d["status"] == "pending" and d["expires_at"] <= iso():
            self._finish(request_row_id, "expired")
            d["status"] = "expired"
        return d

    def get(self, request_row_id: str) -> dict[str, Any] | None:
        return self._row(request_row_id)

    def pending(self) -> list[dict[str, Any]]:
        rows = self.ctx.db.query("SELECT id FROM vault_requests WHERE status = 'pending' ORDER BY created_at")
        return [r for r in (self._row(x["id"]) for x in rows) if r and r["status"] == "pending"]

    def _finish(self, request_row_id: str, status: str) -> None:
        self.ctx.db.execute(
            "UPDATE vault_requests SET status = ?, completed_at = ? WHERE id = ? AND status = 'pending'",
            (status, iso(), request_row_id),
        )
        self.ctx.chat.store.touch()

    def _validate_resume(self, resume: dict[str, Any] | None) -> dict[str, Any] | None:
        if resume is None:
            return None
        kind = resume.get("kind")
        if kind not in RESUME_KINDS:
            raise VaultRequestError(f"unknown follow-up job {kind!r}")
        profile_id = resume.get("profile_id")
        if not profile_id:
            raise VaultRequestError("a resume job needs a profile_id")
        self.ctx.profiles.get(profile_id)  # must exist
        params = dict(resume.get("params") or {})
        if kind in ("fetch_statement", "fetch_activity"):
            from friday.capabilities.activity import FetchActivity
            from friday.capabilities.statement import FetchStatement

            params = (FetchStatement if kind == "fetch_statement" else FetchActivity).validate(params)
        elif kind == "task":
            from friday.capabilities.task import OwnerTask

            params = OwnerTask.validate(params)
        else:
            params = {}
        return {"kind": kind, "profile_id": profile_id, "params": params}

    async def request(self, request_id: str, *, account: str | None = None, purpose: str | None = None,
                      resume: dict[str, Any] | None = None) -> str:  # fmt: skip
        """Post a secure vault-request card. Nothing is saved until the owner submits it."""
        if request_id not in REQUESTS:
            raise VaultRequestError(f"unknown request {request_id!r}; one of {sorted(REQUESTS)}")
        build = REQUESTS[request_id](self.ctx, account)
        for f in build.fields:
            validate_name(f.secret_name)
        resume = self._validate_resume(resume)
        row_id = secrets.token_urlsafe(12)
        now = utcnow()
        self.ctx.db.execute(
            "INSERT INTO vault_requests (id, status, request_id, title, purpose, fields_json, resume_json, created_at, expires_at)"
            " VALUES (?, 'pending', ?, ?, ?, ?, ?, ?, ?)",
            (row_id, request_id, build.title, (purpose or build.purpose)[:300],
             json.dumps([{"label": f.label, "secret_name": f.secret_name, "kind": f.kind.value, "input": f.input_type,
                          "optional": f.optional} for f in build.fields]),
             json.dumps(resume) if resume else None, iso(now), iso(now + REQUEST_TTL)),
        )  # fmt: skip
        self.ctx.audit.record("vault_request.post", actor="assistant", target=request_id, request=row_id,
                              names=[f.secret_name for f in build.fields])  # fmt: skip
        await self.ctx.chat.send(build.title, None, meta={"kind": "vault", "ref": row_id})
        return row_id

    async def request_existing(self, name: str) -> str:
        """Post a card to replace one already-existing secret (used by secrets_update). The name
        comes from the vault, not from the model composing a new path."""
        validate_name(name)
        meta = next((m for m in self.ctx.vault.list() if m.name == name), None)
        if meta is None:
            raise VaultRequestError(f"no secret called {name!r}")
        field = VaultField(f"New value for {name}", name, meta.kind, "password")
        row_id = secrets.token_urlsafe(12)
        now = utcnow()
        self.ctx.db.execute(
            "INSERT INTO vault_requests (id, status, request_id, title, purpose, fields_json, resume_json, created_at, expires_at)"
            " VALUES (?, 'pending', 'update', ?, ?, ?, NULL, ?, ?)",
            (row_id, f"Update {name}", f"Re-enter the value for {name}.",
             json.dumps([{"label": field.label, "secret_name": name, "kind": meta.kind.value, "input": "password", "optional": False}]),
             iso(now), iso(now + REQUEST_TTL)),
        )  # fmt: skip
        self.ctx.audit.record("vault_request.post", actor="assistant", target="update", request=row_id, names=[name])
        await self.ctx.chat.send(f"Update {name}", None, meta={"kind": "vault", "ref": row_id})
        return row_id

    def cancel(self, request_row_id: str) -> bool:
        r = self._row(request_row_id)
        if not r or r["status"] != "pending":
            return False
        self._finish(request_row_id, "cancelled")
        self.ctx.audit.record("vault_request.cancelled", actor="admin", request=request_row_id)
        return True

    async def complete(self, request_row_id: str, values: dict[str, str]) -> dict[str, Any]:
        """Save the typed secrets into the vault by their code-chosen names, then start any resume job.

        `values` is keyed by secret_name and holds secrets: they go to the vault and are not logged,
        echoed or kept anywhere else.
        """
        ctx = self.ctx
        r = self._row(request_row_id)
        if r is None:
            raise VaultRequestError("no such request")
        if r["status"] != "pending":
            raise VaultRequestError(f"this request is {r['status']}")
        fields: list[VaultField] = r["fields"]
        clean: dict[str, str] = {}
        for f in fields:
            v = values.get(f.secret_name) or ""
            if f.input_type == "text":
                v = v.strip()
            if not v:
                if f.optional or ctx.vault.exists(f.secret_name):
                    continue
                raise VaultRequestError(f"{f.label.split(' (')[0]} is required")
            if len(v) > MAX_VALUE:
                raise VaultRequestError(f"{f.label.split(' (')[0]} is too long")
            clean[f.secret_name] = v
        saved_labels = []
        try:
            for f in fields:
                if f.secret_name in clean:
                    ctx.vault.put(f.secret_name, clean[f.secret_name], kind=f.kind, actor="admin")
                    saved_labels.append(f.label.split(" (")[0])
        finally:
            clean.clear()
            values.clear()
        self._finish(request_row_id, "done")
        ctx.audit.record("vault_request.done", actor="admin", request=request_row_id, saved=saved_labels)
        result: dict[str, Any] = {"request": request_row_id, "saved": saved_labels}
        resume = r["resume"]
        if resume:
            run_id = ctx.jobs.submit(resume["kind"], resume["profile_id"], resume.get("params") or {}, force=True)
            result["run_id"] = run_id
            await ctx.chat.send(f"Saved {', '.join(saved_labels)} in the encrypted vault. Now continuing.", None,
                                meta={"kind": "run", "ref": run_id, "profile_id": resume["profile_id"]})  # fmt: skip
        else:
            await ctx.chat.send(f"Saved {', '.join(saved_labels)} in the encrypted vault.")
        if ctx.chat.assistant is not None:
            ctx.chat.assistant.note(f"The owner completed the vault-request card {request_row_id}; saved {', '.join(saved_labels)}.")
        return result

    def card(self, request_row_id: str) -> dict[str, Any] | None:
        r = self._row(request_row_id)
        if r is None:
            return None
        left = max(0, int((datetime.fromisoformat(r["expires_at"]) - utcnow()).total_seconds())) if r["status"] == "pending" else 0
        return {
            "status": r["status"],
            "title": r["title"],
            "purpose": r["purpose"],
            "fields": [{"name": f.secret_name, "label": f.label, "input": f.input_type, "optional": f.optional,
                        "saved": self.ctx.vault.exists(f.secret_name)} for f in r["fields"]],  # fmt: skip
            "seconds_left": left,
        }
