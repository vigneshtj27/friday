"""Stage-1 web surface: login, profile/site/secret API, and the remote-view handoff.

The full admin portal arrives in stage 6; this is the minimum needed to seed
profiles from anywhere through an authenticated remote view.
"""

from __future__ import annotations

import asyncio
import html
import json
import logging
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Annotated

from fastapi import Depends, FastAPI, Form, HTTPException, Request, WebSocket
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse, Response
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from friday.context import AppContext
from friday.handoff.bridge import bridge
from friday.profiles.manager import ProfileBusy, ProfileNotFound
from friday.profiles.models import ProfileConfig, Site
from friday.vault.store import SecretKind
from friday.web.auth import COOKIE, Auth

log = logging.getLogger(__name__)

STATIC_DIR = Path(__file__).parent / "static"
NOVNC_ENTRY = STATIC_DIR / "novnc" / "core" / "rfb.js"

SECURITY_HEADERS = {
    "X-Frame-Options": "DENY",
    "X-Content-Type-Options": "nosniff",
    # not "no-referrer": that makes browsers send "Origin: null" on same-origin form POSTs.
    "Referrer-Policy": "same-origin",
    "Cache-Control": "no-store",
    "Content-Security-Policy": "default-src 'self'; script-src 'self' 'unsafe-inline'; style-src 'self' 'unsafe-inline';"
    " img-src 'self' data:; connect-src 'self'; frame-ancestors 'none'",
}


class SecretIn(BaseModel):
    value: str
    kind: SecretKind = SecretKind.OTHER


class ProfileIn(BaseModel):
    site_id: str
    account: str
    config: ProfileConfig | None = None


def _page(title: str, body: str) -> HTMLResponse:
    return HTMLResponse(
        f"""<!doctype html><html><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{html.escape(title)}</title>
<style>
body{{font-family:system-ui,sans-serif;margin:0;background:#111;color:#eee}}
main{{max-width:560px;margin:10vh auto;padding:0 16px}}
input,button{{font:inherit;padding:8px 12px;border-radius:6px;border:1px solid #444;background:#222;color:#eee}}
button{{cursor:pointer;background:#2d6cdf;border-color:#2d6cdf}}
.bar{{display:flex;gap:12px;align-items:center;padding:8px 12px;background:#1b1b1b;border-bottom:1px solid #333}}
.bar .grow{{flex:1}} #screen{{height:calc(100vh - 52px)}} .muted{{color:#999}}
</style></head><body>{body}</body></html>"""
    )


def create_app(ctx: AppContext) -> FastAPI:
    auth = Auth(ctx.settings, ctx.cipher)
    require = Depends(auth.require)

    def _startup_doctor() -> None:
        """Report identity-pin drift at start; never re-pin automatically (P1-6).

        A stale pin (e.g. after the global driver default changed from playwright to patchright)
        is flagged for the owner, who repairs it deliberately with `profile doctor --repin` or the
        portal. Re-pinning is forgetting the recorded identity, so it stays owner-controlled; it
        never touches cookies, sessions or secrets either way.
        """
        from friday.profiles.doctor import check_all

        try:
            reports = check_all(ctx, repin=False)
        except Exception:
            log.exception("profile doctor failed at startup")
            return
        stale = [r for r in reports if r.needs_repin]
        broken = [r for r in reports if r.issues and not r.needs_repin]
        if stale:
            ctx.chat.store.add("friday", "At startup I found the pinned browser identity of "
                               + ", ".join(f"{r.profile_id} ({'; '.join(r.issues)})" for r in stale)
                               + " no longer matches its settings (the driver default is now patchright). "
                               "Re-pin it when you're ready: Accounts → Check preflight → Re-pin, "
                               "or `profile doctor <id> --repin`. Nothing was changed.")  # fmt: skip
        for r in broken:
            log.warning("profile %s identity mismatch: %s", r.profile_id, "; ".join(r.issues))

    @asynccontextmanager
    async def lifespan(_: FastAPI):
        await ctx.notifier.start(ctx.broker.handle_reply)  # chat replies resolve pending OTP requests
        ctx.scheduler.start()
        _startup_doctor()
        yield
        await ctx.scheduler.stop()
        await ctx.jobs.shutdown()
        await ctx.supervisor.shutdown()
        await ctx.notifier.stop()

    app = FastAPI(title="friday", lifespan=lifespan, docs_url=None, redoc_url=None, openapi_url=None)
    app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")

    @app.middleware("http")
    async def headers(request: Request, call_next):
        response: Response = await call_next(request)
        for k, v in SECURITY_HEADERS.items():
            response.headers.setdefault(k, v)
        return response

    # ---- auth -----------------------------------------------------------

    @app.get("/healthz")
    async def healthz() -> dict:
        from friday.version import build_info

        b = build_info()  # unauthenticated: the short commit and build time only
        return {"ok": True, "version": b["short"], "built_at": b["built_at"]}

    @app.get("/login")
    async def login_form(next: str = "/") -> HTMLResponse:
        return _page(
            "friday - login",
            f"""<main><h1>friday</h1><form method="post" action="/login">
<input type="hidden" name="next" value="{html.escape(next)}">
<p><input type="password" name="password" placeholder="Password" autofocus required></p>
<p><button type="submit">Log in</button></p></form></main>""",
        )

    @app.post("/login")
    async def login(request: Request, password: Annotated[str, Form()], next: Annotated[str, Form()] = "/"):
        if not auth.origin_ok(request.headers.get("origin"), request.headers.get("host")):
            raise HTTPException(403, "cross-origin request refused")
        if not await auth.check_password(password):
            ctx.audit.record("portal.login_failed", actor="anonymous")
            return RedirectResponse("/login?error=1", status_code=303)
        ctx.audit.record("portal.login", actor="admin")
        dest = next if next.startswith("/") and not next.startswith("//") else "/"
        resp = RedirectResponse(dest, status_code=303)
        resp.set_cookie(
            COOKIE, auth.issue(), httponly=True, secure=auth.cookie_secure, samesite="strict",
            max_age=ctx.settings.session_ttl_seconds,
        )  # fmt: skip
        return resp

    @app.post("/logout", dependencies=[require])
    async def logout():
        resp = RedirectResponse("/login", status_code=303)
        resp.delete_cookie(COOKIE)
        return resp

    @app.get("/")
    async def index(request: Request):
        if not auth.valid(request.cookies.get(COOKIE)):
            return RedirectResponse("/login?next=/chat")
        return RedirectResponse("/chat")

    @app.get("/portal")
    async def portal(request: Request):
        if not auth.valid(request.cookies.get(COOKIE)):
            return RedirectResponse("/login?next=/portal")
        return HTMLResponse((STATIC_DIR / "portal.html").read_text())

    @app.get("/stage1")
    async def stage1_index(request: Request):
        if not auth.valid(request.cookies.get(COOKIE)):
            return RedirectResponse("/login")
        rows = "".join(
            f"<li>{html.escape(p.id)} <span class='muted'>({html.escape(p.status)})</span></li>"
            for p in ctx.profiles.list()
        )
        opens = "".join(
            f"<li><a href='/handoff/{h.id}'>{html.escape(h.profile_id)}</a>: {html.escape(h.reason)}</li>"
            for h in ctx.handoffs.list_open()
        )
        return _page(
            "friday",
            f"<main><h1>friday</h1><p class='muted'>Stage 1. The admin portal comes in stage 6.</p>"
            f"<h3>Open handoffs</h3><ul>{opens or '<li class=muted>none</li>'}</ul>"
            f"<h3>Profiles</h3><ul>{rows or '<li class=muted>none</li>'}</ul></main>",
        )

    # ---- sites / profiles / secrets API ----------------------------------

    @app.get("/api/sites", dependencies=[require])
    async def list_sites() -> list[Site]:
        return ctx.sites.list()

    @app.put("/api/sites/{site_id}", dependencies=[require])
    async def put_site(site_id: str, site: Site) -> Site:
        if site.id != site_id:
            raise HTTPException(400, "id mismatch")
        try:
            return ctx.sites.upsert(site)
        except ValueError as e:
            raise HTTPException(400, str(e)) from e

    @app.get("/api/profiles", dependencies=[require])
    async def list_profiles():
        busy = set(ctx.supervisor.running())
        return [{**p.model_dump(exclude={"fingerprint"}), "busy": p.id in busy} for p in ctx.profiles.list()]

    @app.post("/api/profiles", dependencies=[require], status_code=201)
    async def create_profile(body: ProfileIn):
        try:
            return ctx.profiles.create(body.site_id, body.account, body.config)
        except (KeyError, ValueError) as e:
            raise HTTPException(400, str(e)) from e

    @app.get("/api/profiles/{profile_id}", dependencies=[require])
    async def get_profile(profile_id: str):
        try:
            return ctx.profiles.get(profile_id)
        except ProfileNotFound as e:
            raise HTTPException(404, "no such profile") from e

    @app.post("/api/profiles/{profile_id}/seed", dependencies=[require])
    async def seed_profile(profile_id: str):
        try:
            h = await ctx.supervisor.start_seed(profile_id)
        except ProfileNotFound as e:
            raise HTTPException(404, "no such profile") from e
        except ProfileBusy as e:
            raise HTTPException(409, str(e)) from e
        except Exception as e:
            log.exception("seeding %s failed", profile_id)
            raise HTTPException(502, f"could not open the browser: {type(e).__name__}: {e}"[:500]) from e
        return {"handoff_id": h.id, "url": ctx.handoffs.link(h), "remote_view": h.vnc is not None}

    @app.delete("/api/profiles/{profile_id}", dependencies=[require])
    async def revoke_profile(profile_id: str):
        """Revoke everything: wipe the Chrome profile, its downloads and its secrets."""
        try:
            ctx.profiles.wipe(profile_id)
        except ProfileNotFound as e:
            raise HTTPException(404, "no such profile") from e
        except ProfileBusy as e:
            raise HTTPException(409, str(e)) from e
        return {"wiped": profile_id}

    @app.get("/api/secrets", dependencies=[require])
    async def list_secrets():
        return [m.__dict__ for m in ctx.vault.list()]

    @app.put("/api/secrets/{name:path}", dependencies=[require], status_code=204)
    async def put_secret(name: str, body: SecretIn):
        try:
            ctx.vault.put(name, body.value, kind=body.kind)
        except ValueError as e:
            raise HTTPException(400, str(e)) from e

    @app.delete("/api/secrets/{name:path}", dependencies=[require], status_code=204)
    async def delete_secret(name: str):
        if not ctx.vault.delete(name):
            raise HTTPException(404, "no such secret")

    @app.get("/api/audit", dependencies=[require])
    async def audit_log(limit: int = 200, action: str | None = None):
        return {"chain_ok": ctx.audit.verify_chain(), "entries": ctx.audit.list(limit=limit, action_prefix=action)}

    # ---- remote-view handoff --------------------------------------------

    @app.get("/handoff/{handoff_id}")
    async def handoff_page(request: Request, handoff_id: str):
        if not auth.valid(request.cookies.get(COOKIE)):
            return RedirectResponse(f"/login?next=/handoff/{handoff_id}")
        h = ctx.handoffs.get(handoff_id)
        if h is None or not h.is_open:
            return _page("friday", "<main><h1>Handoff closed</h1><p>This handoff is finished or expired.</p></main>")
        bar = f"""<div class="bar"><strong>{html.escape(h.profile_id)}</strong>
<span class="grow muted">{html.escape(h.reason)}</span>
<form method="post" action="/handoff/{h.id}/cancel"><button style="background:#444;border-color:#444">Cancel</button></form>
<form method="post" action="/handoff/{h.id}/done"><button>Done</button></form></div>"""
        if h.vnc is None:
            body = bar + (
                "<main><p>This browser runs on the host's own display (native mode). "
                "Finish the step in that Chrome window, then click Done.</p></main>"
            )
        elif not NOVNC_ENTRY.exists():
            body = bar + "<main><p>noVNC is not installed. Run <code>scripts/fetch_novnc.sh</code>.</p></main>"
        else:
            cfg = json.dumps({"path": f"/handoff/{h.id}/vnc", "password": h.vnc.password})
            body = (
                bar
                + f"""<form id="type-form" class="bar" autocomplete="off"><label class="muted" for="type-text">Type into the browser (click the field in the browser first):</label>
<input id="type-text" type="password" class="grow" spellcheck="false" aria-label="Text to type into the remote browser">
<label class="muted"><input id="type-enter" type="checkbox"> then Enter</label><button>Type</button></form>
<div id="screen"></div>
<script type="module">
import RFB from '/static/novnc/core/rfb.js';
const cfg = {cfg};
const proto = location.protocol === 'https:' ? 'wss' : 'ws';
const rfb = new RFB(document.getElementById('screen'), `${{proto}}://${{location.host}}${{cfg.path}}`,
  {{credentials: {{password: cfg.password}}}});
rfb.scaleViewport = true;
rfb.addEventListener('disconnect', () => document.getElementById('screen').textContent = 'Disconnected.');
// Typing aid: sends each character as a key press (works where the canvas doesn't get the keyboard).
const keysym = (c) => c === '\\n' ? 0xff0d : c === '\\t' ? 0xff09 : c.codePointAt(0) < 0x100 ? c.codePointAt(0) : 0x01000000 + c.codePointAt(0);
document.getElementById('type-form').addEventListener('submit', (e) => {{
  e.preventDefault();
  const box = document.getElementById('type-text');
  for (const c of box.value) rfb.sendKey(keysym(c));
  if (document.getElementById('type-enter').checked) rfb.sendKey(0xff0d, 'Enter');
  box.value = '';
  rfb.focus();
}});
</script>"""
            )
        return _page(f"friday - {h.profile_id}", body.replace('<div class="bar">', '<div class="bar"><a href="/portal" style="color:#9cf">&larr; portal</a>', 1))

    def _finish(handoff_id: str, ok: bool):
        if not ok:
            raise HTTPException(404, "handoff not open")
        return RedirectResponse(f"/handoff/{handoff_id}", status_code=303)

    @app.post("/handoff/{handoff_id}/done", dependencies=[require])
    async def handoff_done(handoff_id: str):
        return _finish(handoff_id, ctx.handoffs.complete(handoff_id))

    @app.post("/handoff/{handoff_id}/cancel", dependencies=[require])
    async def handoff_cancel(handoff_id: str):
        return _finish(handoff_id, ctx.handoffs.cancel(handoff_id))

    @app.websocket("/handoff/{handoff_id}/vnc")
    async def handoff_vnc(ws: WebSocket, handoff_id: str):
        if not auth.websocket_ok(ws):
            await ws.close(code=1008)
            return
        h = ctx.handoffs.get(handoff_id)
        if h is None or not h.is_open or h.vnc is None:
            await ws.close(code=1008)
            return
        offered = ws.headers.get("sec-websocket-protocol", "")
        await ws.accept(subprotocol="binary" if "binary" in offered else None)
        ctx.audit.record("handoff.view", actor="admin", target=h.profile_id, handoff=h.id)

        async def keep_alive() -> None:
            while ctx.handoffs.extend(h.id):  # a human is looking at it: don't expire mid-sign-in
                await asyncio.sleep(60)

        alive = asyncio.create_task(keep_alive())
        try:
            await bridge(ws, h.vnc.host, h.vnc.port)
        finally:
            alive.cancel()

    from friday.web.api import register_api

    register_api(app, ctx, require)

    from friday.web.chat_api import register_chat

    register_chat(app, ctx, require, auth)

    from friday.web.google_api import register_google

    register_google(app, ctx, auth)

    @app.exception_handler(ProfileBusy)
    async def busy_handler(_: Request, exc: ProfileBusy):
        return JSONResponse({"detail": str(exc)}, status_code=409)

    return app
