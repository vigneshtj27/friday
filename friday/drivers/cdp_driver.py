"""Launch Chrome as a plain process and attach Playwright over CDP.

No --enable-automation and none of Playwright's default launch flags, so the
browser process is what a user would get by starting Chrome with a profile
directory. Playwright still attaches over the DevTools protocol.
"""

from __future__ import annotations

import asyncio
import logging

from friday.config import ConfigError
from friday.drivers.base import BrowserSession, Driver, LaunchRequest, find_chrome_executable

log = logging.getLogger(__name__)


class CdpAttachDriver(Driver):
    name = "cdp"
    stealth = False

    async def launch(self, req: LaunchRequest) -> BrowserSession:
        from playwright.async_api import async_playwright

        if req.proxy and (req.proxy.username or req.proxy.password):
            raise ConfigError("the cdp driver cannot do proxy authentication; use an IP-allowlisted proxy")
        exe = req.executable or find_chrome_executable()
        port_file = req.user_data_dir / "DevToolsActivePort"
        port_file.unlink(missing_ok=True)
        args = [
            str(exe),
            f"--user-data-dir={req.user_data_dir}",
            "--remote-debugging-address=127.0.0.1",
            "--remote-debugging-port=0",
            "--no-first-run",
            "--no-default-browser-check",
            *req.chrome_args(),
            *req.macos_language_args(),
        ]
        if req.proxy:
            args.append(f"--proxy-server={req.proxy.server}")
            if req.proxy.bypass:
                args.append(f"--proxy-bypass-list={req.proxy.bypass}")
        if not req.sandbox:
            args.append("--no-sandbox")
        if req.headless:
            args.append("--headless=new")
        args.append("about:blank")
        proc = await asyncio.create_subprocess_exec(
            *args, env=req.env(), stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.DEVNULL
        )

        async def kill() -> None:
            if proc.returncode is None:
                proc.terminate()
                try:
                    await asyncio.wait_for(proc.wait(), 10)
                except TimeoutError:
                    proc.kill()

        try:
            loop = asyncio.get_running_loop()
            deadline = loop.time() + 30
            while not (port_file.exists() and port_file.read_text().strip()):
                if proc.returncode is not None:
                    raise RuntimeError(f"Chrome exited early with code {proc.returncode}")
                if loop.time() > deadline:
                    raise RuntimeError("Chrome did not open a DevTools port")
                await asyncio.sleep(0.1)
            port = int(port_file.read_text().splitlines()[0])
            pw = await async_playwright().start()
        except BaseException:
            await kill()
            raise
        browser = None
        for attempt in range(2):  # attaching occasionally stalls when many Chromes start at once
            try:
                browser = await pw.chromium.connect_over_cdp(f"http://127.0.0.1:{port}", timeout=30000)
                break
            except Exception:
                if attempt == 1:
                    await pw.stop()
                    await kill()
                    raise
                log.warning("CDP attach timed out; retrying once")
        assert browser is not None
        context = browser.contexts[0]

        async def close() -> None:
            try:
                # Ask Chrome to shut down cleanly so cookies and storage are flushed.
                cdp = await browser.new_browser_cdp_session()
                await cdp.send("Browser.close")
                await asyncio.wait_for(proc.wait(), 15)
            except Exception:
                log.warning("clean Chrome shutdown failed; terminating", exc_info=True)
            finally:
                await kill()
                await pw.stop()

        return BrowserSession(self.name, context, False, browser.version, close)
