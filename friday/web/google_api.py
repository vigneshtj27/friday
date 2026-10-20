"""Portal pages for connecting the owner's Google account (Gmail for codes, Sheets for expenses)."""

from __future__ import annotations

import html
import logging
from typing import Any

from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, RedirectResponse

from friday.google_oauth import PURPOSES
from friday.web.auth import COOKIE

log = logging.getLogger(__name__)


def _page(title: str, body: str) -> HTMLResponse:
    return HTMLResponse(
        "<!doctype html><meta charset=utf-8><meta name=viewport content='width=device-width,initial-scale=1'>"
        f"<title>{html.escape(title)}</title><style>body{{font:16px/1.5 system-ui,sans-serif;background:#fff;color:#18181b;"
        "max-width:520px;margin:12vh auto;padding:0 16px}a{color:#2f5bea}.muted{color:#71717a}</style>" + body
    )


def register_google(app: FastAPI, ctx: Any, auth: Any) -> None:
    def redirect_uri() -> str:
        return ctx.settings.public_base_url.rstrip("/") + "/oauth/google/callback"

    @app.get("/oauth/google/start")
    async def google_start(request: Request, purpose: str = "sheets"):
        if not auth.valid(request.cookies.get(COOKIE)):
            return RedirectResponse(f"/login?next=/oauth/google/start%3Fpurpose%3D{purpose}")
        try:
            url = ctx.google.start(purpose, redirect_uri())
        except ValueError as e:
            return _page("friday", f"<h2>Can't connect Google yet</h2><p>{html.escape(str(e))}.</p>"
                         "<p class=muted>See docs/google.md for the one-time OAuth client setup.</p>")
        ctx.audit.record("google.connect_started", actor="admin", purpose=purpose)
        return RedirectResponse(url, status_code=303)

    @app.get("/oauth/google/callback")
    async def google_callback(state: str = "", code: str = "", error: str = ""):
        # No session cookie here (cross-site redirect, SameSite=Strict): the single-use state authorises it.
        if error or not code:
            return _page("friday", f"<h2>Google sign-in was not completed</h2><p class=muted>{html.escape(error or 'no code')}</p>"
                         "<p><a href='/chat'>Back to the chat</a></p>")
        try:
            purpose = await ctx.google.finish(state, code)
        except Exception as e:
            log.warning("google connect failed: %s", type(e).__name__)
            return _page("friday", f"<h2>Couldn't connect Google</h2><p>{html.escape(str(e))[:300]}</p><p><a href='/chat'>Back to the chat</a></p>")
        label = PURPOSES[purpose]["label"]
        ctx.audit.record("google.connected", actor="admin", purpose=purpose)
        await ctx.chat.send(f"{label} is connected.", None)
        if ctx.chat.assistant is not None:
            ctx.chat.assistant.note(f"The owner connected {label}.")
        return _page("friday", f"<h2>{html.escape(label)} is connected</h2><p>The token is stored encrypted in friday's vault.</p>"
                     "<p><a href='/chat'>Back to the chat</a></p>")
