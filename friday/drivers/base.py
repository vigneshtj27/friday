"""Pluggable ways of launching real Google Chrome on a persistent profile.

Every driver hands back a Playwright `BrowserContext`, so the agent's tools
have one page API regardless of how Chrome was started:

* playwright  - Playwright launch_persistent_context, channel="chrome". No stealth.
* patchright  - same API via the Patchright fork (stealth patches). Opt-in only;
                the session is flagged `stealth=True` and the open is audited.
* cdp         - we start Chrome ourselves as a plain process (no automation
                launch flags) and attach Playwright over CDP. This is the same
                idea nodriver uses, without adopting nodriver's separate API.
"""

from __future__ import annotations

import os
import sys
from abc import ABC, abstractmethod
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from friday.config import ConfigError


@dataclass(frozen=True)
class ProxySettings:
    server: str
    username: str | None = None
    password: str | None = None
    bypass: str | None = None

    def __repr__(self) -> str:
        return f"ProxySettings(server={self.server!r}, auth={'yes' if self.username else 'no'})"

    def playwright(self) -> dict[str, str]:
        d = {"server": self.server}
        if self.username:
            d["username"] = self.username
        if self.password:
            d["password"] = self.password
        if self.bypass:
            d["bypass"] = self.bypass
        return d


@dataclass
class LaunchRequest:
    user_data_dir: Path
    downloads_dir: Path
    locale: str
    timezone: str | None
    window_width: int
    window_height: int
    display_env: dict[str, str] = field(default_factory=dict)
    headless: bool = False  # only for diagnosis experiments
    proxy: ProxySettings | None = None
    channel: str = "chrome"
    executable: Path | None = None
    sandbox: bool = True

    def env(self) -> dict[str, str]:
        env = dict(os.environ)
        env.update(self.display_env)
        if self.timezone:
            env["TZ"] = self.timezone
        lang = self.locale.replace("-", "_")
        env["LANG"] = env["LANGUAGE"] = f"{lang}.UTF-8"
        return env

    def chrome_args(self) -> list[str]:
        args = [
            f"--lang={self.locale}",
            f"--window-size={self.window_width},{self.window_height}",
            "--window-position=0,0",
        ]
        return args

    def macos_language_args(self) -> list[str]:
        """macOS Chrome ignores --lang and reads the Cocoa AppleLanguages default, which a
        per-process argument pair overrides. Playwright refuses non-flag args, so only the
        cdp driver can pass these."""
        return ["-AppleLanguages", f"({self.locale})"] if sys.platform == "darwin" else []


@dataclass
class BrowserSession:
    driver: str
    context: Any  # playwright.async_api.BrowserContext
    stealth: bool
    browser_version: str | None
    _closer: Callable[[], Awaitable[None]]

    async def page(self) -> Any:
        pages = self.context.pages
        return pages[0] if pages else await self.context.new_page()

    async def close(self) -> None:
        await self._closer()


class Driver(ABC):
    name: str
    stealth: bool = False

    @abstractmethod
    async def launch(self, req: LaunchRequest) -> BrowserSession: ...


def find_chrome_executable() -> Path:
    candidates = {
        "darwin": ["/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"],
        "linux": ["/usr/bin/google-chrome-stable", "/usr/bin/google-chrome", "/opt/google/chrome/chrome"],
        "win32": [r"C:\Program Files\Google\Chrome\Application\chrome.exe"],
    }.get(sys.platform, [])
    for c in candidates:
        if Path(c).exists():
            return Path(c)
    raise ConfigError("Google Chrome not found; install it or set FRIDAY_CHROME_EXECUTABLE")


def get_driver(name: str) -> Driver:
    if name == "playwright":
        from friday.drivers.playwright_driver import PlaywrightDriver

        return PlaywrightDriver("playwright")
    if name == "patchright":
        from friday.drivers.playwright_driver import PlaywrightDriver

        return PlaywrightDriver("patchright")
    if name == "cdp":
        from friday.drivers.cdp_driver import CdpAttachDriver

        return CdpAttachDriver()
    if name == "nodriver":
        raise ConfigError("nodriver was evaluated and not integrated; see docs/decisions.md. Use 'cdp' instead.")
    raise ConfigError(f"unknown driver {name!r}")
