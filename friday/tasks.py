"""The task ledger: a durable spine for owner-requested work and crash-safe execution.

Every owner request the planner handles gets one row in `tasks` (the plan id is the task id), moving
through planned -> running -> (waiting_on_me) -> done | blocked | cancelled. If friday restarts
mid-task the row is left in running/waiting and recovery flips it to `interrupted`, tells the owner,
and lets them resume or drop it - friday never silently re-runs a side effect.

Scheduled fires do NOT create task rows (they already record a durable `schedule_runs` row); they
still journal their side effects here so a crash mid-fire is detectable.

Crash safety: each side-effecting tool call is journaled before (started) and after (finished). A
`started` with no matching `finished` is a step that was in flight at the crash - uncertain. On
resume such a step is re-checked by its own tool (the Sheets writer, for instance, reads the tab
back and skips rows already there), not blindly repeated.

Everything stored here is redacted first; no secret, statement or account id reaches these tables.
"""

from __future__ import annotations

import json
import logging
from typing import Any

from friday.util import iso

log = logging.getLogger(__name__)

OPEN_STATUSES = ("planned", "running", "waiting_on_me", "interrupted")
LIVE_AT_CRASH = ("planned", "running", "waiting_on_me")
SIDE_EFFECTS = ("write", "send", "share", "spend")  # "account-change" joins in a later phase


class TaskLedger:
    def __init__(self, ctx: Any):
        self.ctx = ctx

    # ---- lifecycle ---------------------------------------------------------------------------

    def open(self, task_id: str, request: str, *, origin: str = "owner", schedule_id: str | None = None) -> None:
        """Record an owner-requested task. Scheduled fires pass origin='schedule:<id>' and are not stored."""
        if origin != "owner":
            return
        now = iso()
        self.ctx.db.execute(
            "INSERT OR IGNORE INTO tasks (id, request, origin, schedule_id, status, created_at, updated_at)"
            " VALUES (?, ?, ?, ?, 'planned', ?, ?)",
            (task_id, self.ctx.redactor.redact(request)[:2000], origin, schedule_id, now, now),
        )

    def _update(self, task_id: str, **cols: Any) -> None:
        if not self.ctx.db.query_one("SELECT 1 FROM tasks WHERE id = ?", (task_id,)):
            return
        cols["updated_at"] = iso()
        self.ctx.db.execute(f"UPDATE tasks SET {', '.join(f'{k} = ?' for k in cols)} WHERE id = ?",
                            (*cols.values(), task_id))  # fmt: skip

    def running(self, task_id: str, next_step: str | None = None) -> None:
        self._update(task_id, status="running", next_step=self.ctx.redactor.redact(next_step)[:300] if next_step else None)

    def waiting(self, task_id: str, what: str | None = None) -> None:
        self._update(task_id, status="waiting_on_me", next_step=self.ctx.redactor.redact(what)[:300] if what else None)

    def finish(self, task_id: str, status: str, *, closeout: str | None = None, evidence: list[str] | None = None) -> None:
        self._update(task_id, status=status, next_step=None,
                     closeout=self.ctx.redactor.redact(closeout)[:4000] if closeout else None,
                     evidence_json=self.ctx.redactor.redact(json.dumps(evidence)) if evidence else None)  # fmt: skip

    # ---- journal (crash safety) --------------------------------------------------------------

    def journal_start(self, plan_id: str, step_id: str, tool: str, effect: str) -> None:
        self._journal(plan_id, step_id, tool, effect, "started", None)

    def journal_finish(self, plan_id: str, step_id: str, tool: str, effect: str, marker: str | None = None) -> None:
        self._journal(plan_id, step_id, tool, effect, "finished", self.ctx.redactor.redact(marker)[:300] if marker else None)

    def _journal(self, plan_id: str, step_id: str, tool: str, effect: str, phase: str, marker: str | None) -> None:
        self.ctx.db.execute(
            "INSERT INTO task_journal (plan_id, step_id, tool, effect, phase, marker, ts) VALUES (?, ?, ?, ?, ?, ?, ?)",
            (plan_id, step_id, tool, effect, phase, marker, iso()),
        )

    def uncertain_steps(self, plan_id: str) -> list[dict[str, Any]]:
        """Side-effecting steps started but never confirmed finished for this plan."""
        rows = self.ctx.db.query("SELECT * FROM task_journal WHERE plan_id = ? ORDER BY id", (plan_id,))
        finished = {r["step_id"] for r in rows if r["phase"] == "finished"}
        return [dict(r) for r in rows if r["phase"] == "started" and r["step_id"] not in finished]

    # ---- queries -----------------------------------------------------------------------------

    def get(self, task_id: str) -> dict[str, Any] | None:
        r = self.ctx.db.query_one("SELECT * FROM tasks WHERE id = ?", (task_id,))
        return dict(r) if r else None

    def list(self, statuses: tuple[str, ...] | None = None, *, limit: int = 100) -> list[dict[str, Any]]:
        if statuses:
            q = f"SELECT * FROM tasks WHERE status IN ({','.join('?' * len(statuses))}) ORDER BY updated_at DESC LIMIT ?"
            rows = self.ctx.db.query(q, (*statuses, limit))
        else:
            rows = self.ctx.db.query("SELECT * FROM tasks ORDER BY updated_at DESC LIMIT ?", (limit,))
        return [self._public(dict(r)) for r in rows]

    def open_tasks(self) -> list[dict[str, Any]]:
        return self.list(OPEN_STATUSES)

    def waiting_tasks(self) -> list[dict[str, Any]]:
        return self.list(("waiting_on_me", "interrupted"))

    @staticmethod
    def _public(r: dict[str, Any]) -> dict[str, Any]:
        return {"id": r["id"], "request": r["request"], "status": r["status"], "next_step": r["next_step"],
                "closeout": r["closeout"], "origin": r["origin"], "deadline_at": r["deadline_at"],
                "updated_at": r["updated_at"]}  # fmt: skip

    def _resolve(self, query: str, statuses: tuple[str, ...]) -> dict[str, Any] | None:
        """Find one task by exact id or by words in its request, among the given statuses."""
        q = query.strip().lower()
        cands = self.list(statuses)
        hit = [t for t in cands if t["id"] == q]
        if not hit:
            hit = [t for t in cands if q and all(w in (t["request"] or "").lower() for w in q.split())]
        return hit[0] if len(hit) == 1 else None

    # ---- owner actions -----------------------------------------------------------------------

    def cancel(self, query: str) -> dict[str, Any] | None:
        t = self._resolve(query, OPEN_STATUSES)
        if t is None:
            return None
        self.finish(t["id"], "cancelled")
        self.ctx.audit.record("task.cancel", actor="owner", target=t["id"])
        return self.get(t["id"])

    def drop(self, query: str) -> dict[str, Any] | None:
        t = self._resolve(query, ("interrupted",))
        if t is None:
            return None
        self.finish(t["id"], "cancelled")
        self.ctx.audit.record("task.drop", actor="owner", target=t["id"])
        return self.get(t["id"])

    def set_deadline(self, query: str, when_iso: str | None) -> dict[str, Any] | None:
        t = self._resolve(query, OPEN_STATUSES)
        if t is None:
            return None
        self._update(t["id"], deadline_at=when_iso)
        self.ctx.audit.record("task.deadline", actor="owner", target=t["id"], deadline=when_iso)
        return self.get(t["id"])

    def due_nudges(self, now_iso: str) -> list[dict[str, Any]]:
        """Waiting/interrupted tasks whose owner-set deadline has passed and that haven't been
        nudged yet. Opt-in: a task with no deadline is never re-notified, only listed."""
        rows = self.ctx.db.query(
            "SELECT * FROM tasks WHERE deadline_at IS NOT NULL AND deadline_at <= ? AND notified_at IS NULL"
            " AND status IN ('waiting_on_me', 'interrupted')", (now_iso,))  # fmt: skip
        out = []
        for r in rows:
            self._update(r["id"], notified_at=iso())  # nudge once
            out.append(self._public(dict(r)))
        return out

    def to_resume(self, query: str) -> dict[str, Any] | None:
        """The task's stored request, for the planner to run again (owner-approved resume)."""
        return self._resolve(query, ("interrupted",))

    def mark_running_again(self, task_id: str) -> None:
        self._update(task_id, status="running")

    # ---- recovery ----------------------------------------------------------------------------

    def recover(self) -> list[dict[str, Any]]:
        """At startup: any owner task left mid-flight is now interrupted (returned so the caller can
        tell the owner). For a scheduled fire that was in flight when it crashed (a side effect
        journaled 'started' but never 'finished', on a plan tied to a schedule and not an owner task),
        a durable 'interrupted' schedule_runs row is recorded - with the uncertain steps listed - so
        it surfaces in the daily brief's "didn't complete" section."""
        live = self.ctx.db.query(f"SELECT id FROM tasks WHERE status IN ({','.join('?' * len(LIVE_AT_CRASH))})", LIVE_AT_CRASH)
        for r in live:
            self._update(r["id"], status="interrupted")
        # scheduled fires have no task row; map an in-flight one to its schedule via the plan row.
        sched = self.ctx.db.query(
            "SELECT p.id AS plan_id, p.schedule_id AS schedule_id FROM plans p"
            " WHERE p.schedule_id IS NOT NULL AND p.id NOT IN (SELECT id FROM tasks)"
            " AND p.id IN (SELECT plan_id FROM task_journal)"
        )
        for r in sched:
            uncertain = self.uncertain_steps(r["plan_id"])
            if not uncertain:
                continue
            steps = ", ".join(f"{u['tool']}({u['step_id']})" for u in uncertain)
            now = iso()
            # id keyed by the plan so repeated recover() calls don't duplicate the row.
            self.ctx.db.execute(
                "INSERT OR IGNORE INTO schedule_runs (id, schedule_id, outcome, detail, started_at, finished_at)"
                " VALUES (?, ?, 'interrupted', ?, ?, ?)",
                (f"intr-{r['plan_id']}", r["schedule_id"],
                 f"a restart interrupted this scheduled run mid side-effect; uncertain steps: {steps}", now, now),
            )  # fmt: skip
            log.warning("scheduled plan %s (schedule %s) was interrupted mid side-effect", r["plan_id"], r["schedule_id"])
        if live or sched:
            self.ctx.audit.record("task.recover", actor="system", count=len(live))
        return [self.get(r["id"]) for r in live]  # type: ignore[misc]
