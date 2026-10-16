from __future__ import annotations

import random

import pytest

from friday.vault.injector import InjectionRefused, InjectionScope, SecretInjector, host_allowed

SCOPE = InjectionScope("mybank.me", "mybank/me/", ("mybank.example",))


class FakeLocator:
    def __init__(self, frame_url: str):
        self.frame_url = frame_url
        self.typed = ""
        self.clicked = False

    async def evaluate(self, expression: str):
        return self.frame_url

    async def click(self):
        self.clicked = True

    async def fill(self, value: str):
        self.typed = value

    async def press_sequentially(self, text: str, *, delay: float = 0):
        self.typed += text


class FakePage:
    def __init__(self, url: str):
        self.url = url
        self.screenshot_kwargs = None

    def locator(self, selector: str):
        return ("selector", selector)

    async def screenshot(self, **kwargs):
        self.screenshot_kwargs = kwargs
        return b"png"


@pytest.fixture
def injector(ctx):
    ctx.vault.put("mybank/me/password", "Hunter-2-Strong")
    ctx.vault.put("otherbank/me/password", "Other-Pass-9")
    return SecretInjector(ctx.vault, ctx.audit, key_delay_ms=(0, 0), rng=random.Random(1))


async def test_fill_types_value_and_audits(ctx, injector):
    page, loc = FakePage("https://login.mybank.example/signin"), FakeLocator("https://login.mybank.example/signin")
    await injector.fill(page, loc, "secret:mybank/me/password", SCOPE)
    assert loc.typed == "Hunter-2-Strong" and loc.clicked
    use = ctx.audit.list(action_prefix="secret.use")[0]
    assert use.details["purpose"] == "fill@login.mybank.example"


class DroppingLocator(FakeLocator):
    """Loses the first `drop` keystrokes of the first attempt, like a freshly committed page."""

    def __init__(self, frame_url: str, drop: int, *, rejects_all: bool = False):
        super().__init__(frame_url)
        self.drop, self.rejects_all = drop, rejects_all

    async def press_sequentially(self, text: str, *, delay: float = 0):
        if self.drop > 0:
            self.drop -= 1
            return
        self.typed += text

    async def fill(self, value: str):
        self.typed = "" if self.rejects_all else value

    async def input_value(self):
        return self.typed


async def test_fill_retypes_when_keystrokes_are_dropped(ctx, injector):
    page, loc = FakePage("https://mybank.example/"), DroppingLocator("https://mybank.example/", drop=3)
    await injector.fill(page, loc, "secret:mybank/me/password", SCOPE)
    assert loc.typed == "Hunter-2-Strong"


async def test_fill_clears_and_refuses_when_field_never_matches(ctx, injector):
    page, loc = FakePage("https://mybank.example/"), DroppingLocator("https://mybank.example/", drop=10**6, rejects_all=True)
    with pytest.raises(InjectionRefused, match="did not accept"):
        await injector.fill(page, loc, "secret:mybank/me/password", SCOPE)
    assert loc.typed == ""
    assert "Hunter" not in repr(ctx.audit.list(limit=100))


@pytest.mark.parametrize(
    "page_url,frame_url,why",
    [
        ("https://evil.example/", "https://evil.example/", "not one of the site's domains"),
        ("https://mybank.example.evil.example/", "https://mybank.example.evil.example/", "not one of"),
        ("https://login.mybank.example/", "https://evil.example/frame", "not one of"),  # cross-origin iframe
        ("http://login.mybank.example/", "http://login.mybank.example/", "non-HTTPS"),
        ("https://login.mybank.example/", "data:text/html,x", "non-HTTPS"),
    ],
)
async def test_fill_refuses_wrong_target(ctx, injector, page_url, frame_url, why):
    loc = FakeLocator(frame_url)
    with pytest.raises(InjectionRefused, match=why):
        await injector.fill(FakePage(page_url), loc, "secret:mybank/me/password", SCOPE)
    assert loc.typed == ""
    assert not ctx.audit.list(action_prefix="secret.use")
    assert ctx.audit.list(action_prefix="secret.refused")


async def test_fill_refuses_other_profiles_secret(ctx, injector):
    loc = FakeLocator("https://login.mybank.example/")
    with pytest.raises(InjectionRefused, match="does not belong"):
        await injector.fill(FakePage("https://login.mybank.example/"), loc, "secret:otherbank/me/password", SCOPE)
    assert loc.typed == ""


async def test_fill_rejects_literal_values(injector):
    with pytest.raises(ValueError):
        await injector.fill(FakePage("https://mybank.example/"), FakeLocator("https://mybank.example/"), "Hunter-2-Strong", SCOPE)


async def test_localhost_http_allowed_for_mock_sites(ctx, injector):
    scope = InjectionScope("mybank.me", "mybank/me/", ("localhost",))
    loc = FakeLocator("http://localhost:8000/login")
    await injector.fill(FakePage("http://localhost:8000/login"), loc, "secret:mybank/me/password", scope)
    assert loc.typed == "Hunter-2-Strong"


async def test_screenshot_masks_password_fields_and_filled_locators(injector):
    page, loc = FakePage("https://mybank.example/"), FakeLocator("https://mybank.example/")
    await injector.fill(page, loc, "secret:mybank/me/password", SCOPE)
    await injector.screenshot(page)
    masks = page.screenshot_kwargs["mask"]
    assert ("selector", "input[type=password]") in masks
    assert loc in masks


def test_host_allowed():
    assert host_allowed("mybank.example", ["mybank.example"])
    assert host_allowed("www.MYBANK.example.", ["mybank.example"])
    assert not host_allowed("notmybank.example", ["mybank.example"])
    assert not host_allowed(None, ["mybank.example"])
