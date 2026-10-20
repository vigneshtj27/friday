"""Portal JSON API (stage 6) and inbound webhooks (SMS forwarder, WhatsApp)."""

from __future__ import annotations

import csv
import hashlib
import hmac
import io
from typing import Any

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse, PlainTextResponse, Response
from pydantic import BaseModel

from friday.context import AppContext
from friday.preflight import otp_channel
from friday.version import build_info
from friday.jobs import Backoff
from friday.profiles.manager import ProfileNotFound
from friday.profiles.models import ProfileConfig


class RunIn(BaseModel):
    kind: str
    params: dict[str, Any] = {}
    force: bool = False


class DecisionIn(BaseModel):
    approve: bool


class CodeIn(BaseModel):
    code: str


class ProbesIn(BaseModel):
    names: list[str] | None = None


def register_api(app: FastAPI, ctx: AppContext, require: Any) -> None:
    deps = [require]

    @app.get("/api/state", dependencies=deps)
    async def state():
        active = set(ctx.jobs.active())
        profiles = []
        for p in ctx.profiles.list():
            last = ctx.runs.list(profile_id=p.id, limit=1)
            profiles.append({
                "id": p.id, "site_id": p.site_id, "account": p.account, "status": p.status,
                "last_used_at": p.last_used_at, "last_run": last[0] if last else None,
                "running": any(r["profile_id"] == p.id for r in ctx.runs.list(limit=50) if r["id"] in active),
                "secrets": [m.name.rsplit("/", 1)[-1] for m in ctx.vault.list(p.secret_prefix)],
                "proxy": p.config.proxy.server if p.config.proxy else None, "timezone": p.config.timezone, "locale": p.config.locale,
            })  # fmt: skip
        return {
            "profiles": profiles,
            "approvals": [a.public() for a in ctx.gate.pending()],
            "otp": [r.public() for r in ctx.broker.pending()],
            "handoffs": [
                {"id": h.id, "profile_id": h.profile_id, "reason": h.reason, "url": ctx.handoffs.link(h), "expires_at": h.expires_at.isoformat()}
                for h in ctx.handoffs.list_open()
            ],
            "active_runs": list(active),
            "gmail_connected": ctx.gmail.configured,
            "sheets_connected": ctx.sheets.configured,
            "notify": [n.name for n in ctx.notifier.notifiers],
            "model": ctx.settings.model,
            "version": build_info(),
            "preflight": {p.id: ctx.prefs.get(f"preflight:{p.id}") for p in ctx.profiles.list()},
            "egress": {p.id: ctx.prefs.get(f"egress:{p.id}") for p in ctx.profiles.list()},
            "otp_channels": {s.id: otp_channel(ctx, s) for s in ctx.sites.list()},
        }

    # ---- profiles & runs ----------------------------------------------------

    @app.patch("/api/profiles/{profile_id}", dependencies=deps)
    async def update_profile(profile_id: str, config: ProfileConfig):
        try:
            ctx.profiles.update_config(profile_id, config)
        except ProfileNotFound as e:
            raise HTTPException(404, "no such profile") from e
        return ctx.profiles.get(profile_id)

    @app.post("/api/profiles/{profile_id}/runs", dependencies=deps)
    async def start_run(profile_id: str, body: RunIn):
        try:
            run_id = ctx.jobs.submit(body.kind, profile_id, body.params, force=body.force)
        except ProfileNotFound as e:
            raise HTTPException(404, "no such profile") from e
        except Backoff as e:
            raise HTTPException(429, str(e)) from e
        except (ValueError, KeyError) as e:
            raise HTTPException(400, str(e)) from e
        return {"run_id": run_id}

    @app.get("/api/runs", dependencies=deps)
    async def list_runs(profile_id: str | None = None, limit: int = 100):
        return ctx.runs.list(profile_id=profile_id, limit=limit)

    @app.get("/api/runs/{run_id}", dependencies=deps)
    async def get_run(run_id: str, after: int = 0):
        r = ctx.runs.get(run_id)
        if r is None:
            raise HTTPException(404, "no such run")
        return {"run": r, "steps": ctx.runs.steps(run_id, after_id=after), "active": run_id in ctx.jobs.active()}

    @app.post("/api/runs/{run_id}/cancel", dependencies=deps)
    async def cancel_run(run_id: str):
        if not ctx.jobs.cancel(run_id):
            raise HTTPException(409, "run is not active")
        return {"cancelled": run_id}

    # ---- files & transactions ---------------------------------------------------

    @app.get("/api/files", dependencies=deps)
    async def list_files(profile_id: str | None = None, kind: str = "download"):
        return [f.__dict__ for f in ctx.files.list(profile_id=profile_id, kind=kind)]

    @app.get("/api/files/{file_id}", dependencies=deps)
    async def get_file(file_id: str, inline: bool = False):
        meta = ctx.files.meta(file_id)
        if meta is None:
            raise HTTPException(404, "no such file")
        if meta.kind != "screenshot" and meta.kind != "approval" and meta.kind != "diagnosis":
            ctx.audit.record("file.read", actor="admin", target=file_id, name=meta.name)
        disp = "inline" if inline or meta.mime.startswith("image/") else "attachment"
        safe = meta.name.replace('"', "")
        return Response(ctx.files.read(file_id), media_type=meta.mime, headers={"Content-Disposition": f'{disp}; filename="{safe}"'})

    @app.get("/api/transactions", dependencies=deps)
    async def transactions(source: str | None = None, start: str | None = None, end: str | None = None, format: str = "json"):
        rows = ctx.transactions.list(source=source, start=start, end=end, limit=20000)
        if format == "csv":
            buf = io.StringIO()
            w = csv.writer(buf)
            cols = ["date", "description", "amount", "currency", "direction", "balance", "source", "raw_ref", "hash"]
            w.writerow(cols)
            for r in rows:
                w.writerow([r[c] for c in cols])
            return PlainTextResponse(buf.getvalue(), media_type="text/csv", headers={"Content-Disposition": 'attachment; filename="transactions.csv"'})
        return rows

    # ---- approvals, OTP, handoffs ------------------------------------------------

    @app.get("/api/approvals", dependencies=deps)
    async def approvals():
        return {"pending": [a.public() for a in ctx.gate.pending()], "history": ctx.gate.history(50)}

    @app.post("/api/approvals/{approval_id}", dependencies=deps)
    async def decide(approval_id: str, body: DecisionIn):
        if not ctx.gate.decide(approval_id, body.approve, actor="admin"):
            raise HTTPException(404, "approval not pending")
        return {"ok": True}

    @app.get("/api/otp", dependencies=deps)
    async def otp_pending():
        return [r.public() for r in ctx.broker.pending()]

    @app.post("/api/otp/{request_id}", dependencies=deps)
    async def otp_submit(request_id: str, body: CodeIn):
        if not ctx.broker.submit(request_id, body.code, via="portal"):
            raise HTTPException(400, "request not pending, or code format invalid")
        return {"ok": True}

    # ---- diagnosis ----------------------------------------------------------------

    @app.get("/api/diagnosis", dependencies=deps)
    async def diagnosis_list(profile_id: str | None = None):
        return [
            {k: r.get(k) for k in ("id", "profile_id", "run_id", "trigger_state", "created_at", "next_experiment")}
            | {"top_cause": (r.get("causes") or [{}])[0].get("cause")}
            for r in ctx.diagnoser.list(profile_id)
        ]

    @app.get("/api/diagnosis/{report_id}", dependencies=deps)
    async def diagnosis_get(report_id: str):
        r = ctx.diagnoser.get(report_id)
        if r is None:
            raise HTTPException(404, "no such report")
        return r

    @app.post("/api/diagnosis/{report_id}/probes", dependencies=deps)
    async def diagnosis_probes(report_id: str, body: ProbesIn):
        r = ctx.diagnoser.get(report_id)
        if r is None:
            raise HTTPException(404, "no such report")
        return {"run_id": ctx.jobs.submit("diagnose_probes", r["profile_id"], {"report_id": report_id, "names": body.names})}

    @app.post("/api/diagnosis/{report_id}/manual", dependencies=deps)
    async def diagnosis_manual(report_id: str):
        r = ctx.diagnoser.get(report_id)
        if r is None:
            raise HTTPException(404, "no such report")
        return {"run_id": ctx.jobs.submit("diagnose_manual", r["profile_id"], {"report_id": report_id})}

    # ---- sites: notes & backoff ------------------------------------------------------

    @app.get("/api/sites/{site_id}/notes", dependencies=deps)
    async def site_notes(site_id: str):
        return {"notes": ctx.sitenotes.as_dicts(site_id), "backoff": ctx.jobs.backoff_state(site_id)}

    @app.delete("/api/sites/{site_id}/notes/{note_id}", dependencies=deps)
    async def delete_note(site_id: str, note_id: str):
        if not ctx.sitenotes.delete(site_id, note_id):
            raise HTTPException(404, "no such note")
        return {"ok": True}

    @app.post("/api/sites/{site_id}/backoff/clear", dependencies=deps)
    async def clear_backoff(site_id: str):
        ctx.jobs.clear_backoff(site_id)
        return {"ok": True}

    # ---- inbound webhooks (no cookie; shared-secret or signature auth) ----------------

    @app.post("/hooks/sms")
    async def sms_hook(request: Request):
        """Android SMS-forwarder: POST JSON/form with the SMS text. Auth: X-Friday-Token header."""
        expected = ctx.settings.sms_webhook_token
        token = request.headers.get("x-friday-token", "")
        if expected is None or not hmac.compare_digest(token, expected.get_secret_value()):
            raise HTTPException(401, "bad token")
        try:
            data = await request.json()
        except Exception:
            data = dict(await request.form())
        text = str(data.get("text") or data.get("message") or data.get("body") or data.get("msg") or "")
        handled = await ctx.broker.handle_reply(text, "sms-forwarder")
        ctx.audit.record("hook.sms", target="sms", handled=handled)  # the text itself is not logged
        return {"handled": handled}

    @app.get("/hooks/whatsapp")
    async def whatsapp_verify(request: Request):
        q = request.query_params
        vt = ctx.settings.whatsapp_verify_token
        if vt and q.get("hub.mode") == "subscribe" and hmac.compare_digest(q.get("hub.verify_token", ""), vt.get_secret_value()):
            return PlainTextResponse(q.get("hub.challenge", ""))
        raise HTTPException(403, "verification failed")

    @app.post("/hooks/whatsapp")
    async def whatsapp_inbound(request: Request):
        secret = ctx.settings.whatsapp_app_secret
        body = await request.body()
        sig = request.headers.get("x-hub-signature-256", "")
        if secret is None or not hmac.compare_digest(sig, "sha256=" + hmac.new(secret.get_secret_value().encode(), body, hashlib.sha256).hexdigest()):
            raise HTTPException(401, "bad signature")
        data = await request.json()
        handled = False
        for entry in data.get("entry", []):
            for change in entry.get("changes", []):
                for msg in (change.get("value") or {}).get("messages", []):
                    if msg.get("from") == (ctx.settings.whatsapp_to or "").lstrip("+") and msg.get("type") == "text":
                        handled = await ctx.broker.handle_reply(msg["text"]["body"], "whatsapp") or handled
        return JSONResponse({"handled": handled})
