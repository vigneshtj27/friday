"""The chat page and its JSON API.

The page long-polls GET /api/chat: it returns as soon as a message arrives or
any card's state changes (a code resolved, an approval decided elsewhere, a
handoff closed), and otherwise after `wait` seconds.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
from datetime import datetime
from pathlib import Path
from typing import Any

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from pydantic import BaseModel, Field

from friday.context import AppContext
from friday.util import utcnow
from friday.web.auth import COOKIE

STATIC_DIR = Path(__file__).parent / "static"
WINDOW = 200  # messages whose cards are kept current


class ChatIn(BaseModel):
    text: str = Field(min_length=1, max_length=2000)




def _seconds_left(expires_at: str | None) -> int:
    if not expires_at:
        return 0
    return max(0, int((datetime.fromisoformat(expires_at) - utcnow()).total_seconds()))


def register_chat(app: FastAPI, ctx: AppContext, require: Any, auth: Any) -> None:
    store = ctx.chat.store
    deps = [require]

    def typing() -> bool:
        return bool(ctx.chat.assistant and ctx.chat.assistant.busy)

    def signature() -> str:
        live = [
            sorted(r.id for r in ctx.broker.pending()),
            sorted(a.id for a in ctx.gate.pending()),
            sorted(h.id for h in ctx.handoffs.list_open()),
            sorted(r["id"] for r in ctx.onboarding.pending()) if ctx.onboarding else [],
            sorted(r["id"] for r in ctx.vault_requests.pending()) if ctx.vault_requests else [],
            sorted(ctx.jobs.active()) if ctx.jobs else [],
            [tuple(r) for r in ctx.db.query("SELECT id, updated_at FROM plans WHERE status IN ('planning', 'running')")],
            typing(),
        ]
        return hashlib.sha256(json.dumps(live).encode()).hexdigest()[:16]

    def card(kind: str, ref: str) -> dict[str, Any] | None:
        if kind == "otp":
            r = ctx.db.query_one("SELECT channel, prompt, status, expires_at FROM otp_requests WHERE id = ?", (ref,))
            if r is None:
                return None
            live = any(p.id == ref for p in ctx.broker.pending())
            return {"status": "pending" if live else ("expired" if r["status"] == "pending" else r["status"]),
                    "channel": r["channel"], "prompt": r["prompt"], "seconds_left": _seconds_left(r["expires_at"]) if live else 0}  # fmt: skip
        if kind == "approval":
            r = ctx.db.query_one("SELECT action, details_json, status, expires_at FROM approvals WHERE id = ?", (ref,))
            if r is None:
                return None
            live = any(a.id == ref for a in ctx.gate.pending())
            details = json.loads(r["details_json"] or "{}")
            return {"status": "pending" if live else ("expired" if r["status"] == "pending" else r["status"]),
                    "action": r["action"], "url": details.get("url"), "screenshot_file_id": details.get("screenshot_file_id"),
                    "seconds_left": _seconds_left(r["expires_at"]) if live else 0}  # fmt: skip
        if kind == "handoff":
            h = ctx.handoffs.get(ref)
            r = ctx.db.query_one("SELECT reason, state FROM handoffs WHERE id = ?", (ref,))
            if r is None:
                return None
            is_open = bool(h and h.is_open)
            return {"status": "pending" if is_open else ("expired" if r["state"] == "pending" else r["state"]),
                    "reason": r["reason"], "remote_view": bool(h and h.vnc), "link": f"/handoff/{ref}"}  # fmt: skip
        if kind == "setup":
            return ctx.onboarding.card(ref)
        if kind == "vault":
            return ctx.vault_requests.card(ref)
        if kind == "plan":
            r = ctx.db.query_one("SELECT status, state_json FROM plans WHERE id = ?", (ref,))
            if r is None:
                return None
            st = json.loads(r["state_json"])
            return {"status": r["status"], "goal": st.get("goal"), "steps": st.get("steps", []), "events": st.get("events", [])[-5:],
                    "replans": st.get("replans", 0)}  # fmt: skip
        if kind == "run":
            r = ctx.runs.get(ref)
            if r is None:
                return None
            return {"status": r["status"], "kind": r["kind"], "summary": r["summary"], "goal": (r["params"] or {}).get("goal"),
                    "start": (r["params"] or {}).get("start"), "end": (r["params"] or {}).get("end"),
                    "sheet_tab": (((r["params"] or {}).get("then") or {}).get("sheet") or {}).get("tab"), "link": f"/portal#runs/{ref}"}  # fmt: skip
        return None

    def cards_for(messages: list[dict[str, Any]]) -> dict[str, Any]:
        out: dict[str, Any] = {}
        for m in messages:
            if m["kind"] and m["ref"] and m["ref"] not in out:
                c = card(m["kind"], m["ref"])
                if c is not None:
                    out[m["ref"]] = c
        return out

    def status_line() -> str:
        otp, appr, hand = ctx.broker.pending(), ctx.gate.pending(), ctx.handoffs.list_open()
        active = ctx.jobs.active() if ctx.jobs else []
        parts = []
        setups = ctx.onboarding.pending()
        if setups:
            parts.append(f"{len(setups)} setup card{'s' if len(setups) > 1 else ''} ({', '.join(r['profile_id'] for r in setups)})")
        vaults = ctx.vault_requests.pending() if ctx.vault_requests else []
        if vaults:
            parts.append(f"{len(vaults)} secret card{'s' if len(vaults) > 1 else ''} ({', '.join(r['title'] for r in vaults)})")
        if otp:
            parts.append(f"{len(otp)} code request{'s' if len(otp) > 1 else ''} ({', '.join(r.profile_id for r in otp)})")
        if appr:
            parts.append(f"{len(appr)} approval{'s' if len(appr) > 1 else ''} ({', '.join(a.profile_id for a in appr)})")
        if hand:
            parts.append(f"{len(hand)} browser handoff{'s' if len(hand) > 1 else ''} ({', '.join(h.profile_id for h in hand)})")
        waiting = "Waiting on you: " + "; ".join(parts) + "." if parts else "Nothing is waiting on you."
        running = f" {len(active)} run{'s' if len(active) != 1 else ''} in progress." if active else " No runs in progress."
        return waiting + running

    @app.get("/chat")
    async def chat_page(request: Request):
        if not auth.valid(request.cookies.get(COOKIE)):
            return RedirectResponse("/login?next=/chat")
        return HTMLResponse((STATIC_DIR / "chat.html").read_text())

    @app.get("/api/chat", dependencies=deps)
    async def chat_poll(after: int = 0, before: int | None = None, wait: float = 0, sig: str = "", limit: int = 100):
        limit = max(1, min(limit, WINDOW))
        if wait > 0 and before is None:
            deadline = asyncio.get_running_loop().time() + min(wait, 30)
            while store.last_id() <= after and signature() == sig:
                left = deadline - asyncio.get_running_loop().time()
                if left <= 0:
                    break
                await store.wait(min(left, 1.0))  # wakes at once on a new message; card states are re-checked each second
        messages = store.list(after=after, before=before, limit=limit)
        window = messages if before is not None else store.list(limit=WINDOW)
        return {
            "messages": messages,
            "cards": cards_for(window),
            "last_id": store.last_id(),
            "sig": signature(),
            "typing": typing(),
            "has_more": bool(messages) and not after and store.has_before(messages[0]["id"]),
        }

    @app.post("/api/chat", dependencies=deps)
    async def chat_send(body: ChatIn):
        mine = await ctx.chat.owner_says(body.text, status_line)
        return {"id": mine["id"]}

    @app.post("/api/setup/{setup_id}", dependencies=deps)
    async def setup_submit(setup_id: str, request: Request):
        # Parsed by hand: FastAPI's validation errors echo the submitted input, and these are secrets.
        from friday.onboarding import SetupError

        try:
            body = await request.json()
        except Exception:
            raise HTTPException(400, "expected a JSON body") from None
        raw = body.get("values") if isinstance(body, dict) else None
        if not isinstance(raw, dict) or len(raw) > 8 or not all(isinstance(k, str) and isinstance(v, str) for k, v in raw.items()):
            raise HTTPException(400, "values must be an object of text fields")
        values: dict[str, str] = dict(raw)
        raw.clear()
        try:
            return await ctx.onboarding.complete(setup_id, values)
        except SetupError as e:
            raise HTTPException(400, str(e)) from None
        finally:
            values.clear()

    @app.post("/api/setup/{setup_id}/cancel", dependencies=deps)
    async def setup_cancel(setup_id: str):
        if not ctx.onboarding.cancel(setup_id):
            raise HTTPException(404, "setup request is not pending")
        await ctx.chat.send("Cancelled. Nothing was saved.")
        return {"ok": True}

    @app.post("/api/vault-request/{request_id}", dependencies=deps)
    async def vault_request_submit(request_id: str, request: Request):
        # Hand-parsed like setup_submit: FastAPI's validation errors echo the input, and these are secrets.
        from friday.vault_requests import VaultRequestError

        try:
            body = await request.json()
        except Exception:
            raise HTTPException(400, "expected a JSON body") from None
        raw = body.get("values") if isinstance(body, dict) else None
        if not isinstance(raw, dict) or len(raw) > 8 or not all(isinstance(k, str) and isinstance(v, str) for k, v in raw.items()):
            raise HTTPException(400, "values must be an object of text fields")
        values: dict[str, str] = dict(raw)
        raw.clear()
        try:
            return await ctx.vault_requests.complete(request_id, values)
        except VaultRequestError as e:
            raise HTTPException(400, str(e)) from None
        finally:
            values.clear()

    @app.post("/api/vault-request/{request_id}/cancel", dependencies=deps)
    async def vault_request_cancel(request_id: str):
        if not ctx.vault_requests.cancel(request_id):
            raise HTTPException(404, "vault request is not pending")
        await ctx.chat.send("Cancelled. Nothing was saved.")
        return {"ok": True}

    @app.delete("/api/chat", dependencies=deps, status_code=204)
    async def chat_clear():
        store.clear()
        ctx.audit.record("chat.cleared", actor="admin")
