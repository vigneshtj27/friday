"""Profile doctor: does the pinned browser identity still match the profile's config?

A pin records what the site has seen (driver, navigator.webdriver, language, timezone) and the
config it was taken under. If the config has moved on since (driver switched to patchright, a
locale fix, a proxy added), or the pin itself disagrees with the config (the live `en-GB` vs
configured `en-IN` drift), the pin is stale: re-pinning clears it so the next open records a fresh
one under the current config.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass
class DoctorReport:
    profile_id: str
    pinned: bool
    issues: list[str] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)
    stale: bool = False  # a re-pin fixes it (the pin predates the current config)

    @property
    def needs_repin(self) -> bool:
        return self.stale

    def lines(self) -> list[str]:
        if not self.pinned:
            return [f"{self.profile_id}: no identity pinned yet; it will be pinned under the current config on the next open."]
        if not self.issues:
            return [f"{self.profile_id}: pinned identity matches the config.", *self.notes]
        return [f"{self.profile_id}: pinned identity is stale:", *(f"  - {i}" for i in self.issues), *self.notes]


def examine(profiles: Any, profile: Any) -> DoctorReport:
    fp = profile.fingerprint
    rep = DoctorReport(profile.id, pinned=fp is not None)
    if fp is None:
        return rep
    cfg = profile.config
    want = profiles.identity_config(cfg)
    pinned_for = fp.get("pinned_for")
    if pinned_for is None:
        rep.stale = True
        rep.issues.append("pinned by an older friday that didn't record the config it was pinned under")
    else:
        changed = [k for k in want if pinned_for.get(k) != want[k]]
        if changed:
            rep.stale = True
            rep.issues.append("config changed since the pin: " + ", ".join(f"{k} {pinned_for.get(k)!r} -> {want[k]!r}" for k in changed))
    if fp.get("driver") and fp["driver"] != want["driver"]:
        rep.stale = True
        rep.issues.append(f"pinned with driver {fp['driver']}, configured {want['driver']}")
    if fp.get("webdriver") is True and want["driver"] == "patchright":
        rep.stale = rep.stale or pinned_for is None or pinned_for.get("driver") != "patchright"
        rep.issues.append("pinned with navigator.webdriver = true, but the profile uses patchright")
    n_stale = len(rep.issues)
    lang = str(fp.get("language") or "")
    if lang and lang.lower() != cfg.locale.lower():
        rep.issues.append(f"pinned language {lang}, configured locale {cfg.locale}")
    langs = fp.get("languages") or []
    if langs and str(langs[0]).lower() != cfg.locale.lower():
        rep.issues.append(f"pinned languages {langs}, configured locale {cfg.locale}")
    if cfg.timezone and fp.get("timezone"):
        from friday.profiles.manager import same_timezone

        if not same_timezone(fp["timezone"], cfg.timezone):
            rep.issues.append(f"pinned timezone {fp['timezone']}, configured {cfg.timezone}")
    if not rep.stale and len(rep.issues) > n_stale:
        rep.notes.append("  a fresh pin under this config still disagrees: the browser isn't applying the config (check the driver)")
    if fp.get("webdriver") is True and want["driver"] != "patchright":
        rep.notes.append("  note: navigator.webdriver is true with this driver; sites can see the automation flag (see preflight)")
    return rep


def check_all(ctx: Any, *, repin: bool) -> list[DoctorReport]:
    """Doctor every profile; with repin, clear stale pins (used at startup)."""
    out = []
    for p in ctx.profiles.list():
        rep = examine(ctx.profiles, p)
        if repin and rep.needs_repin:
            ctx.profiles.repin(p.id, actor="system", reason="; ".join(rep.issues)[:500])
        out.append(rep)
    return out
