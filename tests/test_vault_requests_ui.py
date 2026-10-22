"""Browser test of the in-chat vault-request card, with a screenshot. Marked chrome (skipped by
default). Run: FRIDAY_CHROME_EXECUTABLE=/path/to/chromium uv run pytest -m chrome tests/test_vault_requests_ui.py
"""

from __future__ import annotations

import os
import threading
import time
from pathlib import Path

import pytest
import uvicorn

from friday.web.app import create_app
from tests.conftest import TEST_PASSWORD
from tests.fakes import free_port

pytestmark = pytest.mark.chrome

SHOTS = Path(os.environ["FRIDAY_UI_SHOTS"]) if os.environ.get("FRIDAY_UI_SHOTS") else None
XSS = '<img src=x onerror="window.__xss=1">proxy'


@pytest.fixture
def served(ctx):
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


async def test_vault_card_renders_and_submits(served):
    ctx, base = served
    from playwright.async_api import async_playwright

    # a card with an attacker-influenced purpose string, to prove it is escaped
    rid = await ctx.vault_requests.request("egress_proxy", purpose=f"route {XSS} through home")

    pw = await async_playwright().start()
    browser = await pw.chromium.launch(executable_path=os.environ.get("FRIDAY_CHROME_EXECUTABLE") or None, args=["--no-sandbox"])
    try:
        page = await (await browser.new_context()).new_page()
        await page.goto(f"{base}/login?next=/chat")
        await page.fill("input[name=password]", TEST_PASSWORD)
        await page.click("button[type=submit]")
        await page.wait_for_url(f"{base}/chat")
        await page.wait_for_selector("form[data-vault]")

        # fields are password inputs; the injected HTML is escaped (no element, no script ran)
        assert await page.evaluate("() => window.__xss") is None
        assert await page.evaluate("() => document.querySelectorAll('img[src=\"x\"]').length") == 0
        inputs = page.locator("form[data-vault] input[name]")
        assert await inputs.count() == 2
        assert await page.locator('input[name="system/proxy/password"]').get_attribute("type") == "password"
        assert await page.locator('input[name="system/proxy/password"]').get_attribute("autocomplete") == "off"
        if SHOTS:
            SHOTS.mkdir(parents=True, exist_ok=True)
            await page.screenshot(path=str(SHOTS / "vault-card.png"), full_page=True)

        await page.fill('input[name="system/proxy/username"]', "friday")
        await page.fill('input[name="system/proxy/password"]', "Egress-Secret-xyz-123")
        await page.click("form[data-vault] button[type=submit]")
        await page.wait_for_selector("text=Saved to the encrypted vault", timeout=5000)
        assert ctx.vault.reveal("system/proxy/password", purpose="t") == "Egress-Secret-xyz-123"
        # the value never appears in the page DOM
        assert "Egress-Secret-xyz-123" not in await page.content()
    finally:
        await browser.close()
        await pw.stop()
