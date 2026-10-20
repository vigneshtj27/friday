"""Reminders and recurring jobs, inside the app's own event loop (a small loop over `schedules`).

  reminder - posts the text into the chat when due
  job      - runs the owner's stored request through the normal planner, with the same tools and
             the same approval cards as if the owner had typed it

Schedules are created only from the owner's own words (the schedule tool needs `owner_quote`), so a
web page or an email can't schedule anything. A schedule that came due while friday was down fires
once when it's back, not once per missed slot.

When: {"in_minutes": n} | {"at": "YYYY-MM-DDTHH:MM"} (owner's timezone) for one-shot;
{"every": "minutes"|"hours"|"days"|"weeks", "n": 1, "time": "HH:MM", "weekdays": [0-6, Monday=0]} for recurring.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import secrets
from datetime import datetime, timedelta, timezone
from typing import Any
from zoneinfo import ZoneInfo

from friday.util import iso

log = logging.getLogger(__name__)
WEEKDAYS = ["monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday"]


class ScheduleError(ValueError):
    pass


def _utc(dt: datetime) -> datetime:
    return dt.astimezone(timezone.utc)


def normalise(when: dict[str, Any]) -> dict[str, Any]:
    """Validate a schedule spec; returns the canonical form."""
    keys = set(when)
    if keys & {"in_minutes", "at"}:
        if "in_minutes" in when:
            n = when["in_minutes"]
            if not isinstance(n, int | float) or not 0 < n <= 60 * 24 * 366:
                raise ScheduleError("in_minutes must be between 1 minute and a year")
            return {"in_minutes": n}
        try:
            datetime.fromisoformat(str(when["at"]))
        except ValueError as e:
            raise ScheduleError("at must look like 2026-10-21T18:00") from e
        return {"at": str(when["at"])}
    every = when.get("every")
    if every not in ("minutes", "hours", "days", "weeks"):
        raise ScheduleError("give in_minutes, at, or every (minutes|hours|days|weeks)")
    n = int(when.get("n") or 1)
    if every == "minutes" and n < 15:
        raise ScheduleError("recurring jobs run at most every 15 minutes")
    if n < 1 or n > 366:
        raise ScheduleError("n must be 1..366")
    out: dict[str, Any] = {"every": every, "n": n}
    if every in ("days", "weeks"):
        t = str(when.get("time") or "09:00")
        try:
            hh, mm = (int(x) for x in t.split(":"))
            assert 0 <= hh < 24 and 0 <= mm < 60
        except (ValueError, AssertionError) as e:
            raise ScheduleError("time must be HH:MM") from e
        out["time"] = f"{hh:02d}:{mm:02d}"
    if every == "weeks":
        days = when.get("weekdays") or [0]
        if not all(isinstance(d, int) and 0 <= d <= 6 for d in days):
            raise ScheduleError("weekdays are 0 (Monday) to 6 (Sunday)")
        out["weekdays"] = sorted(set(days))
    return out


def next_run(spec: dict[str, Any], tz: str, *, after: datetime, created: datetime | None = None) -> datetime | None:
    """Next time (UTC) strictly after `after`, or None for a one-shot already past."""
    z = ZoneInfo(tz)
    if "in_minutes" in spec:
        due = (created or after) + timedelta(minutes=spec["in_minutes"])
        return due if created is not None else None
    if "at" in spec:
        at = datetime.fromisoformat(spec["at"])
        at = at.replace(tzinfo=z) if at.tzinfo is None else at
        return _utc(at) if created is not None else None
    every, n = spec["every"], spec["n"]
    if every in ("minutes", "hours"):
        step = timedelta(minutes=n) if every == "minutes" else timedelta(hours=n)
        return after + step
    hh, mm = (int(x) for x in spec["time"].split(":"))
    local = after.astimezone(z)
    cand = local.replace(hour=hh, minute=mm, second=0, microsecond=0)
    if cand <= local:
        cand += timedelta(days=1)
    if every == "days":
        return _utc(cand) if n == 1 else _utc(cand + timedelta(days=n - 1))
    for _ in range(8):
        if cand.weekday() in spec["weekdays"]:
            return _utc(cand)
        cand += timedelta(days=1)
    return None


def describe(spec: dict[str, Any]) -> str:
    if "in_minutes" in spec:
        return f"once, {spec['in_minutes']} min after it was set"
    if "at" in spec:
        return f"once at {spec['at'].replace('T', ' ')}"
    if spec["every"] in ("minutes", "hours"):
        return f"every {spec['n']} {spec['every']}"
    if spec["every"] == "days":
        return f"every {'day' if spec['n'] == 1 else str(spec['n']) + ' days'} at {spec['time']}"
    days = ", ".join(WEEKDAYS[d].capitalize() for d in spec["weekdays"])
    return f"every {days} at {spec['time']}"


class Scheduler:
    def __init__(self, ctx: Any, *, poll_seconds: float = 15.0):
        self.ctx = ctx
        self.poll = poll_seconds
        self._task: asyncio.Task[None] | None = None
        self._jobs: set[asyncio.Task[None]] = set()

    @property
    def tz(self) -> str:
        return self.ctx.settings.default_timezone or "UTC"

    def create(self, kind: str, text: str, when: dict[str, Any], *, now: datetime | None = None) -> dict[str, Any]:
        if kind not in ("reminder", "job"):
            raise ScheduleError("kind is reminder or job")
        text = " ".join(text.split())[:1000]
        if not text:
            raise ScheduleError("what should it say or do?")
        spec = normalise(when)
        now = now or datetime.now(timezone.utc)
        due = next_run(spec, self.tz, after=now, created=now)
        if due is None or due <= now - timedelta(seconds=5):
            raise ScheduleError("that time is in the past")
        sid = secrets.token_hex(4)
        self.ctx.db.execute(
            "INSERT INTO schedules (id, kind, text, spec_json, tz, next_run_at, created_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
            (sid, kind, self.ctx.redactor.redact(text), json.dumps(spec), self.tz, iso(due), iso(now)),
        )
        self.ctx.audit.record("schedule.create", actor="owner", target=sid, kind=kind, when=describe(spec))
        return self.get(sid)  # type: ignore[return-value]

    def get(self, sid: str) -> dict[str, Any] | None:
        r = self.ctx.db.query_one("SELECT * FROM schedules WHERE id = ?", (sid,))
        return self._public(dict(r)) if r else None

    def _public(self, r: dict[str, Any]) -> dict[str, Any]:
        spec = json.loads(r["spec_json"])
        nxt = r["next_run_at"]
        local = datetime.fromisoformat(nxt).astimezone(ZoneInfo(r["tz"])).strftime("%a %d %b %H:%M") if nxt else None
        return {"id": r["id"], "kind": r["kind"], "text": r["text"], "when": describe(spec), "next": local,
                "runs": r["runs"], "active": r["cancelled_at"] is None and nxt is not None}  # fmt: skip

    def list(self, *, include_done: bool = False) -> list[dict[str, Any]]:
        rows = self.ctx.db.query("SELECT * FROM schedules ORDER BY next_run_at IS NULL, next_run_at")
        out = [self._public(dict(r)) for r in rows]
        return out if include_done else [s for s in out if s["active"]]

    def cancel(self, query: str) -> list[dict[str, Any]]:
        """Cancel by id, or every active schedule whose text contains the words."""
        active = self.list()
        q = query.strip().lower()
        hits = [s for s in active if s["id"] == q] or [s for s in active if q and all(w in s["text"].lower() for w in q.split())]
        for s in hits:
            self.ctx.db.execute("UPDATE schedules SET cancelled_at = ?, next_run_at = NULL WHERE id = ?", (iso(), s["id"]))
            self.ctx.audit.record("schedule.cancel", actor="owner", target=s["id"])
        return hits

    # ---- firing ---------------------------------------------------------------------------

    async def tick(self, now: datetime | None = None) -> list[str]:
        """Fire everything due. Returns the ids fired."""
        now = now or datetime.now(timezone.utc)
        due = self.ctx.db.query("SELECT * FROM schedules WHERE cancelled_at IS NULL AND next_run_at IS NOT NULL AND next_run_at <= ?",
                                (iso(now),))  # fmt: skip
        fired = []
        for r in due:
            spec = json.loads(r["spec_json"])
            nxt = next_run(spec, r["tz"], after=now)  # one-shot -> None; a recurring one skips missed slots
            self.ctx.db.execute("UPDATE schedules SET next_run_at = ?, last_run_at = ?, runs = runs + 1 WHERE id = ?",
                                (iso(nxt) if nxt else None, iso(now), r["id"]))  # fmt: skip
            fired.append(r["id"])
            if r["kind"] == "reminder":
                await self.ctx.notifier.send(f"Reminder: {r['text']}", None)
            else:
                task = asyncio.create_task(self._run_job(r["id"], r["text"]))
                self._jobs.add(task)
                task.add_done_callback(self._jobs.discard)
        return fired

    async def _run_job(self, sid: str, request: str) -> None:
        chat = self.ctx.chat
        await self.ctx.notifier.send(f"Scheduled job: {request}", None)
        if chat.assistant is None:
            chat.store.add("friday", "I couldn't run that: the model isn't configured.")
            return
        try:
            reply = await chat.assistant.reply(request, origin=f"schedule:{sid}")
        except Exception as e:
            log.warning("scheduled job %s failed: %s", sid, type(e).__name__)
            reply = f"The scheduled job didn't run: {type(e).__name__}."
        chat.store.add("friday", reply)

    async def _loop(self) -> None:
        while True:
            try:
                await self.tick()
            except asyncio.CancelledError:
                raise
            except Exception:
                log.exception("scheduler tick failed")
            await asyncio.sleep(self.poll)

    def start(self) -> None:
        if self._task is None:
            self._task = asyncio.create_task(self._loop())

    async def stop(self) -> None:
        for t in [self._task, *self._jobs]:
            if t:
                t.cancel()
                with contextlib.suppress(asyncio.CancelledError, Exception):
                    await t
        self._task = None
