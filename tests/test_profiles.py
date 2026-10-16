from __future__ import annotations

import pytest

from friday.drivers.base import BrowserSession, Driver, LaunchRequest
from friday.profiles import manager as manager_mod
from friday.profiles.manager import ProfileBusy, VolumeNotEncrypted
from friday.profiles.models import ProfileConfig, ProxyConfig


class FakePage:
    def __init__(self, fp):
        self.fp = fp

    async def evaluate(self, js):
        return dict(self.fp)


class FakeDriver(Driver):
    name = "fake"

    def __init__(self, fp, stealth=False):
        self.fp = fp
        self.stealth = stealth
        self.requests: list[LaunchRequest] = []
        self.closed = 0

    async def launch(self, req):
        self.requests.append(req)

        async def close():
            self.closed += 1

        class Ctx:
            pages = [FakePage(self.fp)]

        return BrowserSession("fake", Ctx(), self.stealth, "155.0.0.0", close)


FP = {"userAgent": "UA/155", "language": "en-US", "timezone": "Asia/Kolkata", "screen": [1920, 1080, 24], "webdriver": True}


@pytest.fixture
def fake_driver(monkeypatch):
    drv = FakeDriver(FP)
    monkeypatch.setattr(manager_mod, "get_driver", lambda name: drv)
    return drv


def test_create_paths_are_isolated(ctx):
    a = ctx.profiles.create("mybank", "me")
    b = ctx.profiles.create("mybank", "spouse")
    assert a.id == "mybank.me"
    da, db_ = ctx.profiles.user_data_dir(a), ctx.profiles.user_data_dir(b)
    assert da != db_ and da.exists() and db_.exists()
    assert ctx.profiles.root in da.parents
    assert (da.parent.stat().st_mode & 0o777) == 0o700


@pytest.mark.parametrize("bad", ["../x", "Me", "a/b", "", "a.b"])
def test_account_slug_validation(ctx, bad):
    with pytest.raises(ValueError):
        ctx.profiles.create("mybank", bad)


def test_unknown_site_rejected(ctx):
    with pytest.raises(KeyError):
        ctx.profiles.create("nosuchsite", "me")


async def test_open_launches_headed_with_persistent_dir(ctx, fake_driver):
    p = ctx.profiles.create("mybank", "me", ProfileConfig(locale="en-IN", timezone="Asia/Kolkata"))
    async with ctx.profiles.open(p.id, purpose="test") as op:
        assert op.site.id == "mybank"
    req = fake_driver.requests[0]
    assert req.user_data_dir == ctx.profiles.user_data_dir(p)
    assert req.headless is False
    env = req.env()
    assert env["TZ"] == "Asia/Kolkata" and env["LANG"] == "en_IN.UTF-8"
    assert "--lang=en-IN" in req.chrome_args()
    assert fake_driver.closed == 1
    # Same directory on every open: never a throwaway profile.
    async with ctx.profiles.open(p.id, purpose="test"):
        pass
    assert fake_driver.requests[1].user_data_dir == req.user_data_dir


async def test_one_open_per_profile(ctx, fake_driver):
    p = ctx.profiles.create("mybank", "me")
    async with ctx.profiles.open(p.id, purpose="a"):
        assert ctx.profiles.is_busy(p.id)
        with pytest.raises(ProfileBusy):
            async with ctx.profiles.open(p.id, purpose="b"):
                pass
    assert not ctx.profiles.is_busy(p.id)


async def test_lock_released_when_launch_fails(ctx, monkeypatch):
    class Boom(FakeDriver):
        async def launch(self, req):
            raise RuntimeError("chrome crashed")

    monkeypatch.setattr(manager_mod, "get_driver", lambda name: Boom(FP))
    p = ctx.profiles.create("mybank", "me")
    with pytest.raises(RuntimeError):
        async with ctx.profiles.open(p.id, purpose="a"):
            pass
    assert not ctx.profiles.is_busy(p.id)


async def test_fingerprint_pinned_then_drift_audited(ctx, fake_driver):
    p = ctx.profiles.create("mybank", "me", ProfileConfig(locale="en-US", timezone="Asia/Kolkata"))
    async with ctx.profiles.open(p.id, purpose="t") as op:
        assert op.warnings == []
    assert ctx.profiles.get(p.id).fingerprint["timezone"] == "Asia/Kolkata"
    fake_driver.fp = {**FP, "userAgent": "UA/156", "timezone": "Europe/Berlin"}
    async with ctx.profiles.open(p.id, purpose="t") as op:
        assert any("timezone is Europe/Berlin" in w for w in op.warnings)
        assert any("drifted" in w and "timezone" in w and "userAgent" not in w for w in op.warnings)
    assert ctx.audit.list(action_prefix="profile.fingerprint_drift")


async def test_stealth_driver_is_recorded(ctx, monkeypatch):
    monkeypatch.setattr(manager_mod, "get_driver", lambda name: FakeDriver(FP, stealth=True))
    p = ctx.profiles.create("mybank", "me")
    async with ctx.profiles.open(p.id, purpose="t", driver="patchright"):
        pass
    opened = ctx.audit.list(action_prefix="profile.open")[0]
    assert opened.details["stealth"] is True and opened.details["driver"] == "patchright"


async def test_volume_check_refuses_unencrypted(ctx, fake_driver, monkeypatch):
    ctx.settings.volume_encrypted_assert = False
    monkeypatch.setattr(manager_mod, "detect_encrypted", lambda path: (False, "plain ext4"))
    p = ctx.profiles.create("mybank", "me")
    with pytest.raises(VolumeNotEncrypted):
        async with ctx.profiles.open(p.id, purpose="t"):
            pass
    assert fake_driver.requests == []
    ctx.settings.volume_check = "warn"
    async with ctx.profiles.open(p.id, purpose="t") as op:
        assert any("not confirmed encrypted" in w for w in op.warnings)


async def test_proxy_credentials_come_from_vault_and_are_scoped(ctx, fake_driver):
    ctx.vault.put("mybank/me/proxy-user", "puser")
    ctx.vault.put("mybank/me/proxy-pass", "ppass-123")
    cfg = ProfileConfig(
        proxy=ProxyConfig(
            server="http://home.example:3128", username_secret="mybank/me/proxy-user", password_secret="mybank/me/proxy-pass"
        )
    )
    p = ctx.profiles.create("mybank", "me", cfg)
    async with ctx.profiles.open(p.id, purpose="t"):
        pass
    proxy = fake_driver.requests[0].proxy
    assert proxy.server == "http://home.example:3128" and proxy.password == "ppass-123"
    assert "ppass" not in repr(proxy)

    ctx.vault.put("otherbank/me/proxy-pass", "nope-nope")
    bad = ProfileConfig(proxy=ProxyConfig(server="http://h.example:1", password_secret="otherbank/me/proxy-pass"))
    p2 = ctx.profiles.create("mybank", "spouse", bad)
    with pytest.raises(Exception, match="must live under"):
        async with ctx.profiles.open(p2.id, purpose="t"):
            pass


def test_proxy_server_rejects_inline_credentials():
    with pytest.raises(ValueError):
        ProxyConfig(server="http://user:pass@host:8080")


async def test_wipe_revokes_everything(ctx, fake_driver):
    p = ctx.profiles.create("mybank", "me")
    ctx.vault.put("mybank/me/password", "pw-to-wipe")
    ctx.vault.put("mybank/spouse/password", "keep-me")
    udd = ctx.profiles.user_data_dir(p)
    (udd / "Cookies").write_text("x")
    async with ctx.profiles.open(p.id, purpose="t"):
        with pytest.raises(ProfileBusy):
            ctx.profiles.wipe(p.id)
    ctx.profiles.wipe(p.id)
    assert not udd.exists()
    assert [m.name for m in ctx.vault.list()] == ["mybank/spouse/password"]
    assert ctx.profiles.list() == []
    assert ctx.audit.list(action_prefix="profile.wipe")[0].details["secrets_deleted"] == 1


def test_timezone_aliases_match():
    from friday.profiles.manager import same_timezone

    assert same_timezone("Asia/Calcutta", "Asia/Kolkata")
    assert same_timezone("Europe/Kiev", "Europe/Kyiv")
    assert not same_timezone("Asia/Kolkata", "Europe/Berlin")
