"""The opt-in daily brief: one roundup of what needs the owner's attention.

It gathers, for the last day: pending approvals, open tasks, reminders due soon, watches that
changed, and scheduled runs that failed or were blocked. If none of those has anything, the brief is
silent - nothing is sent. It reads only friday's own durable state; it starts no plan and touches no
site, so untrusted content can never reach it.
"""

from __future__ import annotations

from datetime import datetime, timedelta
from typing import Any

from friday.util import iso, utcnow


def _due_reminders(ctx: Any, now: datetime, horizon: datetime) -> list[str]:
    rows = ctx.db.query(
        "SELECT text, next_run_at FROM schedules WHERE kind = 'reminder' AND cancelled_at IS NULL"
        " AND paused_at IS NULL AND next_run_at IS NOT NULL AND next_run_at <= ? ORDER BY next_run_at",
        (iso(horizon),),
    )
    return [r["text"] for r in rows]


def _changed_watches(ctx: Any, since: str) -> list[str]:
    rows = ctx.db.query(
        "SELECT last_summary FROM schedule_state WHERE last_changed_at IS NOT NULL AND last_changed_at >= ?"
        " AND last_summary IS NOT NULL ORDER BY last_changed_at DESC",
        (since,),
    )
    return [r["last_summary"] for r in rows]


def _failed_runs(ctx: Any, since: str) -> list[str]:
    rows = ctx.db.query(
        "SELECT s.text AS text, r.outcome AS outcome FROM schedule_runs r JOIN schedules s ON s.id = r.schedule_id"
        " WHERE r.finished_at >= ? AND r.outcome IN ('error', 'blocked', 'approval_denied') ORDER BY r.finished_at DESC",
        (since,),
    )
    return [f"{r['text']} ({r['outcome']})" for r in rows]


def build_brief(ctx: Any, *, now: datetime | None = None) -> str | None:
    now = now or utcnow()
    since = iso(now - timedelta(hours=24))
    horizon = now + timedelta(hours=24)
    sections: list[str] = []

    pending = ctx.gate.pending() if ctx.gate else []
    if pending:
        sections.append("Waiting on your approval:\n" + "\n".join(f"  • {a.action}" for a in pending[:10]))

    tasks = ctx.tasks.open_tasks() if ctx.tasks else []
    if tasks:
        sections.append("Open tasks:\n" + "\n".join(f"  • {t['request'][:90]} ({t['status']})" for t in tasks[:10]))

    reminders = _due_reminders(ctx, now, horizon)
    if reminders:
        sections.append("Reminders due soon:\n" + "\n".join(f"  • {t}" for t in reminders[:10]))

    changed = _changed_watches(ctx, since)
    if changed:
        sections.append("Watches that changed:\n" + "\n".join(f"  • {s[:120]}" for s in changed[:10]))

    failed = _failed_runs(ctx, since)
    if failed:
        sections.append("Scheduled runs that didn't complete:\n" + "\n".join(f"  • {t}" for t in failed[:10]))

    if not sections:
        return None
    return ctx.redactor.redact("Daily brief\n\n" + "\n\n".join(sections))
