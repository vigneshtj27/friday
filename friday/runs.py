"""Persistent run records and their step logs (all text redacted before storage)."""

from __future__ import annotations

import json
import secrets
from enum import StrEnum
from typing import Any

from friday.db import Database
from friday.util import canonical_json, iso
from friday.vault.masking import Redactor


class RunStatus(StrEnum):
    QUEUED = "queued"
    RUNNING = "running"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    NEEDS_ATTENTION = "needs_attention"
    ABORTED = "aborted"


TERMINAL = {RunStatus.SUCCEEDED, RunStatus.FAILED, RunStatus.NEEDS_ATTENTION, RunStatus.ABORTED}


class RunStore:
    def __init__(self, db: Database, redactor: Redactor):
        self._db = db
        self._redactor = redactor

    def create(self, kind: str, profile_id: str, params: dict[str, Any]) -> str:
        run_id = secrets.token_hex(8)
        self._db.execute(
            "INSERT INTO runs (id, kind, profile_id, params_json, status, created_at) VALUES (?, ?, ?, ?, ?, ?)",
            (run_id, kind, profile_id, canonical_json(params), RunStatus.QUEUED.value, iso()),
        )
        return run_id

    def start(self, run_id: str) -> None:
        self._db.execute("UPDATE runs SET status = ?, started_at = ? WHERE id = ?", (RunStatus.RUNNING.value, iso(), run_id))

    def finish(
        self, run_id: str, status: RunStatus, summary: str, result: dict[str, Any] | None = None, cost_usd: float = 0.0
    ) -> None:
        self._db.execute(
            "UPDATE runs SET status = ?, finished_at = ?, summary = ?, result_json = ?, cost_usd = cost_usd + ? WHERE id = ?",
            (
                status.value,
                iso(),
                self._redactor.redact(summary)[:4000],
                canonical_json(self._redactor.redact_obj(result or {})),
                cost_usd,
                run_id,
            ),
        )

    def add_cost(self, run_id: str, usd: float) -> None:
        self._db.execute("UPDATE runs SET cost_usd = cost_usd + ? WHERE id = ?", (usd, run_id))

    def step(
        self,
        run_id: str,
        kind: str,
        *,
        tool: str | None = None,
        args: dict[str, Any] | None = None,
        result: str | None = None,
        state: str | None = None,
        screenshot_file_id: str | None = None,
    ) -> None:
        with self._db.tx() as c:
            c.execute(
                "INSERT INTO run_steps (run_id, ts, kind, tool, args_json, result, state, screenshot_file_id)"
                " VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    run_id,
                    iso(),
                    kind,
                    tool,
                    canonical_json(self._redactor.redact_obj(args)) if args is not None else None,
                    self._redactor.redact(result)[:8000] if result else None,
                    state,
                    screenshot_file_id,
                ),
            )
            if kind == "tool":
                c.execute("UPDATE runs SET steps = steps + 1 WHERE id = ?", (run_id,))

    def get(self, run_id: str) -> dict[str, Any] | None:
        r = self._db.query_one("SELECT * FROM runs WHERE id = ?", (run_id,))
        if not r:
            return None
        d = dict(r)
        d["params"] = json.loads(d.pop("params_json"))
        d["result"] = json.loads(d.pop("result_json")) if d.get("result_json") else None
        return d

    def steps(self, run_id: str, after_id: int = 0) -> list[dict[str, Any]]:
        rows = self._db.query("SELECT * FROM run_steps WHERE run_id = ? AND id > ? ORDER BY id", (run_id, after_id))
        out = []
        for r in rows:
            d = dict(r)
            d["args"] = json.loads(d.pop("args_json")) if d.get("args_json") else None
            out.append(d)
        return out

    def list(self, *, profile_id: str | None = None, limit: int = 100) -> list[dict[str, Any]]:
        sql, params = "SELECT id, kind, profile_id, status, created_at, started_at, finished_at, summary, steps, cost_usd FROM runs", []
        if profile_id:
            sql += " WHERE profile_id = ?"
            params.append(profile_id)
        sql += " ORDER BY created_at DESC LIMIT ?"
        params.append(limit)
        return [dict(r) for r in self._db.query(sql, params)]
