from __future__ import annotations

import importlib
import logging

from friday.drivers.base import BrowserSession, Driver, LaunchRequest

log = logging.getLogger(__name__)


class PlaywrightDriver(Driver):
    """Playwright (or its Patchright fork, which has the same API)."""

    def __init__(self, module: str = "playwright"):
        if module not in ("playwright", "patchright"):
            raise ValueError(module)
        self.name = module
        self.stealth = module == "patchright"

    async def launch(self, req: LaunchRequest) -> BrowserSession:
        api = importlib.import_module(f"{self.name}.async_api")
        pw = await api.async_playwright().start()
        try:
            # Locale and timezone come from the process environment and --lang,
            # not from Playwright's CDP emulation overrides, so what the page
            # sees is what this Chrome genuinely is. Viewport follows the real window.
            context = await pw.chromium.launch_persistent_context(
                str(req.user_data_dir),
                # An explicit executable (FRIDAY_CHROME_EXECUTABLE) wins over the channel lookup.
                channel=None if req.executable else req.channel,
                executable_path=str(req.executable) if req.executable else None,
                headless=req.headless,
                # One consistent locale per profile, matching the egress IP's country.
                locale=req.locale,
                # Playwright defaults to --no-sandbox on Linux: a security downgrade and a
                # visible "unsupported flag" bar no real user has. Keep Chrome's sandbox on.
                chromium_sandbox=req.sandbox,
                env=req.env(),
                args=req.chrome_args(),
                no_viewport=True,
                accept_downloads=True,
                downloads_path=str(req.downloads_dir),
                proxy=req.proxy.playwright() if req.proxy else None,
            )
        except BaseException:
            await pw.stop()
            raise
        version = context.browser.version if context.browser else None

        async def close() -> None:
            try:
                await context.close()
            finally:
                await pw.stop()

        return BrowserSession(self.name, context, self.stealth, version, close)
