"""Persistent Chrome profiles: one user_data_dir per (site, account).

* A profile is never shared between accounts and never thrown away per run.
* At most one open per profile at a time (OS file lock, so it also holds
  across processes).
* The browser identity is pinned: on first launch we record what Chrome
  reports (version, UA, locale, timezone, screen) and audit any drift later.
"""

from __future__ import annotations

import fcntl
import json
import logging
import os
import shutil
import tempfile
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from friday.audit import AuditLog
from friday.config import ConfigError, Settings
from friday.db import Database
from friday.drivers import BrowserSession, LaunchRequest, ProxySettings, get_driver
from friday.profiles.display import Display, DisplayAllocator
from friday.profiles.models import Profile, ProfileConfig, Site, validate_slug
from friday.profiles.volume import detect_encrypted
from friday.util import iso
from friday.vault.store import SecretStore

log = logging.getLogger(__name__)

FINGERPRINT_JS = """() => ({
  userAgent: navigator.userAgent,
  language: navigator.language,
  languages: navigator.languages,
  timezone: Intl.DateTimeFormat().resolvedOptions().timeZone,
  platform: navigator.platform,
  hardwareConcurrency: navigator.hardwareConcurrency,
  deviceMemory: navigator.deviceMemory ?? null,
  screen: [screen.width, screen.height, screen.colorDepth],
  webdriver: navigator.webdriver,
})"""
# Fields that legitimately change (Chrome auto-updates).
VOLATILE_FINGERPRINT = {"userAgent", "browserVersion"}


# Legacy IANA names ICU/Chrome may still report, mapped to current names.
TZ_ALIASES = {
    "Asia/Calcutta": "Asia/Kolkata",
    "Asia/Katmandu": "Asia/Kathmandu",
    "Asia/Saigon": "Asia/Ho_Chi_Minh",
    "Asia/Rangoon": "Asia/Yangon",
    "Europe/Kiev": "Europe/Kyiv",
    "America/Buenos_Aires": "America/Argentina/Buenos_Aires",
    "Atlantic/Faeroe": "Atlantic/Faroe",
    "Pacific/Truk": "Pacific/Chuuk",
    "US/Eastern": "America/New_York",
    "US/Central": "America/Chicago",
    "US/Mountain": "America/Denver",
    "US/Pacific": "America/Los_Angeles",
}


def same_timezone(a: str | None, b: str | None) -> bool:
    if not a or not b:
        return a == b
    a, b = TZ_ALIASES.get(a, a), TZ_ALIASES.get(b, b)
    if a == b:
        return True
    import zoneinfo

    for root in zoneinfo.TZPATH:  # identical tzfile data means one is a link to the other
        pa, pb = Path(root) / a, Path(root) / b
        if pa.is_file() and pb.is_file():
            return pa.read_bytes() == pb.read_bytes()
    return False


class ProfileBusy(RuntimeError):
    pass


class ProfileNotFound(KeyError):
    pass


class VolumeNotEncrypted(ConfigError):
    pass


class SiteRegistry:
    def __init__(self, db: Database, audit: AuditLog):
        self._db = db
        self._audit = audit

    def upsert(self, site: Site, *, actor: str = "admin") -> Site:
        self._db.execute(
            "INSERT INTO sites (id, name, login_url, domains_json, otp_domains_json, otp_senders_json, created_at)"
            " VALUES (?, ?, ?, ?, ?, ?, ?) ON CONFLICT(id) DO UPDATE SET name=excluded.name,"
            " login_url=excluded.login_url, domains_json=excluded.domains_json,"
            " otp_domains_json=excluded.otp_domains_json, otp_senders_json=excluded.otp_senders_json",
            (
                site.id,
                site.name,
                site.login_url,
                json.dumps(site.domains),
                json.dumps(site.otp_domains),
                json.dumps(site.otp_senders),
                iso(),
            ),
        )
        self._audit.record("site.upsert", actor=actor, target=site.id, domains=site.domains)
        return site

    def get(self, site_id: str) -> Site | None:
        r = self._db.query_one("SELECT * FROM sites WHERE id = ?", (site_id,))
        return self._row(r) if r else None

    def list(self) -> list[Site]:
        return [self._row(r) for r in self._db.query("SELECT * FROM sites ORDER BY id")]

    @staticmethod
    def _row(r: Any) -> Site:
        return Site(
            id=r["id"],
            name=r["name"],
            login_url=r["login_url"],
            domains=json.loads(r["domains_json"]),
            otp_domains=json.loads(r["otp_domains_json"]),
            otp_senders=json.loads(r["otp_senders_json"]),
        )


class _FileLock:
    def __init__(self, path: Path):
        self.path = path
        self._fd: int | None = None

    def acquire(self) -> bool:
        fd = os.open(self.path, os.O_RDWR | os.O_CREAT, 0o600)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            os.close(fd)
            return False
        self._fd = fd
        return True

    def release(self) -> None:
        if self._fd is not None:
            fcntl.flock(self._fd, fcntl.LOCK_UN)
            os.close(self._fd)
            self._fd = None


@dataclass
class OpenProfile:
    profile: Profile
    site: Site
    session: BrowserSession
    display: Display
    warnings: list[str] = field(default_factory=list)


class ProfileManager:
    def __init__(
        self,
        settings: Settings,
        db: Database,
        audit: AuditLog,
        vault: SecretStore,
        sites: SiteRegistry,
        displays: DisplayAllocator,
    ):
        self.settings = settings
        self._db = db
        self._audit = audit
        self._vault = vault
        self._sites = sites
        self._displays = displays
        self.root = settings.resolved_profiles_dir
        vault._redactor.add_names(*(r["id"] for r in db.query("SELECT id FROM profiles")))

    # ---- CRUD -----------------------------------------------------------

    def create(self, site_id: str, account: str, config: ProfileConfig | None = None, *, actor: str = "admin") -> Profile:
        validate_slug(account, "account")
        if self._sites.get(site_id) is None:
            raise KeyError(f"unknown site {site_id!r}; add it first")
        config = config or ProfileConfig(
            locale=self.settings.default_locale,
            timezone=self.settings.default_timezone,
            window_width=self.settings.screen_width,
            window_height=self.settings.screen_height,
        )
        profile = Profile(
            id=Profile.make_id(site_id, account), site_id=site_id, account=account, config=config, created_at=iso()
        )
        self._vault._redactor.add_names(profile.id)
        self._db.execute(
            "INSERT INTO profiles (id, site_id, account, config_json, status, created_at) VALUES (?, ?, ?, ?, ?, ?)",
            (profile.id, site_id, account, config.model_dump_json(), profile.status, profile.created_at),
        )
        self.root.mkdir(parents=True, exist_ok=True, mode=0o700)
        self._base(profile).mkdir(parents=True, exist_ok=True, mode=0o700)
        self.user_data_dir(profile).mkdir(exist_ok=True, mode=0o700)
        self._audit.record("profile.create", actor=actor, target=profile.id)
        return profile

    def get(self, profile_id: str) -> Profile:
        r = self._db.query_one("SELECT * FROM profiles WHERE id = ?", (profile_id,))
        if r is None:
            raise ProfileNotFound(profile_id)
        return self._row(r)

    def list(self) -> list[Profile]:
        return [self._row(r) for r in self._db.query("SELECT * FROM profiles ORDER BY id")]

    def set_status(self, profile_id: str, status: str) -> None:
        self._db.execute("UPDATE profiles SET status = ? WHERE id = ?", (status, profile_id))

    def update_config(self, profile_id: str, config: ProfileConfig, *, actor: str = "admin") -> None:
        old = self.get(profile_id).config
        self._db.execute("UPDATE profiles SET config_json = ? WHERE id = ?", (config.model_dump_json(), profile_id))
        self._audit.record(
            "profile.config", actor=actor, target=profile_id, old=old.model_dump(), new=config.model_dump()
        )

    @staticmethod
    def _row(r: Any) -> Profile:
        return Profile(
            id=r["id"],
            site_id=r["site_id"],
            account=r["account"],
            config=ProfileConfig.model_validate_json(r["config_json"]),
            status=r["status"],
            fingerprint=json.loads(r["fingerprint_json"]) if r["fingerprint_json"] else None,
            created_at=r["created_at"],
            last_used_at=r["last_used_at"],
        )

    # ---- paths & locks --------------------------------------------------

    def _base(self, profile: Profile) -> Path:
        path = (self.root / profile.site_id / profile.account).resolve()
        if self.root not in path.parents:
            raise ValueError("profile path escapes profiles dir")
        return path

    def user_data_dir(self, profile: Profile) -> Path:
        return self._base(profile) / "chrome"

    def downloads_dir(self, profile: Profile) -> Path:
        return self._base(profile) / "downloads"

    def _lock(self, profile: Profile) -> _FileLock:
        base = self._base(profile)
        base.mkdir(parents=True, exist_ok=True, mode=0o700)
        return _FileLock(base / ".lock")

    def is_busy(self, profile_id: str) -> bool:
        lock = self._lock(self.get(profile_id))
        if lock.acquire():
            lock.release()
            return False
        return True

    def check_volume(self) -> list[str]:
        if self.settings.volume_encrypted_assert:
            return []
        self.root.mkdir(parents=True, exist_ok=True, mode=0o700)
        encrypted, detail = detect_encrypted(self.root)
        if encrypted:
            return []
        msg = f"profiles dir {self.root} is not confirmed encrypted ({detail})"
        if self.settings.volume_check == "require":
            raise VolumeNotEncrypted(
                msg + ". Move it to an encrypted volume, or set FRIDAY_VOLUME_ENCRYPTED_ASSERT=true if it is."
            )
        log.warning(msg)
        return [msg]

    # ---- open / close ---------------------------------------------------

    def _proxy(self, profile: Profile) -> ProxySettings | None:
        p = profile.config.proxy
        if p is None:
            return None
        for name in (p.username_secret, p.password_secret):
            if name and not name.startswith(profile.secret_prefix):
                raise ConfigError(f"proxy secret {name} must live under {profile.secret_prefix}")
        purpose = f"proxy:{profile.id}"
        return ProxySettings(
            server=p.server,
            username=self._vault.reveal(p.username_secret, purpose=purpose) if p.username_secret else None,
            password=self._vault.reveal(p.password_secret, purpose=purpose) if p.password_secret else None,
            bypass=p.bypass,
        )

    @asynccontextmanager
    async def open(
        self,
        profile_id: str,
        *,
        purpose: str,
        headless: bool = False,
        driver: str | None = None,
        with_vnc: bool = True,
        actor: str = "system",
        proxy_mode: str = "profile",  # profile | none | alt (diagnosis experiments)
        channel: str | None = None,  # e.g. "chromium" for the bundled-Chromium experiment
        fresh: bool = False,  # throwaway user_data_dir (diagnosis experiment), deleted afterwards
    ) -> AsyncIterator[OpenProfile]:
        profile = self.get(profile_id)
        site = self._sites.get(profile.site_id)
        assert site is not None
        warnings = self.check_volume()
        lock = self._lock(profile)
        if not lock.acquire():
            raise ProfileBusy(f"profile {profile_id} is already open")
        display: Display | None = None
        session: BrowserSession | None = None
        tmp: Path | None = Path(tempfile.mkdtemp(prefix="friday-fresh-")) if fresh else None
        try:
            driver_name = driver or profile.config.driver or self.settings.driver
            drv = get_driver(driver_name)
            display = await self._displays.acquire(with_vnc=with_vnc and not headless)
            udd = tmp / "chrome" if tmp else self.user_data_dir(profile)
            dl = self.downloads_dir(profile)
            if proxy_mode == "none":
                proxy = None
            elif proxy_mode == "alt":
                if not self.settings.diag_alt_proxy:
                    raise ConfigError("FRIDAY_DIAG_ALT_PROXY is not set")
                proxy = ProxySettings(server=self.settings.diag_alt_proxy)
            else:
                proxy = self._proxy(profile)
            udd.mkdir(parents=True, exist_ok=True, mode=0o700)
            dl.mkdir(parents=True, exist_ok=True, mode=0o700)
            session = await drv.launch(
                LaunchRequest(
                    user_data_dir=udd,
                    downloads_dir=dl,
                    locale=profile.config.locale,
                    timezone=profile.config.timezone,
                    window_width=profile.config.window_width,
                    window_height=profile.config.window_height,
                    display_env=display.env,
                    headless=headless,
                    proxy=proxy,
                    channel=channel or self.settings.chrome_channel,
                    executable=None if channel else self.settings.chrome_executable,  # an experiment's channel wins
                    sandbox=self.settings.chrome_sandbox,
                )
            )
            self._audit.record(
                "profile.open",
                actor=actor,
                target=profile.id,
                purpose=purpose,
                driver=driver_name,
                stealth=session.stealth,
                headless=headless,
                sandbox=self.settings.chrome_sandbox,
                display=display.kind,
                proxy=proxy_mode if proxy_mode != "profile" else bool(profile.config.proxy),
                fresh_profile=fresh,
                channel=channel or self.settings.chrome_channel,
                browser_version=session.browser_version,
            )
            if session.stealth:
                log.warning("profile %s opened with stealth driver %s", profile.id, driver_name)
            if not self.settings.chrome_sandbox:
                log.warning("profile %s opened with Chrome's sandbox disabled", profile.id)
            if not headless and not fresh and channel is None and proxy_mode == "profile":
                # experiments must not overwrite the pinned identity
                warnings += await self._check_fingerprint(profile, session)
            self._db.execute("UPDATE profiles SET last_used_at = ? WHERE id = ?", (iso(), profile.id))
            yield OpenProfile(profile, site, session, display, warnings)
        finally:
            try:
                if session is not None:
                    await session.close()
            finally:
                if display is not None:
                    await self._displays.release(display)
                if tmp is not None:
                    shutil.rmtree(tmp, ignore_errors=True)
                lock.release()
                self._audit.record("profile.close", actor=actor, target=profile.id, purpose=purpose)

    async def _check_fingerprint(self, profile: Profile, session: BrowserSession) -> list[str]:
        page = await session.page()
        try:
            observed: dict[str, Any] = await page.evaluate(FINGERPRINT_JS)
        except Exception as e:  # never block a run on this
            return [f"fingerprint probe failed: {e}"]
        observed["browserVersion"] = session.browser_version
        observed["driver"] = session.driver
        warnings: list[str] = []
        cfg = profile.config
        if cfg.timezone and not same_timezone(observed.get("timezone"), cfg.timezone):
            warnings.append(f"timezone is {observed.get('timezone')}, configured {cfg.timezone}")
        if observed.get("language") and not str(observed["language"]).startswith(cfg.locale.split("-")[0]):
            warnings.append(f"language is {observed.get('language')}, configured {cfg.locale}")
        pinned = profile.fingerprint
        if pinned is None:
            self._db.execute(
                "UPDATE profiles SET fingerprint_json = ? WHERE id = ?", (json.dumps(observed), profile.id)
            )
            self._audit.record("profile.fingerprint_pinned", target=profile.id, fingerprint=observed)
        else:
            drift = {
                k: {"pinned": pinned.get(k), "now": observed.get(k)}
                for k in observed
                if k not in VOLATILE_FINGERPRINT
                and pinned.get(k) != observed.get(k)
                and not (k == "timezone" and same_timezone(pinned.get(k), observed.get(k)))
            }
            if drift:
                warnings.append(f"browser identity drifted: {sorted(drift)}")
                self._audit.record("profile.fingerprint_drift", target=profile.id, drift=drift)
        for w in warnings:
            log.warning("profile %s: %s", profile.id, w)
        return warnings

    # ---- revoke ---------------------------------------------------------

    def wipe(self, profile_id: str, *, actor: str = "admin") -> None:
        """Revoke everything: delete the Chrome profile, downloads, and the profile's secrets."""
        profile = self.get(profile_id)
        lock = self._lock(profile)
        if not lock.acquire():
            raise ProfileBusy(f"profile {profile_id} is open; stop it before wiping")
        try:
            base = self._base(profile)
            for child in base.iterdir():
                if child.name != ".lock":
                    shutil.rmtree(child) if child.is_dir() else child.unlink()
            n = self._vault.delete_prefix(profile.secret_prefix, actor=actor)
            self._db.execute("DELETE FROM profiles WHERE id = ?", (profile_id,))
            self._audit.record("profile.wipe", actor=actor, target=profile_id, secrets_deleted=n)
        finally:
            lock.release()
            (self._base(profile) / ".lock").unlink(missing_ok=True)
