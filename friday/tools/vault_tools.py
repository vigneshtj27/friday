"""Secrets as tools: ask for any missing secret with a secure chat card, and manage secret
metadata. Values are never an argument and never a result - only names, kinds and labels are."""

from __future__ import annotations

from typing import Any

from friday.tools.base import Call, Tool, ToolError
from friday.vault_requests import REQUESTS

DATE = {"type": "string", "description": "YYYY-MM-DD"}


class VaultRequest(Tool):
    name = "vault_request"
    description = ("Post a secure chat card to collect a missing secret straight into the vault (never to you). Use this "
                   "whenever friday needs a credential it doesn't have - never ask the owner to type a secret as chat text. "
                   f"request_id is one of {sorted(REQUESTS)}; the fields and vault names are fixed in code. Pass account for "
                   "site_credentials, and optionally then to continue a job once the owner submits.")
    schema = {"type": "object", "properties": {
        "request_id": {"type": "string", "enum": sorted(REQUESTS)},
        "account": {"type": "string", "description": "profile id, for request_id=site_credentials"},
        "purpose": {"type": "string"},
        "then": {"type": "object", "description": "job to start once the card is submitted",
                 "properties": {"kind": {"type": "string", "enum": ["fetch_statement", "fetch_activity", "task", "login"]},
                                "profile_id": {"type": "string"}, "start": DATE, "end": DATE, "goal": {"type": "string"},
                                "account_type": {"type": "string", "enum": ["card", "bank"]}},
                 "required": ["kind", "profile_id"]},
    }, "required": ["request_id"]}  # fmt: skip
    effect = "write"

    async def precheck(self, args: dict[str, Any], call: Call) -> str | None:
        return None  # posting a card writes nothing until the owner submits it; no approval needed to ask

    async def run(self, args: dict[str, Any], call: Call) -> Any:
        from friday.vault_requests import VaultRequestError

        then = args.get("then")
        resume = None
        if then:
            resume = {"kind": then["kind"], "profile_id": then["profile_id"],
                      "params": {k: then[k] for k in ("start", "end", "goal", "account_type") if k in then}}  # fmt: skip
        try:
            rid = await call.ctx.vault_requests.request(args["request_id"], account=args.get("account"),
                                                        purpose=args.get("purpose"), resume=resume)  # fmt: skip
        except VaultRequestError as e:
            raise ToolError(str(e)) from e
        return {"vault_card": rid, "note": "The card is in the chat. The owner types the secret into it; it goes straight "
                "to the vault, never to you."}


class SecretsList(Tool):
    name = "secrets_list"
    description = "List saved secret names, kinds and when each was last used. Values are never returned."
    schema = {"type": "object", "properties": {"prefix": {"type": "string", "description": "only names under this prefix"}}}

    async def run(self, args: dict[str, Any], call: Call) -> Any:
        metas = call.ctx.vault.list(args.get("prefix"))
        return {"secrets": [{"name": m.name, "kind": m.kind.value, "last_used_at": m.last_used_at,
                             "updated_at": m.updated_at} for m in metas]}  # fmt: skip


class SecretsDelete(Tool):
    name = "secrets_delete"
    description = "Delete one saved secret by name. Always asks for your approval first. Values are never shown."
    schema = {"type": "object", "properties": {"name": {"type": "string"}}, "required": ["name"]}
    effect = "write"

    async def precheck(self, args: dict[str, Any], call: Call) -> str | None:
        if not call.ctx.vault.exists(args["name"]):
            raise ToolError(f"no secret called {args['name']!r}")
        return f"Delete the saved secret '{args['name']}' (this cannot be undone)"

    async def run(self, args: dict[str, Any], call: Call) -> Any:
        return {"deleted": call.ctx.vault.delete(args["name"], actor="assistant"), "name": args["name"]}


class SecretsUpdate(Tool):
    name = "secrets_update"
    description = ("Replace an existing secret's value: posts a secure card for that exact name (values are never shown, so "
                   "updating means re-entering it). The name must already exist.")
    schema = {"type": "object", "properties": {"name": {"type": "string"}}, "required": ["name"]}
    effect = "write"

    async def precheck(self, args: dict[str, Any], call: Call) -> str | None:
        return None

    async def run(self, args: dict[str, Any], call: Call) -> Any:
        from friday.vault_requests import VaultRequestError

        try:
            row_id = await call.ctx.vault_requests.request_existing(args["name"])
        except VaultRequestError as e:
            raise ToolError(str(e)) from e
        return {"vault_card": row_id, "note": f"A card to re-enter '{args['name']}' is in the chat; the value goes straight to the vault."}


TOOLS: list[Tool] = [VaultRequest(), SecretsList(), SecretsDelete(), SecretsUpdate()]
