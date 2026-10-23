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


def is_one_shot(spec: dict[str, Any]) -> bool:
    return "in_minutes" in spec or "at" in spec


def one_shot_due(spec: dict[str, Any], tz: str, created: datetime) -> datetime:
    """The single fixed UTC moment a one-shot was set for (never re-based)."""
    if "in_minutes" in spec:
        return created + timedelta(minutes=spec["in_minutes"])
    z = ZoneInfo(tz)
    at = datetime.fromisoformat(spec["at"])
    return _utc(at.replace(tzinfo=z) if at.tzinfo is None else at)


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
        self._pending_watch: dict[str, dict[str, Any]] = {}  # sid -> watch_report staged this run
        self._outcomes: dict[str, dict[str, Any]] = {}  # sid -> planner outcome this run
        self._locks: dict[str, asyncio.Lock] = {}  # per-schedule: serialize same-schedule runs

    @property
    def tz(self) -> str:
        return self.ctx.settings.default_timezone or "UTC"

    def create(self, kind: str, text: str, when: dict[str, Any], *, notify: str = "always",
               now: datetime | None = None) -> dict[str, Any]:  # fmt: skip
        if kind not in ("reminder", "job"):
            raise ScheduleError("kind is reminder or job")
        if notify not in ("always", "on_change"):
            raise ScheduleError("notify is always or on_change")
        text = " ".join(text.split())[:1000]
        if not text:
            raise ScheduleError("what should it say or do?")
        spec = normalise(when)
        now = now or datetime.now(timezone.utc)
        due = next_run(spec, self.tz, after=now, created=now)
        if due is None or due <= now - timedelta(seconds=5):
            raise ScheduleError("that time is in the past")
        # A watch (on_change) only makes sense for a recurring job that checks something repeatedly.
        if notify == "on_change" and (kind != "job" or not spec.get("every")):
            raise ScheduleError("on_change is only for a recurring job (every ...)")
        sid = secrets.token_hex(4)
        self.ctx.db.execute(
            "INSERT INTO schedules (id, kind, text, spec_json, tz, next_run_at, created_at, notify) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (sid, kind, self.ctx.redactor.redact(text), json.dumps(spec), self.tz, iso(due), iso(now), notify),
        )
        self.ctx.audit.record("schedule.create", actor="owner", target=sid, kind=kind, when=describe(spec), notify=notify)
        return self.get(sid)  # type: ignore[return-value]

    def get(self, sid: str) -> dict[str, Any] | None:
        r = self.ctx.db.query_one("SELECT * FROM schedules WHERE id = ?", (sid,))
        return self._public(dict(r)) if r else None

    def _fmt_local(self, ts: str | None, tz: str) -> str | None:
        return datetime.fromisoformat(ts).astimezone(ZoneInfo(tz)).strftime("%a %d %b %H:%M") if ts else None

    def _public(self, r: dict[str, Any]) -> dict[str, Any]:
        spec = json.loads(r["spec_json"])
        nxt = r["next_run_at"]
        paused = r["paused_at"] is not None and r["cancelled_at"] is None
        out = {"id": r["id"], "kind": r["kind"], "text": r["text"], "when": describe(spec),
               "next": None if paused else self._fmt_local(nxt, r["tz"]), "runs": r["runs"],
               "notify": r.get("notify", "always"), "paused": paused,
               "active": r["cancelled_at"] is None and nxt is not None and not paused}  # fmt: skip
        if r.get("notify") == "on_change":
            st = self.ctx.db.query_one("SELECT * FROM schedule_state WHERE schedule_id = ?", (r["id"],))
            out["last_checked"] = self._fmt_local(st["last_checked_at"], r["tz"]) if st else None
            out["last_changed"] = self._fmt_local(st["last_changed_at"], r["tz"]) if st else None
        return out

    def list(self, *, include_done: bool = False) -> list[dict[str, Any]]:
        rows = self.ctx.db.query("SELECT * FROM schedules ORDER BY next_run_at IS NULL, next_run_at")
        out = [self._public(dict(r)) for r in rows]
        return out if include_done else [s for s in out if s["active"] or s["paused"]]

    def _match(self, query: str, candidates: list[dict[str, Any]]) -> list[dict[str, Any]]:
        q = query.strip().lower()
        return [s for s in candidates if s["id"] == q] or [s for s in candidates if q and all(w in s["text"].lower() for w in q.split())]

    def cancel(self, query: str) -> list[dict[str, Any]]:
        """Cancel by id, or every active/paused schedule whose text contains the words."""
        hits = self._match(query, self.list())
        for s in hits:
            self.ctx.db.execute("UPDATE schedules SET cancelled_at = ?, next_run_at = NULL WHERE id = ?", (iso(), s["id"]))
            self.ctx.audit.record("schedule.cancel", actor="owner", target=s["id"])
        return hits

    def pause(self, query: str) -> list[dict[str, Any]]:
        """Stop future fires but keep the spec. A mid-run job already firing is not interrupted."""
        hits = self._match(query, [s for s in self.list() if not s["paused"]])
        for s in hits:
            self.ctx.db.execute("UPDATE schedules SET paused_at = ? WHERE id = ? AND cancelled_at IS NULL", (iso(), s["id"]))
            self.ctx.audit.record("schedule.pause", actor="owner", target=s["id"])
        return [self.get(s["id"]) for s in hits]  # type: ignore[misc]

    def resume(self, query: str, *, now: datetime | None = None) -> list[dict[str, Any]]:
        """Resume a paused schedule. A recurring job's next run is recomputed from now (no missed
        slots back-fire). A one-shot keeps its original fixed time: if that time is still in the
        future it re-arms to it; if it has already passed, it is marked expired and NOT fired - the
        owner is told to set a new time. A dated commitment is never silently moved or back-fired.
        """
        now = now or datetime.now(timezone.utc)
        hits = self._match(query, [s for s in self.list() if s["paused"]])
        out = []
        for s in hits:
            r = self.ctx.db.query_one("SELECT * FROM schedules WHERE id = ?", (s["id"],))
            spec = json.loads(r["spec_json"])
            expired = False
            if is_one_shot(spec):
                due = one_shot_due(spec, r["tz"], datetime.fromisoformat(r["created_at"]))
                nxt = due if due > now else None
                expired = nxt is None
            else:
                nxt = next_run(spec, r["tz"], after=now)
            # An expired one-shot stays un-scheduled (next_run_at NULL) and un-paused: it will not fire.
            self.ctx.db.execute("UPDATE schedules SET paused_at = NULL, next_run_at = ? WHERE id = ?",
                                (iso(nxt) if nxt else None, s["id"]))  # fmt: skip
            self.ctx.audit.record("schedule.resume", actor="owner", target=s["id"], expired=expired)
            pub = self.get(s["id"])
            pub["expired"] = expired  # type: ignore[index]
            out.append(pub)
        return out  # type: ignore[return-value]

    def run_now(self, query: str) -> list[dict[str, Any]]:
        """Run a schedule's job immediately, out of band, without touching its next scheduled fire."""
        rows = self.ctx.db.query("SELECT * FROM schedules WHERE cancelled_at IS NULL")
        cands = [self._public(dict(r)) for r in rows]
        hits = [s for s in self._match(query, cands) if s["kind"] == "job"]
        for s in hits:
            r = self.ctx.db.query_one("SELECT * FROM schedules WHERE id = ?", (s["id"],))
            self.ctx.audit.record("schedule.run_now", actor="owner", target=s["id"])
            task = asyncio.create_task(self._run_job(s["id"], r["text"], notify=r["notify"]))
            self._jobs.add(task)
            task.add_done_callback(self._jobs.discard)
        return hits

    # ---- watch state ----------------------------------------------------------------------

    def _state(self, sid: str) -> dict[str, Any] | None:
        r = self.ctx.db.query_one("SELECT * FROM schedule_state WHERE schedule_id = ?", (sid,))
        return dict(r) if r else None

    def record_watch(self, sid: str, value: str, summary: str, changed: bool) -> None:
        """Staged by watch_report during a run; the scheduler applies it after the run finishes."""
        self._pending_watch[sid] = {"value": value, "summary": summary, "changed": changed}

    def note_outcome(self, sid: str, *, status: str, stopped_by_approval: bool) -> None:
        """Called by the planner when a scheduled plan ends, so the scheduler can react to it."""
        self._outcomes[sid] = {"status": status, "stopped_by_approval": stopped_by_approval}

    # ---- firing ---------------------------------------------------------------------------

    async def tick(self, now: datetime | None = None) -> list[str]:
        """Fire everything due. Returns the ids fired."""
        now = now or datetime.now(timezone.utc)
        due = self.ctx.db.query(
            "SELECT * FROM schedules WHERE cancelled_at IS NULL AND paused_at IS NULL AND next_run_at IS NOT NULL AND next_run_at <= ?",
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
                task = asyncio.create_task(self._run_job(r["id"], r["text"], notify=r["notify"]))
                self._jobs.add(task)
                task.add_done_callback(self._jobs.discard)
        # Opt-in nudges: re-notify about a waiting task only if the owner gave it a deadline.
        if self.ctx.tasks is not None:
            for t in self.ctx.tasks.due_nudges(iso(now)):
                await self.ctx.notifier.send(f"Still waiting on you: “{t['request'][:100]}” (task {t['id']}).", None)
        return fired

    def _lock_for(self, sid: str) -> asyncio.Lock:
        return self._locks.setdefault(sid, asyncio.Lock())

    async def _run_job(self, sid: str, request: str, *, notify: str = "always") -> None:
        # Per-schedule serialization covers init, execution and final state application, so a second
        # run of the same schedule can never clobber state a first run has staged (the global
        # assistant lock alone is not enough: it is released before state is applied).
        async with self._lock_for(sid):
            await self._run_job_locked(sid, request, notify=notify)

    async def _run_job_locked(self, sid: str, request: str, *, notify: str) -> None:
        chat = self.ctx.chat
        on_change = notify == "on_change"
        exec_id = secrets.token_hex(6)
        started = iso()
        if not on_change:  # a watch stays quiet unless something changed, so no "running" preamble
            await self.ctx.notifier.send(f"Scheduled job: {request}", None)
        # Isolated run state: clear any stragglers, then this run owns these slots until we finalize.
        self._pending_watch.pop(sid, None)
        self._outcomes.pop(sid, None)
        if chat.assistant is None:
            await self._finalize(sid, exec_id, started, on_change=on_change, outcome="missing_model",
                                 reply="The scheduled job didn't run: the model isn't configured.", pending=None)  # fmt: skip
            return
        try:
            reply = await chat.assistant.reply(request, origin=f"schedule:{sid}", schedule_id=sid,
                                               watch_id=sid if on_change else None)  # fmt: skip
        except Exception as e:
            log.warning("scheduled job %s failed: %s", sid, type(e).__name__)
            self._pending_watch.pop(sid, None)  # a hard failure never commits a staged success
            self._outcomes.pop(sid, None)
            await self._finalize(sid, exec_id, started, on_change=on_change, outcome="error",
                                 reply=f"The scheduled job didn't run: {type(e).__name__}.", pending=None)  # fmt: skip
            return
        outcome = self._outcomes.pop(sid, {"status": "done", "stopped_by_approval": False})
        pending = self._pending_watch.pop(sid, None)
        if outcome.get("stopped_by_approval"):
            await self._finalize(sid, exec_id, started, on_change=on_change, outcome="approval_denied",
                                 reply=reply, pending=None, rerun_sid=sid)  # fmt: skip
            return
        if outcome.get("status") not in ("done", None):
            # Blocked / replans exhausted: a staged watch success is discarded, not committed.
            await self._finalize(sid, exec_id, started, on_change=on_change, outcome="blocked", reply=reply, pending=None)
            return
        await self._finalize(sid, exec_id, started, on_change=on_change, outcome="success", reply=reply, pending=pending)

    async def _finalize(self, sid: str, exec_id: str, started: str, *, on_change: bool, outcome: str,
                        reply: str, pending: dict[str, Any] | None, rerun_sid: str | None = None) -> None:
        """One path for every run's end: durable record, watch accounting, chat + notifier delivery.
        A failed/blocked/denied run never commits a staged value and keeps the previous good one."""
        now = iso()
        red = self.ctx.redactor
        msg = reply if rerun_sid is None else f"{reply}\nReply `run {rerun_sid}` (and approve) to run it now."
        if on_change:
            if outcome == "success":
                if pending is None:
                    self._upsert_state(sid, last_checked_at=now, last_error=None)
                    self._record_run(exec_id, sid, "no_change_missing_report", started, now)
                    log.info("watch %s: no watch_report; treated as no change", sid)
                    return
                val = red.redact(pending["value"])
                if pending["changed"]:
                    self._upsert_state(sid, last_checked_at=now, last_error=None, last_value=val,
                                       last_summary=red.redact(pending["summary"]), last_changed_at=now)  # fmt: skip
                    self._record_run(exec_id, sid, "changed", started, now, detail=red.redact(pending["summary"]), value=val)
                    self.ctx.chat.store.add("friday", pending["summary"])
                    await self._notify_reply(pending["summary"])
                else:
                    self._upsert_state(sid, last_checked_at=now, last_error=None)
                    self._record_run(exec_id, sid, "no_change", started, now, value=val)
                return
            # watch failure (error / blocked / approval_denied / missing_model): last-checked always
            # moves, the previous good value/change-time is preserved, and the error posts once.
            await self._watch_error(sid, msg, exec_id=exec_id, started=started, outcome=outcome)
            return
        # non-watch ("always") job: full reply to chat, redacted+capped to external channels, durable.
        self._record_run(exec_id, sid, outcome, started, now, detail=red.redact(reply))
        self.ctx.chat.store.add("friday", msg)
        await self._notify_reply(msg)

    def _record_run(self, exec_id: str, sid: str, outcome: str, started: str, finished: str, *,
                    detail: str = "", value: str | None = None) -> None:
        """One durable, redacted row per run (survives restart; holds the full reply the chat caps)."""
        self.ctx.db.execute(
            "INSERT INTO schedule_runs (id, schedule_id, outcome, detail, value, started_at, finished_at)"
            " VALUES (?, ?, ?, ?, ?, ?, ?)", (exec_id, sid, outcome, detail or None, value, started, finished),
        )

    async def _notify_reply(self, reply: str) -> None:
        """Deliver a scheduled job's final reply to the external channels (Telegram etc.), redacted
        and capped. The full reply is already in the chat, so the built-in chat channel is skipped
        to avoid a duplicate."""
        text = self.ctx.redactor.redact(reply)[:1500]
        channels = getattr(self.ctx.notifier, "notifiers", [self.ctx.notifier])
        for n in channels:
            if getattr(n, "name", "") == "chat":
                continue
            try:
                await n.send(text, None)
            except Exception:
                log.warning("notifier %s failed on a scheduled reply", getattr(n, "name", "?"))

    def _upsert_state(self, sid: str, **cols: Any) -> None:
        if self._state(sid) is None:
            keys = ["schedule_id", *cols]
            self.ctx.db.execute(f"INSERT INTO schedule_state ({', '.join(keys)}) VALUES ({', '.join('?' * len(keys))})",
                                (sid, *cols.values()))  # fmt: skip
        else:
            self.ctx.db.execute(f"UPDATE schedule_state SET {', '.join(f'{k} = ?' for k in cols)} WHERE schedule_id = ?",
                                (*cols.values(), sid))  # fmt: skip

    async def _watch_error(self, sid: str, reply: str, *, exec_id: str, started: str, outcome: str) -> None:
        """Post a watch's failure once; suppress an identical repeat on the next fire. Always records
        last-checked and a durable run row; never touches the preserved last good value."""
        text = self.ctx.redactor.redact(reply)
        st = self._state(sid)
        self._upsert_state(sid, last_checked_at=iso())
        self._record_run(exec_id, sid, outcome, started, iso(), detail=text)
        if st and st.get("last_error") == text:
            log.info("watch %s: same error as last time; suppressed", sid)
            return
        self._upsert_state(sid, last_error=text)
        self.ctx.chat.store.add("friday", reply)
        await self._notify_reply(reply)

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
