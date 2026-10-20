from __future__ import annotations

import asyncio
import json
from typing import Annotated, Optional

import typer

from friday.config import ConfigError, get_settings
from friday.vault.masking import setup_logging

# Never render local variables in tracebacks: they can hold secrets.
app = typer.Typer(no_args_is_help=True, add_completion=False, pretty_exceptions_show_locals=False)
secret_app = typer.Typer(no_args_is_help=True, help="Encrypted credentials (values are never printed).")
site_app = typer.Typer(no_args_is_help=True, help="Registered sites and their domain allowlists.")
profile_app = typer.Typer(no_args_is_help=True, help="Persistent Chrome profiles, one per (site, account).")
app.add_typer(secret_app, name="secret")
app.add_typer(site_app, name="site")
app.add_typer(profile_app, name="profile")


def _ctx():
    from friday.context import build_context

    try:
        return build_context()
    except ConfigError as e:
        typer.secho(str(e), fg="red", err=True)
        raise typer.Exit(2) from e


@app.command()
def keygen(keyring: Annotated[bool, typer.Option("--keyring", help="Store in the OS keyring instead of printing")] = False):
    """Generate a new master key."""
    from friday.vault.crypto import generate_key_b64, store_master_key_in_keyring

    key = generate_key_b64()
    if keyring:
        store_master_key_in_keyring(get_settings(), key)
        typer.echo("Master key stored in the OS keyring.")
    else:
        typer.echo(f"FRIDAY_MASTER_KEY={key}")
        typer.secho("Keep this safe: losing it makes every stored secret unrecoverable.", fg="yellow", err=True)


@app.command("hash-password")
def hash_password_cmd():
    """Hash a portal password for FRIDAY_ADMIN_PASSWORD_HASH."""
    from friday.web.auth import hash_password

    pw = typer.prompt("Portal password", hide_input=True, confirmation_prompt=True)
    if len(pw) < 12:
        typer.secho("Use at least 12 characters.", fg="red", err=True)
        raise typer.Exit(1)
    # Single-quoted so the $ signs in the argon2 hash survive dotenv/shell parsing.
    typer.echo(f"FRIDAY_ADMIN_PASSWORD_HASH='{hash_password(pw)}'")


# ---- secrets ---------------------------------------------------------------


@secret_app.command("set")
def secret_set(name: str, kind: str = "other"):
    """Store a secret, e.g. `friday secret set mybank/me/password --kind password`. Value is prompted."""
    from friday.vault.store import SecretKind

    value = typer.prompt(f"Value for {name}", hide_input=True)
    _ctx().vault.put(name, value, kind=SecretKind(kind), actor="cli")
    typer.echo(f"stored {name}")


@secret_app.command("list")
def secret_list(prefix: Optional[str] = None):
    for m in _ctx().vault.list(prefix):
        typer.echo(f"{m.name}\t{m.kind}\tupdated {m.updated_at}\tlast used {m.last_used_at or '-'}")


@secret_app.command("rm")
def secret_rm(name: str):
    typer.echo("deleted" if _ctx().vault.delete(name, actor="cli") else "not found")


# ---- sites -----------------------------------------------------------------


@site_app.command("add")
def site_add(
    site_id: str,
    domain: Annotated[list[str], typer.Option("--domain", help="Registered domain (repeatable)")],
    name: Optional[str] = None,
    login_url: Optional[str] = None,
    otp_domain: Annotated[list[str], typer.Option("--otp-domain", help="Auth/OTP domain (repeatable)")] = [],  # noqa: B006
    otp_sender: Annotated[list[str], typer.Option("--otp-sender", help="Email sender of OTP mails (repeatable)")] = [],  # noqa: B006
):
    from friday.profiles.models import Site

    site = Site(
        id=site_id, name=name or site_id, login_url=login_url, domains=domain, otp_domains=otp_domain, otp_senders=otp_sender
    )
    _ctx().sites.upsert(site, actor="cli")
    typer.echo(f"site {site_id}: {', '.join(site.all_domains)}")


@site_app.command("list")
def site_list():
    for s in _ctx().sites.list():
        typer.echo(f"{s.id}\t{s.login_url or '-'}\t{','.join(s.all_domains)}")


# ---- profiles --------------------------------------------------------------


@profile_app.command("create")
def profile_create(
    site_id: str,
    account: str,
    locale: Optional[str] = None,
    timezone: Optional[str] = None,
    proxy: Annotated[Optional[str], typer.Option(help="scheme://host:port (credentials go in the vault)")] = None,
    driver: Optional[str] = None,
):
    from friday.profiles.models import ProfileConfig, ProxyConfig

    ctx = _ctx()
    s = ctx.settings
    cfg = ProfileConfig(
        locale=locale or s.default_locale,
        timezone=timezone or s.default_timezone,
        window_width=s.screen_width,
        window_height=s.screen_height,
        driver=driver,
        proxy=ProxyConfig(server=proxy) if proxy else None,
    )
    p = ctx.profiles.create(site_id, account, cfg, actor="cli")
    typer.echo(f"created profile {p.id} at {ctx.profiles.user_data_dir(p)}")


@profile_app.command("list")
def profile_list():
    for p in _ctx().profiles.list():
        typer.echo(f"{p.id}\t{p.status}\tlast used {p.last_used_at or '-'}")


@profile_app.command("show")
def profile_show(profile_id: str):
    typer.echo(json.dumps(_ctx().profiles.get(profile_id).model_dump(), indent=2))


@profile_app.command("set-proxy")
def profile_set_proxy(
    profile_id: str,
    server: Annotated[str, typer.Option(help="scheme://host:port, e.g. http://residential.example:8000")],
    username: Annotated[Optional[str], typer.Option(help="proxy username (stored in the vault)")] = None,
    patchright: Annotated[bool, typer.Option("--patchright/--no-patchright", help="also switch this profile to the stealth driver")] = False,
    driver: Annotated[Optional[str], typer.Option(help="set the driver (playwright | patchright | cdp)")] = None,
):
    """Route one profile's browser through a proxy. The password is prompted and stored encrypted."""
    from friday.profiles.models import ProfileConfig, ProxyConfig
    from friday.vault.store import SecretKind

    ctx = _ctx()
    profile = ctx.profiles.get(profile_id)
    user_secret = pass_secret = None
    if username:
        prefix = profile.secret_prefix
        ctx.vault.put(f"{prefix}proxy-user", username, kind=SecretKind.PROXY, actor="cli")
        user_secret = f"{prefix}proxy-user"
        pw = typer.prompt("Proxy password", hide_input=True)
        ctx.vault.put(f"{prefix}proxy-pass", pw, kind=SecretKind.PROXY, actor="cli")
        pass_secret = f"{prefix}proxy-pass"
    cfg = profile.config.model_copy(
        update={
            "proxy": ProxyConfig(server=server, username_secret=user_secret, password_secret=pass_secret),
            "driver": "patchright" if patchright else (driver or profile.config.driver),
        }
    )
    ctx.profiles.update_config(profile_id, cfg, actor="cli")
    # Re-pin the browser identity on the next open (egress/driver changed).
    ctx.db.execute("UPDATE profiles SET fingerprint_json = NULL WHERE id = ?", (profile_id,))
    typer.echo(f"{profile_id}: proxy {server}, driver {cfg.driver or ctx.settings.driver}")
    _doctor_after_change(profile_id)


@profile_app.command("set-driver")
def profile_set_driver(profile_id: str, driver: str):
    """Switch a profile's browser driver (playwright | patchright | cdp)."""
    ctx = _ctx()
    profile = ctx.profiles.get(profile_id)
    ctx.profiles.update_config(profile_id, profile.config.model_copy(update={"driver": driver}), actor="cli")
    ctx.db.execute("UPDATE profiles SET fingerprint_json = NULL WHERE id = ?", (profile_id,))
    typer.echo(f"{profile_id}: driver {driver}")
    _doctor_after_change(profile_id)


@app.command("preflight")
def preflight_cmd(
    profile_id: str,
    override: Annotated[bool, typer.Option("--override", help="allow ONE sign-in attempt in the next 30 min despite failures")] = False,
    fresh: Annotated[bool, typer.Option("--fresh", help="look up the egress again (ignore the 15-min cache)")] = False,
    google: Annotated[bool, typer.Option("--google", help="also require Google (for 'populate the sheet' requests)")] = False,
    as_json: Annotated[bool, typer.Option("--json", help="print the report as JSON (for deploy/egress.sh)")] = False,
):
    """Check a profile's sign-in prerequisites (driver, proxy, egress, identity pin, OTP, Google)."""
    ctx = _ctx()
    rep = asyncio.run(ctx.preflight.check(profile_id, need_google=google, fresh=fresh))
    if as_json:
        typer.echo(json.dumps({**rep.public(), "egress": ctx.prefs.get(f"egress:{profile_id}")}))
        raise typer.Exit(0 if rep.ok else 1)
    for line in rep.lines():
        typer.echo(line)
    if override:
        if rep.ok:
            typer.echo("All clear; no override needed.")
        else:
            until = ctx.preflight.grant_override(profile_id)
            typer.secho(f"Override granted: the next sign-in on {profile_id} (until {until[:16]}Z) runs despite the failures above. "
                        "A bounce still counts and starts the backoff.", fg="yellow")  # fmt: skip
    if not rep.ok and not override:
        raise typer.Exit(1)


@profile_app.command("doctor")
def profile_doctor(
    profile_id: str,
    repin: Annotated[Optional[bool], typer.Option("--repin/--no-repin", help="re-pin a stale identity without asking")] = None,
):
    """Check the pinned browser identity against the profile's config, and offer to re-pin a stale one."""
    from friday.profiles.doctor import examine

    ctx = _ctx()
    rep = examine(ctx.profiles, ctx.profiles.get(profile_id))
    for line in rep.lines():
        typer.echo(line)
    if rep.needs_repin and (repin if repin is not None else typer.confirm("Re-pin now (the next open records a fresh identity)?", default=True)):
        ctx.profiles.repin(profile_id, actor="cli", reason="; ".join(rep.issues)[:500])
        typer.echo(f"{profile_id}: re-pinned; the next open records the identity under the current config.")
    elif rep.issues and not rep.needs_repin:
        raise typer.Exit(1)


def _doctor_after_change(profile_id: str) -> None:
    from friday.profiles.doctor import examine

    ctx = _ctx()
    for line in examine(ctx.profiles, ctx.profiles.get(profile_id)).lines():
        typer.echo(line)


@profile_app.command("set-identity")
def profile_set_identity(
    profile_id: str,
    locale: Annotated[str, typer.Option(help="e.g. en-IN; match the egress IP's country")],
    timezone: Annotated[str, typer.Option(help="IANA zone, e.g. Asia/Kolkata")],
):
    """Set a profile's locale and timezone (they must agree with its egress IP)."""
    from zoneinfo import ZoneInfo

    ZoneInfo(timezone)  # raises on a typo
    ctx = _ctx()
    profile = ctx.profiles.get(profile_id)
    ctx.profiles.update_config(profile_id, profile.config.model_copy(update={"locale": locale, "timezone": timezone}), actor="cli")
    ctx.db.execute("UPDATE profiles SET fingerprint_json = NULL WHERE id = ?", (profile_id,))
    typer.echo(f"{profile_id}: locale {locale}, timezone {timezone}")
    _doctor_after_change(profile_id)


# One locale + timezone per country, for countries with one main zone. Others: pass --timezone.
REGIONS = {
    "IN": ("en-IN", "Asia/Kolkata"), "GB": ("en-GB", "Europe/London"), "SG": ("en-SG", "Asia/Singapore"),
    "AE": ("en-AE", "Asia/Dubai"), "IE": ("en-IE", "Europe/Dublin"), "NZ": ("en-NZ", "Pacific/Auckland"),
    "DE": ("de-DE", "Europe/Berlin"), "FR": ("fr-FR", "Europe/Paris"), "JP": ("ja-JP", "Asia/Tokyo"),
    "US": ("en-US", None), "CA": ("en-CA", None), "AU": ("en-AU", None),
}  # fmt: skip


@profile_app.command("set-region")
def profile_set_region(
    profile_id: str,
    country: Annotated[str, typer.Argument(help="two-letter country of the egress IP, e.g. IN")],
    timezone: Annotated[Optional[str], typer.Option(help="IANA zone, for countries with several (US, CA, AU)")] = None,
):
    """Set locale and timezone for a country in one step, and check them against the proxy's IP country."""
    from zoneinfo import ZoneInfo

    cc = country.upper()
    if cc not in REGIONS:
        typer.secho(f"unknown region {cc}; known: {', '.join(sorted(REGIONS))}. Use set-identity instead.", fg="red", err=True)
        raise typer.Exit(2)
    locale, tz = REGIONS[cc]
    tz = timezone or tz
    if tz is None:
        typer.secho(f"{cc} has several timezones; pass --timezone (e.g. America/New_York).", fg="red", err=True)
        raise typer.Exit(2)
    ZoneInfo(tz)
    ctx = _ctx()
    profile = ctx.profiles.get(profile_id)
    ctx.profiles.update_config(profile_id, profile.config.model_copy(update={"locale": locale, "timezone": tz}), actor="cli")
    typer.echo(f"{profile_id}: region {cc}: locale {locale}, timezone {tz}")
    host_tz = ctx.settings.default_timezone
    if host_tz and host_tz != tz:
        typer.secho(f"note: the server's FRIDAY_DEFAULT_TIMEZONE/TZ is {host_tz}; the browser gets {tz} per launch, "
                    "but keep them the same (deploy.sh TIMEZONE=...) so logs and schedules agree.", fg="yellow")  # fmt: skip
    profile = ctx.profiles.get(profile_id)
    if profile.config.proxy is None:
        typer.secho("no proxy yet: the egress country can't be checked (deploy/egress.sh).", fg="yellow")
    else:
        eg = asyncio.run(ctx.preflight.egress(profile, fresh=True))
        if eg.get("error"):
            typer.secho(f"couldn't look up the proxy's IP: {eg['error']}", fg="red")
            raise typer.Exit(1)
        if (eg.get("country") or "").upper() != cc:
            typer.secho(f"MISMATCH: the proxy exits in {eg.get('country')} ({eg.get('ip')} {eg.get('org')}), not {cc}.", fg="red")
            raise typer.Exit(1)
        typer.echo(f"egress {eg.get('ip')} {eg.get('org')} is in {cc}: consistent.")
    _doctor_after_change(profile_id)


@app.command("browser-check")
def browser_check_cmd(
    profile_id: str,
    as_json: Annotated[bool, typer.Option("--json", help="print the report as JSON")] = False,
):
    """Open the profile's real browser on neutral IP-echo pages (never a bank) and report what a site would see."""
    from friday.browsercheck import report_lines

    ctx = _ctx()
    rep = asyncio.run(ctx.browsercheck.run(profile_id))
    if as_json:
        typer.echo(json.dumps(rep, default=str))
    else:
        for line in report_lines(rep):
            typer.echo(line)
    raise typer.Exit(0 if rep["ok"] else 1)


@site_app.command("set-otp-senders")
def site_set_otp_senders(site_id: str, sender: Annotated[list[str], typer.Argument(help="addresses or @domains")]):
    """Replace a site's OTP email senders (used to find codes in Gmail)."""
    ctx = _ctx()
    site = ctx.sites.get(site_id)
    if site is None:
        typer.secho(f"no site {site_id}", fg="red", err=True)
        raise typer.Exit(2)
    ctx.sites.upsert(site.model_copy(update={"otp_senders": sender}), actor="cli")
    typer.echo(f"{site_id}: OTP senders {', '.join(sender)}")


@profile_app.command("wipe")
def profile_wipe(profile_id: str, yes: Annotated[bool, typer.Option("--yes")] = False):
    """Revoke everything for a profile: Chrome data, downloads, and its secrets."""
    if not yes:
        typer.confirm(f"Permanently wipe {profile_id} and all its secrets?", abort=True)
    _ctx().profiles.wipe(profile_id, actor="cli")
    typer.echo(f"wiped {profile_id}")


@profile_app.command("seed")
def profile_seed(profile_id: str):
    """Open the profile in a visible Chrome window on THIS machine for a one-time manual login.

    On a server (Xvfb), use the web app instead: POST /api/profiles/<id>/seed gives a remote-view link.
    """
    setup_logging()
    ctx = _ctx()
    if ctx.displays.backend != "native":
        typer.secho("No local display. Run `friday serve` and seed through the remote-view link.", fg="red", err=True)
        raise typer.Exit(2)

    async def run() -> None:
        async with ctx.profiles.open(profile_id, purpose="seed", actor="cli") as op:
            for w in op.warnings:
                typer.secho(f"warning: {w}", fg="yellow")
            page = await op.session.page()
            if op.site.login_url:
                await page.goto(op.site.login_url, wait_until="domcontentloaded")
            typer.echo("Log in in the Chrome window. Press Enter here when done (Ctrl-C to abort).")
            await asyncio.get_running_loop().run_in_executor(None, input)
            ctx.profiles.set_status(profile_id, "seeded")
            ctx.audit.record("handoff.done", actor="cli", target=profile_id)

    asyncio.run(run())
    typer.echo("Profile closed; cookies and storage are saved.")


@app.command("run")
def run_job(
    kind: str,
    profile_id: str,
    start: Annotated[Optional[str], typer.Option(help="fetch_statement: YYYY-MM-DD")] = None,
    end: Annotated[Optional[str], typer.Option(help="fetch_statement: YYYY-MM-DD")] = None,
    force: Annotated[bool, typer.Option(help="Ignore the site's failure backoff")] = False,
):
    """Run a job in this process (kinds: login, fetch_statement). Approvals/OTP/handoffs need `friday serve`."""
    setup_logging()
    ctx = _ctx()
    params = {"start": start, "end": end} if kind == "fetch_statement" else {}

    async def go() -> dict:
        run_id = ctx.jobs.submit(kind, profile_id, params, force=force)
        typer.echo(f"run {run_id} started")
        await ctx.jobs.wait_idle()
        return ctx.runs.get(run_id)

    r = asyncio.run(go())
    typer.echo(f"{r['status']}: {r['summary']}")
    raise typer.Exit(0 if r["status"] == "succeeded" else 1)


@app.command("runs")
def list_runs(profile_id: Optional[str] = None, limit: int = 20):
    for r in _ctx().runs.list(profile_id=profile_id, limit=limit):
        typer.echo(f"{r['id']}\t{r['kind']}\t{r['profile_id']}\t{r['status']}\t{(r['summary'] or '')[:80]}")


@app.command("diagnosis")
def diagnosis_show(report_id: Optional[str] = None):
    """List diagnosis reports, or show one."""
    ctx = _ctx()
    if report_id:
        typer.echo(json.dumps(ctx.diagnoser.get(report_id), indent=2))
        return
    for r in ctx.diagnoser.list():
        top = (r.get("causes") or [{}])[0]
        typer.echo(f"{r['id']}\t{r['profile_id']}\t{r['trigger_state']}\ttop: {top.get('cause')} ({top.get('likelihood')})")


@app.command("transactions")
def export_transactions(source: Optional[str] = None, start: Optional[str] = None, end: Optional[str] = None):
    """Print normalized transactions as CSV."""
    import csv
    import sys

    w = csv.writer(sys.stdout)
    cols = ["date", "description", "amount", "currency", "direction", "balance", "source", "raw_ref", "hash"]
    w.writerow(cols)
    for r in _ctx().transactions.list(source=source, start=start, end=end, limit=100000):
        w.writerow([r[c] for c in cols])


gmail_app = typer.Typer(no_args_is_help=True, help="Read-only Gmail access for emailed OTP codes.")
app.add_typer(gmail_app, name="gmail")


@gmail_app.command("connect")
def gmail_connect(port: int = 8766):
    """One-time Google OAuth (scope gmail.readonly). Needs FRIDAY_GOOGLE_CLIENT_ID/SECRET (a Desktop-app OAuth client)."""
    import http.server
    import secrets as pysecrets
    import threading
    import webbrowser
    from urllib.parse import parse_qs, urlparse

    from friday.otp.gmail import auth_url, pkce_pair

    ctx = _ctx()
    if not ctx.settings.google_client_id:
        typer.secho("Set FRIDAY_GOOGLE_CLIENT_ID and FRIDAY_GOOGLE_CLIENT_SECRET first.", fg="red", err=True)
        raise typer.Exit(2)
    verifier, challenge = pkce_pair()
    state = pysecrets.token_urlsafe(16)
    redirect = f"http://127.0.0.1:{port}/callback"
    got: dict = {}

    class H(http.server.BaseHTTPRequestHandler):
        def do_GET(self):
            q = parse_qs(urlparse(self.path).query)
            if q.get("state", [""])[0] == state:
                got.update(code=q.get("code", [""])[0], error=q.get("error", [""])[0])
            self.send_response(200)
            self.end_headers()
            self.wfile.write(b"You can close this tab and return to the terminal.")

        def log_message(self, *a):
            pass

    srv = http.server.HTTPServer(("127.0.0.1", port), H)
    threading.Thread(target=srv.handle_request, daemon=True).start()
    url = auth_url(ctx.settings.google_client_id, redirect, challenge, state)
    typer.echo(f"Opening Google consent (read-only Gmail). If no browser opens, visit:\n{url}")
    webbrowser.open(url)
    import time

    for _ in range(600):
        if got:
            break
        time.sleep(0.5)
    srv.server_close()
    if not got.get("code"):
        typer.secho(f"No authorization code received ({got.get('error') or 'timeout'}).", fg="red", err=True)
        raise typer.Exit(1)
    asyncio.run(ctx.gmail.exchange_code(got["code"], verifier, redirect))
    typer.echo("Gmail connected (read-only). The refresh token is stored encrypted in the vault.")


@gmail_app.command("disconnect")
def gmail_disconnect():
    from friday.otp.gmail import REFRESH_SECRET

    _ctx().vault.delete(REFRESH_SECRET, actor="cli")
    typer.echo("Removed the stored Gmail token. Also revoke access at https://myaccount.google.com/permissions")


@app.command("egress-proxy")
def egress_proxy(
    listen: Annotated[str, typer.Option(help="host:port, e.g. your Tailscale IP:3128")] = "127.0.0.1:3128",
    username: str = "friday",
    allow_any_interface: bool = False,
    allow_private_targets: bool = False,
):
    """Run the home egress proxy (password from FRIDAY_EGRESS_PASSWORD)."""
    import os

    from friday.egress import serve as egress_serve

    setup_logging()
    password = os.environ.get("FRIDAY_EGRESS_PASSWORD", "")
    if len(password) < 16:
        typer.secho("Set FRIDAY_EGRESS_PASSWORD (16+ chars).", fg="red", err=True)
        raise typer.Exit(2)
    host, _, port = listen.rpartition(":")
    if host in ("0.0.0.0", "::", "") and not allow_any_interface:
        typer.secho("Refusing to listen on all interfaces; bind to your VPN address.", fg="red", err=True)
        raise typer.Exit(2)

    async def main() -> None:
        srv = await egress_serve(host, int(port), username, password, block_private=not allow_private_targets)
        typer.echo(f"egress proxy on {listen}")
        async with srv:
            await srv.serve_forever()

    asyncio.run(main())


@app.command("mock-site")
def mock_site(port: int = 8780):
    """Run the local mock bank (for trying friday without a real account)."""
    import uvicorn

    from friday.mock_site import app as mock_app

    typer.echo(f"Mock bank on http://localhost:{port}/  (user alice / password correct-horse-battery)")
    uvicorn.run(mock_app, host="127.0.0.1", port=port, log_level="warning")


@app.command()
def serve():
    """Run the web app (remote view + API)."""
    import uvicorn

    from friday.web.app import create_app

    setup_logging()
    ctx = _ctx()
    try:
        web = create_app(ctx)
    except ConfigError as e:
        typer.secho(str(e), fg="red", err=True)
        raise typer.Exit(2) from e
    uvicorn.run(web, host=ctx.settings.bind_host, port=ctx.settings.port, log_config=None, proxy_headers=False)


if __name__ == "__main__":
    app()
