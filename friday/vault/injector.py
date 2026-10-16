"""Last-moment secret injection into page fields, and masked screenshots.

The model only ever handles references like `secret:hdfc/me/password`. The
injector resolves them, and refuses unless:
  * the secret belongs to the profile currently being driven, and
  * the target field's own document (the frame, not just the tab) is on one
    of the site's registered domains, over HTTPS (localhost exempt for mocks).
"""

from __future__ import annotations

import asyncio
import random
import weakref
from dataclasses import dataclass
from typing import Any, Protocol
from urllib.parse import urlparse

from friday.audit import AuditLog
from friday.vault.store import SecretRef, SecretStore

LOCAL_HOSTS = {"localhost", "127.0.0.1", "::1"}
PASSWORD_SELECTOR = "input[type=password]"


class InjectionRefused(Exception):
    pass


@dataclass(frozen=True)
class InjectionScope:
    profile_id: str
    secret_prefix: str  # e.g. "hdfc/me/"
    allowed_domains: tuple[str, ...]


def host_allowed(host: str | None, domains: tuple[str, ...] | list[str]) -> bool:
    if not host:
        return False
    host = host.lower().rstrip(".")
    return any(host == d or host.endswith("." + d) for d in (d.lower().lstrip(".") for d in domains))


def check_target_url(url: str, domains: tuple[str, ...] | list[str]) -> str:
    """Return the host if `url` may receive a secret, else raise."""
    parsed = urlparse(url)
    host = parsed.hostname
    if parsed.scheme != "https" and not (parsed.scheme == "http" and host in LOCAL_HOSTS):
        raise InjectionRefused(f"refusing to type a secret into a non-HTTPS page ({parsed.scheme}://{host})")
    if not host_allowed(host, domains):
        raise InjectionRefused(f"refusing to type a secret into {host}: not one of the site's domains")
    return host  # type: ignore[return-value]


class LocatorLike(Protocol):
    async def evaluate(self, expression: str) -> Any: ...
    async def fill(self, value: str) -> None: ...
    async def click(self) -> None: ...
    async def press_sequentially(self, text: str, *, delay: float = 0) -> None: ...


async def _read_value(locator: Any) -> str | None:
    try:
        return await locator.input_value()
    except Exception:  # contenteditable, detached, or a locator without input_value
        return None


async def type_verified(locator: Any, text: str, delay: Any) -> tuple[bool, str | None]:
    """Type `text` key by key, then read the field back.

    Chrome started without automation flags drops keystrokes that arrive in the
    first few hundred milliseconds after a page commits, so a password could
    silently lose its first characters and burn a login attempt. On a mismatch
    the field is cleared and retyped once; if two attempts agree the field is
    deliberately reformatting input (maxlength, separators) and that is accepted;
    otherwise the value is set directly. Returns (ok, value_now_in_field).
    """
    seen: list[str | None] = []
    for _ in range(2):
        await locator.click()
        await locator.fill("")
        for ch in text:
            await locator.press_sequentially(ch)
            await asyncio.sleep(delay())
        now = await _read_value(locator)
        if now is None or now == text:
            return True, now
        if now and seen and seen[-1] == now:
            return True, now
        seen.append(now)
    await locator.fill(text)
    now = await _read_value(locator)
    return now == text, now


class SecretInjector:
    def __init__(
        self,
        store: SecretStore,
        audit: AuditLog,
        *,
        key_delay_ms: tuple[int, int] = (55, 150),
        rng: random.Random | None = None,
    ):
        self._store = store
        self._audit = audit
        self._key_delay_ms = key_delay_ms
        self._rng = rng or random.Random()
        self._filled: weakref.WeakKeyDictionary[Any, list[Any]] = weakref.WeakKeyDictionary()

    async def fill(
        self,
        page: Any,
        locator: LocatorLike,
        ref: SecretRef | str,
        scope: InjectionScope,
        *,
        human: bool = True,
    ) -> None:
        ref = SecretRef.coerce(ref)
        try:
            if not ref.name.startswith(scope.secret_prefix):
                raise InjectionRefused(f"{ref} does not belong to profile {scope.profile_id}")
            check_target_url(page.url, scope.allowed_domains)
            frame_url = await locator.evaluate("el => el.ownerDocument.location.href")
            host = check_target_url(frame_url, scope.allowed_domains)
        except InjectionRefused as e:
            self._audit.record("secret.refused", target=ref.name, profile=scope.profile_id, reason=str(e))
            raise

        value = self._store.reveal(ref.name, purpose=f"fill@{host}")
        try:
            if human:
                ok, _ = await type_verified(locator, value, lambda: self._rng.uniform(*self._key_delay_ms) / 1000)
            else:
                await locator.click()
                await locator.fill(value)
                ok = (await _read_value(locator)) in (None, value)
        finally:
            del value
        if not ok:
            # Never leave a wrong secret in a field the agent is about to submit.
            try:
                await locator.fill("")
            except Exception:
                pass
            self._audit.record("secret.refused", target=ref.name, profile=scope.profile_id, reason="field did not accept the value")
            raise InjectionRefused(f"the field did not accept {ref} reliably; it was cleared and nothing was submitted")
        try:  # lets page snapshots recognise the field and never show its value
            await locator.evaluate("el => el.setAttribute('data-friday-secret', '1')")
        except Exception:
            pass
        self._filled.setdefault(page, []).append(locator)

    def mask_locators(self, page: Any) -> list[Any]:
        """Everything to paint over in a screenshot of `page`."""
        return [page.locator(PASSWORD_SELECTOR), *self._filled.get(page, [])]

    async def screenshot(self, page: Any, **kwargs: Any) -> bytes:
        """The only screenshot path allowed for anything that leaves the executor."""
        return await page.screenshot(mask=self.mask_locators(page), mask_color="#000000", **kwargs)
