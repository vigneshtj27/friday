"""A local mock bank for tests and first-run demos. Each scenario lives under /s/<scenario>/.

Scenarios: normal, email_otp, sms_otp, totp, push, bounce, denied, captcha, hardened, xhr.
Test credentials: username "alice", password "correct-horse-battery". TOTP seed MOCK_TOTP_SEED.
`/mock/outbox` shows the codes the "bank" sent (for tests standing in for Gmail / SMS).
"""

from __future__ import annotations

import asyncio
import html
import random
import secrets
import time
from datetime import date, timedelta
from decimal import Decimal

from fastapi import FastAPI, Form, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse, Response

from friday.otp.totp import totp

USERNAME = "alice"
PASSWORD = "correct-horse-battery"
MOCK_TOTP_SEED = "JBSWY3DPEHPK3PXP"
SCENARIOS = {"normal", "email_otp", "sms_otp", "totp", "push", "bounce", "denied", "captcha", "hardened", "xhr"}
# "xhr": the form is sent by script and the button spins for XHR_LOGIN_DELAY seconds before the
# page redirects, with no navigation in between (American Express signs in this way).
XHR_LOGIN_DELAY = 4.0
# "hardened": every page disables eval/Function the way some banks (e.g. American Express) do.
HARDEN_JS = ("<script>(()=>{const no=()=>{throw new Error('eval is disabled')};"
             "window.eval=no;window.Function=function(){no()};})()</script>")


def _transactions() -> list[dict]:
    rng = random.Random(7)
    bal = Decimal("50000.00")
    out = []
    d = date(2025, 1, 1)
    payees = ["GROCER MART", "CITY POWER", "SALARY ACME CORP", "COFFEE HOUSE", "UBER TRIP", "RENT", "MOBILE BILL", "BOOKSTORE"]
    while d <= date(2026, 12, 31):
        for _ in range(rng.randint(0, 2)):
            payee = rng.choice(payees)
            credit = payee.startswith("SALARY")
            amt = Decimal(rng.randint(150, 90000 if credit else 6000)) + Decimal(rng.randint(0, 99)) / 100
            bal = bal + amt if credit else bal - amt
            out.append({"date": d, "desc": payee, "amount": amt, "dir": "credit" if credit else "debit", "balance": bal})
        d += timedelta(days=rng.randint(1, 3))
    return out


TXNS = _transactions()


def _pdf(lines: list[str]) -> bytes:
    """Minimal single-font PDF, enough for text extraction."""
    def esc(s: str) -> str:
        return s.replace("\\", "\\\\").replace("(", "\\(").replace(")", "\\)")

    pages, chunk = [], 60
    for i in range(0, max(len(lines), 1), chunk):
        body = "BT /F1 9 Tf 11 TL 36 806 Td " + " ".join(f"({esc(line)}) Tj T*" for line in lines[i : i + chunk]) + " ET"
        pages.append(body.encode("latin-1", "replace"))
    objs: list[bytes] = []
    n_pages = len(pages)
    page_ids = [4 + 2 * i for i in range(n_pages)]
    objs.append(b"<< /Type /Catalog /Pages 2 0 R >>")
    objs.append(f"<< /Type /Pages /Kids [{' '.join(f'{p} 0 R' for p in page_ids)}] /Count {n_pages} >>".encode())
    objs.append(b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>")
    for i, content in enumerate(pages):
        objs.append(f"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 595 842] /Resources << /Font << /F1 3 0 R >> >> /Contents {page_ids[i] + 1} 0 R >>".encode())
        objs.append(f"<< /Length {len(content)} >>\nstream\n".encode() + content + b"\nendstream")
    out = bytearray(b"%PDF-1.4\n")
    offsets = []
    for i, o in enumerate(objs, start=1):
        offsets.append(len(out))
        out += f"{i} 0 obj\n".encode() + o + b"\nendobj\n"
    xref = len(out)
    out += f"xref\n0 {len(objs) + 1}\n0000000000 65535 f \n".encode()
    out += b"".join(f"{off:010d} 00000 n \n".encode() for off in offsets)
    out += f"trailer\n<< /Size {len(objs) + 1} /Root 1 0 R >>\nstartxref\n{xref}\n%%EOF\n".encode()
    return bytes(out)


def create_mock_app() -> FastAPI:
    app = FastAPI(docs_url=None, redoc_url=None, openapi_url=None)
    sessions: dict[str, dict] = {}  # token -> {scenario, stage, code, created}
    outbox: list[dict] = []
    transfers: list[dict] = []

    @app.middleware("http")
    async def harden(request: Request, call_next):
        resp = await call_next(request)
        if request.url.path.startswith("/s/hardened/") and resp.headers.get("content-type", "").startswith("text/html"):
            body = b"".join([c async for c in resp.body_iterator]).replace(b"<head>", b"<head>" + HARDEN_JS.encode(), 1)
            headers = {k: v for k, v in resp.headers.items() if k.lower() != "content-length"}
            return Response(body, status_code=resp.status_code, headers=headers)
        return resp

    def page(title: str, body: str, status: int = 200) -> HTMLResponse:
        return HTMLResponse(
            f"<!doctype html><html><head><title>{html.escape(title)}</title><meta charset=utf-8></head>"
            f"<body style='font-family:sans-serif;max-width:640px;margin:40px auto'><h1>MockBank</h1>{body}</body></html>",
            status_code=status,
        )

    def sess(request: Request, sc: str) -> dict | None:
        s = sessions.get(request.cookies.get(f"mb_{sc}", ""))
        return s if s and s["scenario"] == sc else None

    def login_form(sc: str, error: str = "") -> HTMLResponse:
        captcha = ""
        if sc == "captcha":
            captcha = "<div class='g-recaptcha' data-sitekey='x'>[ ] I'm not a robot</div><p>Please verify you are human.</p>"
        err = f"<p role=alert style='color:red'>{html.escape(error)}</p>" if error else ""
        script = ""
        if sc == "xhr":
            script = f"""<script>document.querySelector('form').addEventListener('submit', async e => {{
  e.preventDefault();
  const b = e.target.querySelector('button'); b.disabled = true; b.setAttribute('aria-busy', 'true'); b.textContent = '...';
  const r = await fetch('/s/{sc}/api/login', {{method: 'POST', body: new FormData(e.target)}});
  const j = await r.json();
  location.href = j.redirect;
}});</script>"""
        return page(
            "Sign in - MockBank",
            f"""<h2>Sign in to online banking</h2>{err}
<form method=post action=/s/{sc}/login>
<label for=u>Customer ID</label><br><input id=u name=username autocomplete=username><br>
<label for=p>Password</label><br><input id=p name=password type=password autocomplete=current-password><br>
{captcha}<button type=submit>Sign in</button></form>{script}""",
        )

    @app.get("/")
    async def root():
        return page("MockBank", "".join(f"<p><a href=/s/{s}/login>{s}</a></p>" for s in sorted(SCENARIOS)))

    @app.get("/mock/outbox")
    async def get_outbox():
        return outbox[-5:]

    @app.get("/mock/transfers")
    async def get_transfers():
        return transfers

    @app.get("/s/{sc}/login")
    async def login_get(request: Request, sc: str):
        if sc == "denied":
            return page("Access Denied", "<h2>Access Denied</h2><p>We detected unusual activity from your network. Reference ID: 18.af2c.9d31</p>", 403)
        s = sess(request, sc)
        if s and s["stage"] == "in":
            return RedirectResponse(f"/s/{sc}/home", status_code=303)
        return login_form(sc)

    @app.post("/s/{sc}/login")
    async def login_post(sc: str, username: str = Form(""), password: str = Form("")):
        if sc == "denied":
            return page("Access Denied", "<h2>Access Denied</h2>", 403)
        if sc == "captcha":
            return login_form(sc, "Please complete the captcha.")
        if sc == "bounce":
            return RedirectResponse(f"/s/{sc}/login", status_code=303)
        if username != USERNAME or password != PASSWORD:
            return login_form(sc, "Incorrect Customer ID or password.")
        token = secrets.token_hex(8)
        stage = {"email_otp": "otp", "sms_otp": "otp", "totp": "otp", "push": "push"}.get(sc, "in")
        s = {"scenario": sc, "stage": stage, "created": time.time(), "code": None}
        if sc in ("email_otp", "sms_otp"):
            s["code"] = f"{random.randint(0, 999999):06d}"
            outbox.append(
                {
                    "channel": "email" if sc == "email_otp" else "sms",
                    "sender": "alerts@mockbank.test",
                    "body": f"Dear customer, {s['code']} is your MockBank OTP for login. It is valid for 5 minutes. Do not share it. A/c XX4821.",
                    "at": time.time(),
                }
            )
        sessions[token] = s
        target = {"otp": "otp", "push": "push"}.get(stage, "home")
        r = RedirectResponse(f"/s/{sc}/{target}", status_code=303)
        r.set_cookie(f"mb_{sc}", token, max_age=30 * 86400, httponly=True, path=f"/s/{sc}")
        return r

    @app.post("/s/{sc}/api/login")
    async def login_api(sc: str, username: str = Form(""), password: str = Form("")):
        await asyncio.sleep(XHR_LOGIN_DELAY)
        r = await login_post(sc, username, password)
        if isinstance(r, RedirectResponse):  # tell the page's script where to go; keep the session cookie
            out = JSONResponse({"redirect": r.headers["location"]})
            if "set-cookie" in r.headers:
                out.headers["set-cookie"] = r.headers["set-cookie"]
            return out
        return JSONResponse({"redirect": f"/s/{sc}/login"})

    @app.get("/s/{sc}/otp")
    async def otp_get(request: Request, sc: str, error: str = ""):
        s = sess(request, sc)
        if not s or s["stage"] != "otp":
            return RedirectResponse(f"/s/{sc}/login", status_code=303)
        where = {
            "email_otp": "We have sent a one-time password to your registered email a***e@example.com.",
            "sms_otp": "We have sent a one-time password by SMS to your mobile number ending 42.",
            "totp": "Enter the 6-digit code from your authenticator app.",
        }[sc]
        err = f"<p role=alert>{html.escape(error)}</p>" if error else ""
        return page(
            "Verify - MockBank",
            f"""<h2>Verify it's you</h2><p>{where}</p>{err}
<form method=post action=/s/{sc}/otp><label for=c>One-time code</label><br>
<input id=c name=code inputmode=numeric autocomplete=one-time-code maxlength=6><br>
<button type=submit>Verify</button></form>""",
        )

    @app.post("/s/{sc}/otp")
    async def otp_post(request: Request, sc: str, code: str = Form("")):
        s = sess(request, sc)
        if not s or s["stage"] != "otp":
            return RedirectResponse(f"/s/{sc}/login", status_code=303)
        expected = totp(MOCK_TOTP_SEED)[0] if sc == "totp" else s["code"]
        if code.strip() != expected:
            return RedirectResponse(f"/s/{sc}/otp?error=That+code+is+not+valid", status_code=303)
        s["stage"] = "in"
        return RedirectResponse(f"/s/{sc}/home", status_code=303)

    @app.get("/s/{sc}/push")
    async def push_get(request: Request, sc: str):
        s = sess(request, sc)
        if not s:
            return RedirectResponse(f"/s/{sc}/login", status_code=303)
        if time.time() - s["created"] > 4:
            s["stage"] = "in"
        if s["stage"] == "in":
            return RedirectResponse(f"/s/{sc}/home", status_code=303)
        return page(
            "Approve sign-in",
            "<h2>Check your phone</h2><p>We sent a notification to your MockBank app. Approve the sign-in request to continue.</p>"
            "<script>setTimeout(() => location.reload(), 1500)</script>",
        )

    def require_in(request: Request, sc: str):
        s = sess(request, sc)
        return s if s and s["stage"] == "in" else None

    @app.get("/s/{sc}/home")
    async def home(request: Request, sc: str):
        if not require_in(request, sc):
            return RedirectResponse(f"/s/{sc}/login", status_code=303)
        return page(
            "Account summary - MockBank",
            f"""<p>Welcome back, Alice</p><nav><a href=/s/{sc}/statements>Statements</a> |
<a href=/s/{sc}/transfer>Transfer money</a> | <a href=/s/{sc}/logout>Sign out</a></nav>
<h3>Account summary</h3><p>Savings XX4821 - Available balance: {TXNS[-1]['balance']:.2f}</p>""",
        )

    @app.get("/s/{sc}/logout")
    async def logout(request: Request, sc: str):
        sessions.pop(request.cookies.get(f"mb_{sc}", ""), None)
        return RedirectResponse(f"/s/{sc}/login", status_code=303)

    @app.get("/s/{sc}/transfer")
    async def transfer_get(request: Request, sc: str):
        if not require_in(request, sc):
            return RedirectResponse(f"/s/{sc}/login", status_code=303)
        return page(
            "Transfer",
            f"""<h2>Transfer money</h2><form method=post action=/s/{sc}/transfer>
<label for=py>Payee account</label><input id=py name=payee><label for=am>Amount</label><input id=am name=amount>
<button type=submit>Pay now</button></form><p><a href=/s/{sc}/home>Back</a> | <a href=/s/{sc}/logout>Sign out</a></p>""",
        )

    @app.post("/s/{sc}/transfer")
    async def transfer_post(request: Request, sc: str, payee: str = Form(""), amount: str = Form("")):
        if not require_in(request, sc):
            return RedirectResponse(f"/s/{sc}/login", status_code=303)
        transfers.append({"payee": payee, "amount": amount})
        return page("Done", f"<p>Transferred {html.escape(amount)} to {html.escape(payee)}.</p><a href=/s/{sc}/logout>Sign out</a>")

    @app.get("/s/{sc}/statements")
    async def statements(request: Request, sc: str):
        if not require_in(request, sc):
            return RedirectResponse(f"/s/{sc}/login", status_code=303)
        return page(
            "Statements - MockBank",
            f"""<h2>Account statements</h2><p>Statements are available for 2025-2026. You can also get e-statements by email under Settings.</p>
<form method=get action=/s/{sc}/statements/download>
<label for=f>From date</label><input id=f type=date name=from>
<label for=t>To date</label><input id=t type=date name=to>
<label for=fmt>Format</label><select id=fmt name=format><option>PDF</option><option>CSV</option></select>
<button type=submit>Download statement</button></form>
<p><a href=/s/{sc}/home>Back to summary</a> | <a href=/s/{sc}/logout>Sign out</a></p>""",
        )

    @app.get("/s/{sc}/statements/download")
    async def download(request: Request, sc: str, format: str = "PDF"):
        if not require_in(request, sc):
            return RedirectResponse(f"/s/{sc}/login", status_code=303)
        q = request.query_params
        try:
            start, end = date.fromisoformat(q.get("from", "")), date.fromisoformat(q.get("to", ""))
        except ValueError:
            return page("Statements", "<p role=alert>Please choose a valid date range.</p>", 400)
        rows = [t for t in TXNS if start <= t["date"] <= end]
        opening = (rows[0]["balance"] + (rows[0]["amount"] if rows[0]["dir"] == "debit" else -rows[0]["amount"])) if rows else Decimal(0)
        if format.upper() == "CSV":
            lines = ["Txn Date,Narration,Withdrawal Amt,Deposit Amt,Closing Balance"]
            for t in rows:
                w = f"{t['amount']:.2f}" if t["dir"] == "debit" else ""
                dpt = f"{t['amount']:.2f}" if t["dir"] == "credit" else ""
                lines.append(f"{t['date'].strftime('%d/%m/%Y')},{t['desc']},{w},{dpt},{t['balance']:.2f}")
            return Response("\n".join(lines) + "\n", media_type="text/csv",
                            headers={"Content-Disposition": f"attachment; filename=statement_{start}_{end}.csv"})  # fmt: skip
        debits = sum((t["amount"] for t in rows if t["dir"] == "debit"), Decimal(0))
        credits = sum((t["amount"] for t in rows if t["dir"] == "credit"), Decimal(0))
        text = [f"MockBank Statement of Account XX4821   Period {start} to {end}", f"Opening balance: {opening:.2f}", "",
                "Date        Description            Debit        Credit       Balance"]  # fmt: skip
        for t in rows:
            d_ = f"{t['amount']:.2f}" if t["dir"] == "debit" else ""
            c_ = f"{t['amount']:.2f}" if t["dir"] == "credit" else ""
            text.append(f"{t['date'].strftime('%d-%m-%Y')}  {t['desc']:<22} {d_:>10}  {c_:>10}  {t['balance']:>12.2f}")
        text += ["", f"Total debits: {debits:.2f}   Total credits: {credits:.2f}", f"Closing balance: {(rows[-1]['balance'] if rows else opening):.2f}"]
        return Response(_pdf(text), media_type="application/pdf",
                        headers={"Content-Disposition": f"attachment; filename=statement_{start}_{end}.pdf"})  # fmt: skip

    return app


app = create_mock_app()
