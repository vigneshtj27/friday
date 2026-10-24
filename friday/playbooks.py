"""Playbooks: owner-approved, versioned instructions for a repeatable task.

A playbook is free-text body (what friday should do) plus a small header that can only *narrow* what
a run is allowed to do:

  * allowed_domains   - a run may touch these and only these (intersected with the site's own
                        domains at run time, so a playbook can never add a domain the account
                        doesn't already use);
  * allowed_effects   - the effect classes a run may use (read/write/send/share/spend); a tool whose
                        effect is not listed is denied even if it would normally be allowed;
  * required_approvals- effect classes that must get an owner tap even inside this playbook;
  * success_check     - a plain-language check the owner can read; a run that fails it rolls the
                        playbook back to its last-good version.

Creating or editing a playbook is a deliberate owner action (the tool needs the owner's own words
AND a tap on an approval card); a web page or an email can never create or change one. Every save
writes a new version and keeps the whole history, so a change can be shown as a diff and rolled back.

Everything stored here is redacted first.
"""

from __future__ import annotations

import difflib
import json
import logging
import re
from typing import Any

from friday.util import iso

log = logging.getLogger(__name__)

EFFECT_CLASSES = ("read", "write", "send", "share", "spend", "publish")


def narrow_domains(allowed: list[str], site_domains: list[str]) -> list[str]:
    """A playbook may only touch domains the account already uses: the intersection, never more."""
    site = {d.lower().lstrip(".") for d in site_domains}
    keep = []
    for d in allowed:
        dl = d.lower().lstrip(".")
        if any(dl == s or dl.endswith("." + s) or s.endswith("." + dl) for s in site):
            keep.append(d)
    return keep or list(site_domains)  # an empty header means "the site's own domains"


def confine_domains(allowed: list[str], site_domains: list[str]) -> list[str]:
    """STRICT intersection for enforcement (unlike narrow_domains, no fallback to the full site): an
    empty header means no narrowing (the site's own domains); a non-empty header that matches none of
    the site's domains returns [], and the caller must refuse the run rather than silently widen it."""
    if not allowed:
        return list(site_domains)
    site = {d.lower().lstrip(".") for d in site_domains}
    return [d for d in allowed
            if any((dl := d.lower().lstrip(".")) == s or dl.endswith("." + s) or s.endswith("." + dl) for s in site)]  # fmt: skip


def narrow_effects(allowed: list[str]) -> set[str]:
    """Reads are always permitted; everything else must be listed. Unknown names are dropped."""
    out = {e for e in allowed if e in EFFECT_CLASSES}
    out.add("read")
    return out


class PlaybookStore:
    def __init__(self, ctx: Any):
        self.ctx = ctx

    # ---- validation --------------------------------------------------------------------------

    def _clean_header(self, header: dict[str, Any]) -> dict[str, Any]:
        domains = [str(d).strip() for d in (header.get("allowed_domains") or []) if str(d).strip()]
        effects = [str(e).strip().lower() for e in (header.get("allowed_effects") or ["read"])]
        effects = [e for e in effects if e in EFFECT_CLASSES] or ["read"]
        approvals = [str(a).strip().lower() for a in (header.get("required_approvals") or []) if str(a).strip().lower() in EFFECT_CLASSES]
        return {"allowed_domains": domains, "allowed_effects": sorted(set(effects)),
                "required_approvals": sorted(set(approvals))}  # fmt: skip

    def propose(self, name: str, goal: str, body: str, header: dict[str, Any] | None = None,
                success_check: str | None = None, *, source_run: str | None = None) -> dict[str, Any]:
        """Build a draft playbook (not stored) for the owner to review before approving."""
        h = self._clean_header(header or {})
        return {"name": name.strip(), "goal": goal.strip(), "body": body.strip(),
                "success_check": (success_check or "").strip() or None, "source_run": source_run, **h}  # fmt: skip

    # ---- persistence -------------------------------------------------------------------------

    def save(self, name: str, goal: str, body: str, header: dict[str, Any] | None = None,
             success_check: str | None = None, *, source_run: str | None = None) -> dict[str, Any]:
        """Create a playbook, or add a new version to an existing one of the same name."""
        red = self.ctx.redactor.redact
        name = red(name.strip())[:120]
        goal, body = red(goal.strip())[:2000], red(body.strip())[:8000]
        sc = red(success_check.strip())[:500] if success_check else None
        h = self._clean_header(header or {})
        now = iso()
        existing = self.get(name)
        if existing is None:
            pid = re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-")[:48] or "pb"
            # keep the id unique
            n, base = 1, pid
            while self.ctx.db.query_one("SELECT 1 FROM playbooks WHERE id = ?", (pid,)):
                n += 1
                pid = f"{base}-{n}"
            self.ctx.db.execute(
                "INSERT INTO playbooks (id, name, goal, body, allowed_domains_json, allowed_effects_json,"
                " required_approvals_json, success_check, version, last_good_version, status, source_run, created_at, updated_at)"
                " VALUES (?, ?, ?, ?, ?, ?, ?, ?, 1, 1, 'active', ?, ?, ?)",
                (pid, name, goal, body, json.dumps(h["allowed_domains"]), json.dumps(h["allowed_effects"]),
                 json.dumps(h["required_approvals"]), sc, source_run, now, now),
            )  # fmt: skip
            version = 1
        else:
            pid = existing["id"]
            version = existing["version"] + 1
            self.ctx.db.execute(
                "UPDATE playbooks SET goal = ?, body = ?, allowed_domains_json = ?, allowed_effects_json = ?,"
                " required_approvals_json = ?, success_check = ?, version = ?, updated_at = ? WHERE id = ?",
                (goal, body, json.dumps(h["allowed_domains"]), json.dumps(h["allowed_effects"]),
                 json.dumps(h["required_approvals"]), sc, version, now, pid),
            )  # fmt: skip
        self.ctx.db.execute(
            "INSERT INTO playbook_versions (playbook_id, version, goal, body, header_json, success_check, created_at)"
            " VALUES (?, ?, ?, ?, ?, ?, ?)",
            (pid, version, goal, body, json.dumps(h), sc, now),
        )
        self.ctx.audit.record("playbook.save", actor="owner", target=pid, version=version)
        return self.get_by_id(pid)  # type: ignore[return-value]

    # ---- reads -------------------------------------------------------------------------------

    def get_by_id(self, pid: str) -> dict[str, Any] | None:
        r = self.ctx.db.query_one("SELECT * FROM playbooks WHERE id = ?", (pid,))
        return self._public(dict(r)) if r else None

    def get(self, query: str) -> dict[str, Any] | None:
        q = query.strip()
        r = self.ctx.db.query_one("SELECT * FROM playbooks WHERE id = ? OR name = ?", (q, q))
        if r:
            return self._public(dict(r))
        cands = [p for p in self.list() if q and all(w in p["name"].lower() for w in q.lower().split())]
        return cands[0] if len(cands) == 1 else None

    def list(self, *, include_archived: bool = False) -> list[dict[str, Any]]:
        where = "" if include_archived else "WHERE status = 'active'"
        rows = self.ctx.db.query(f"SELECT * FROM playbooks {where} ORDER BY updated_at DESC")
        return [self._public(dict(r)) for r in rows]

    def versions(self, pid: str) -> list[dict[str, Any]]:
        rows = self.ctx.db.query("SELECT * FROM playbook_versions WHERE playbook_id = ? ORDER BY version", (pid,))
        return [dict(r) for r in rows]

    def diff(self, pid: str, a: int | None = None, b: int | None = None) -> str:
        vs = self.versions(pid)
        if len(vs) < 2 and b is None:
            return ""  # nothing to compare yet
        bv = b or vs[-1]["version"]
        av = a or (vs[-2]["version"] if len(vs) >= 2 else vs[-1]["version"])
        va = next(v for v in vs if v["version"] == av)
        vb = next(v for v in vs if v["version"] == bv)
        out = difflib.unified_diff(
            (va["body"] + "\n").splitlines(keepends=True), (vb["body"] + "\n").splitlines(keepends=True),
            fromfile=f"v{av}", tofile=f"v{bv}",
        )  # fmt: skip
        return "".join(out)

    @staticmethod
    def _public(r: dict[str, Any]) -> dict[str, Any]:
        return {"id": r["id"], "name": r["name"], "goal": r["goal"], "body": r["body"],
                "allowed_domains": json.loads(r["allowed_domains_json"]),
                "allowed_effects": json.loads(r["allowed_effects_json"]),
                "required_approvals": json.loads(r["required_approvals_json"]),
                "success_check": r["success_check"], "version": r["version"],
                "last_good_version": r["last_good_version"], "status": r["status"],
                "updated_at": r["updated_at"]}  # fmt: skip

    # ---- last-good and rollback --------------------------------------------------------------

    def mark_good(self, pid: str, version: int | None = None) -> None:
        pb = self.get_by_id(pid)
        if pb is None:
            return
        self.ctx.db.execute("UPDATE playbooks SET last_good_version = ?, updated_at = ? WHERE id = ?",
                            (version or pb["version"], iso(), pid))  # fmt: skip

    def rollback(self, pid: str) -> dict[str, Any] | None:
        """Restore the last-good version's body/header as the current one (a new version on top)."""
        pb = self.get_by_id(pid)
        if pb is None or not pb["last_good_version"] or pb["last_good_version"] == pb["version"]:
            return pb
        good = next((v for v in self.versions(pid) if v["version"] == pb["last_good_version"]), None)
        if good is None:
            return pb
        header = json.loads(good["header_json"])
        restored = self.save(pb["name"], good["goal"], good["body"], header, good["success_check"])
        # the restored version is, by definition, good again
        self.mark_good(pid, restored["version"])
        self.ctx.audit.record("playbook.rollback", actor="system", target=pid, to_version=pb["last_good_version"])
        return self.get_by_id(pid)

    def note_run_outcome(self, pid: str, ok: bool) -> dict[str, Any] | None:
        """After a run: a success marks this version good; a failure of a not-yet-good version rolls
        back to the last-good one (the spec's "roll back on a failed success check")."""
        pb = self.get_by_id(pid)
        if pb is None:
            return None
        if ok:
            self.mark_good(pid, pb["version"])
            return self.get_by_id(pid)
        if pb["version"] != pb["last_good_version"]:
            return self.rollback(pid)
        return pb

    def archive(self, pid: str) -> None:
        self.ctx.db.execute("UPDATE playbooks SET status = 'archived', updated_at = ? WHERE id = ?", (iso(), pid))
        self.ctx.audit.record("playbook.archive", actor="owner", target=pid)

    # ---- run constraints ---------------------------------------------------------------------

    def run_constraints(self, pb: dict[str, Any], site_domains: list[str]) -> dict[str, Any]:
        """The narrowed domains and effects a run of this playbook may use (never wider than either
        the playbook header or the account's own domains)."""
        return {
            "allowed_domains": narrow_domains(pb["allowed_domains"], site_domains),
            "allowed_effects": sorted(narrow_effects(pb["allowed_effects"])),
            "required_approvals": pb["required_approvals"],
        }
