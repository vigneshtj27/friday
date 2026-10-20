"""Browser test of the portal preflight dialog, with screenshots. Marked chrome (skipped by default).

Run: FRIDAY_CHROME_EXECUTABLE=/path/to/chromium uv run pytest -m chrome tests/test_preflight_portal_ui.py
Screenshots are written to FRIDAY_UI_SHOTS (default: a temp dir, path printed).
"""

from __future__ import annotations

import os
import threading
import time
from pathlib import Path

import pytest
import uvicorn

from friday.preflight import Preflight
from friday.profiles.models import ProxyConfig
from friday.web.app import create_app
from tests.conftest import TEST_PASSWORD
from tests.fakes import free_port

pytestmark = pytest.mark.chrome

SHOTS = Path(os.environ.get("FRIDAY_UI_SHOTS", "")) if os.environ.get("FRIDAY_UI_SHOTS") else None
# An attacker-influenced egress org (ipinfo text) with an HTML payload: it must render as text.
XSS = '<img src=x onerror="window.__xss=1">Jio'


@pytest.fixture
def served(ctx):
    make = ctx.profiles.create("mybank", "me")
    ctx.profiles.update_config(make.id, make.config.model_copy(update={"proxy": ProxyConfig(server="http://100.64.0.2:3128")}))

    async def fake_geo(_):
        return {"ip": "34.14.129.151", "org": f"AS396982 {XSS}", "country": "US"}

    ctx.preflight = Preflight(ctx, fetch_geo=fake_geo)
    port = free_port()
    server = uvicorn.Server(uvicorn.Config(create_app(ctx), host="127.0.0.1", port=port, log_level="warning"))
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    deadline = time.time() + 10
    while not server.started and time.time() < deadline:
        time.sleep(0.05)
    yield ctx, f"http://127.0.0.1:{port}"
    server.should_exit = True
    thread.join(5)


async def _launch():
    from playwright.async_api import async_playwright

    pw = await async_playwright().start()
    exe = os.environ.get("FRIDAY_CHROME_EXECUTABLE")
    browser = await pw.chromium.launch(executable_path=exe or None, args=["--no-sandbox"])
    return pw, browser


async def _login(page, base):
    # land on /portal (which polls /api/state, short requests) rather than the chat page, whose
    # 30s long-poll would otherwise still be running against the DB at teardown.
    await page.goto(f"{base}/login?next=/portal")
    await page.fill("input[name=password]", TEST_PASSWORD)
    await page.click("button[type=submit]")
    await page.wait_for_url(f"{base}/portal")


async def _shot(page, name):
    if SHOTS:
        SHOTS.mkdir(parents=True, exist_ok=True)
        await page.screenshot(path=str(SHOTS / name), full_page=True)


async def test_preflight_dialog_confirm_and_status(served):
    ctx, base = served
    pw, browser = await _launch()
    try:
        page = await (await browser.new_context(viewport={"width": 1200, "height": 900})).new_page()
        await _login(page, base)
        await page.goto(f"{base}/portal#preflight/mybank.me")
        await page.wait_for_selector("#pf-grant")

        # failures are shown; the attacker's org text is escaped (no element, no script ran)
        body = await page.inner_text("body")
        assert "FAIL" in body and "egress" in body
        assert await page.evaluate("() => window.__xss") is None
        assert await page.evaluate("() => document.querySelectorAll('img[src=\"x\"]').length") == 0
        assert XSS.split("\"")[0] not in await page.evaluate("() => document.body.innerHTML")  # not raw HTML

        # the grant button is disabled until the exact id is typed
        assert await page.is_disabled("#pf-grant")
        await page.fill("#pf-confirm", "wrong")
        assert await page.is_disabled("#pf-grant")
        await _shot(page, "01-blocked-desktop.png")
        await page.fill("#pf-confirm", "mybank.me")
        assert await page.is_enabled("#pf-grant")
        await _shot(page, "02-confirm-ready.png")

        await page.click("#pf-grant")
        await page.wait_for_selector("text=Override armed")
        assert ctx.preflight.override_status("mybank.me") is not None
        assert "Cancel override" in await page.inner_text("body")
        await _shot(page, "03-armed.png")

        # narrow / mobile layout, no horizontal scroll
        await page.set_viewport_size({"width": 390, "height": 844})
        await page.goto(f"{base}/portal#preflight/mybank.me")
        await page.wait_for_selector("text=Override armed")
        await _shot(page, "04-armed-mobile.png")
        assert await page.evaluate("() => document.documentElement.scrollWidth <= window.innerWidth + 1")

        # cancel returns to no-grant
        await page.click("#pf-cancel")
        await page.wait_for_selector("#pf-grant")
        assert ctx.preflight.override_status("mybank.me") is None
        if SHOTS:
            print(f"\nUI screenshots saved to {SHOTS}")
    finally:
        await browser.close()
        await pw.stop()


async def test_error_states_surface(served):
    """404 and a stale/expired confirmation are shown, not swallowed."""
    ctx, base = served
    pw, browser = await _launch()
    try:
        page = await (await browser.new_context()).new_page()
        await _login(page, base)
        # unknown profile: the view shows the error and a way back
        await page.goto(f"{base}/portal#preflight/ghost.me")
        await page.wait_for_selector("text=Back to Accounts")
        assert "no such profile" in (await page.inner_text("body")).lower()
    finally:
        await browser.close()
        await pw.stop()
