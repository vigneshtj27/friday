"""Real Google Chrome, real profile directory. Run with: uv run pytest -m chrome

Opens visible Chrome windows (native display) or uses Xvfb on Linux.
"""

from __future__ import annotations

import http.server
import threading

import pytest

from friday.profiles.models import Site
from friday.vault.injector import InjectionScope

pytestmark = pytest.mark.chrome

LOGIN_HTML = b"""<!doctype html><title>mock</title>
<form><input id=user name=user><input id=pw type=password name=pw>
<input id=note name=note></form>
<script>document.cookie = document.cookie.includes('seen=1') ? 'seen=2; max-age=3600' : 'seen=1; max-age=3600';</script>"""


@pytest.fixture
def local_site():
    class H(http.server.BaseHTTPRequestHandler):
        def do_GET(self):
            self.send_response(200)
            self.send_header("Content-Type", "text/html")
            self.end_headers()
            self.wfile.write(LOGIN_HTML)

        def log_message(self, *a):
            pass

    srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), H)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    yield f"http://localhost:{srv.server_address[1]}/"
    srv.shutdown()


@pytest.mark.parametrize("driver", ["playwright", "cdp"])
async def test_real_chrome_persistent_profile(ctx, local_site, driver):
    ctx.settings.display_backend = "auto"
    ctx.sites.upsert(Site(id="mock", name="Mock", login_url=local_site, domains=["localhost"]))
    p = ctx.profiles.create("mock", "me")
    ctx.vault.put("mock/me/password", "Real-Chrome-Pw-1")
    scope = InjectionScope(p.id, p.secret_prefix, ("localhost",))

    async with ctx.profiles.open(p.id, purpose="smoke", driver=driver) as op:
        page = await op.session.page()
        await page.goto(local_site)
        assert "seen=1" in await page.evaluate("document.cookie")
        await ctx.injector.fill(page, page.locator("#pw"), "secret:mock/me/password", scope)
        assert await page.locator("#pw").input_value() == "Real-Chrome-Pw-1"
        png = await ctx.injector.screenshot(page)
        assert png[:4] == b"\x89PNG"
        fp = await page.evaluate("Intl.DateTimeFormat().resolvedOptions().timeZone")
        print(f"\n[{driver}] version={op.session.browser_version} tz={fp} warnings={op.warnings}")

    pinned = ctx.profiles.get(p.id).fingerprint
    assert pinned and "Chrome/" in pinned["userAgent"] and "Headless" not in pinned["userAgent"]
    print(f"[{driver}] webdriver={pinned['webdriver']} ua={pinned['userAgent']}")

    # Same profile, new process: the cookie written last run must still be there.
    async with ctx.profiles.open(p.id, purpose="smoke", driver=driver) as op:
        page = await op.session.page()
        await page.goto(local_site)
        assert "seen=2" in await page.evaluate("document.cookie")
    assert ctx.audit.verify_chain()
    assert "Real-Chrome-Pw-1" not in repr(ctx.audit.list(limit=1000))


@pytest.mark.parametrize("driver,expect_webdriver", [("playwright", True), ("patchright", False)])
async def test_driver_webdriver_flag_and_locale(driver, expect_webdriver):
    """patchright removes navigator.webdriver; locale is set consistently from the profile."""
    import tempfile
    from pathlib import Path

    from friday.agent import isolated
    from friday.drivers import get_driver, LaunchRequest

    d = Path(tempfile.mkdtemp())
    req = LaunchRequest(user_data_dir=d / "c", downloads_dir=d / "dl", locale="en-IN",
                        timezone="Asia/Kolkata", window_width=1280, window_height=800)
    session = await get_driver(driver).launch(req)
    try:
        page = await session.page()
        await page.goto("https://example.com", wait_until="domcontentloaded", timeout=45000)
        wd = await isolated.evaluate(page.main_frame, "() => navigator.webdriver")
        lang = await isolated.evaluate(page.main_frame, "() => navigator.language")
        assert bool(wd) is expect_webdriver, f"{driver} webdriver={wd}"
        assert session.stealth is (driver == "patchright")
        if driver == "playwright":
            assert lang == "en-IN", f"locale not applied: {lang}"
    finally:
        await session.close()
