from __future__ import annotations

import sqlite3
import threading
from collections.abc import Iterator, Sequence
from contextlib import contextmanager
from pathlib import Path
from typing import Any

# Append-only list; index + 1 == schema version.
MIGRATIONS: list[str] = [
    """
    CREATE TABLE secrets (
        name TEXT PRIMARY KEY,
        kind TEXT NOT NULL,
        ciphertext BLOB NOT NULL,
        created_at TEXT NOT NULL,
        updated_at TEXT NOT NULL,
        last_used_at TEXT
    );
    CREATE TABLE sites (
        id TEXT PRIMARY KEY,
        name TEXT NOT NULL,
        login_url TEXT,
        domains_json TEXT NOT NULL,
        otp_domains_json TEXT NOT NULL DEFAULT '[]',
        created_at TEXT NOT NULL
    );
    CREATE TABLE profiles (
        id TEXT PRIMARY KEY,
        site_id TEXT NOT NULL REFERENCES sites(id),
        account TEXT NOT NULL,
        config_json TEXT NOT NULL,
        fingerprint_json TEXT,
        status TEXT NOT NULL DEFAULT 'new',
        created_at TEXT NOT NULL,
        last_used_at TEXT,
        UNIQUE (site_id, account)
    );
    CREATE TABLE audit_log (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        ts TEXT NOT NULL,
        actor TEXT NOT NULL,
        action TEXT NOT NULL,
        target TEXT,
        details_json TEXT NOT NULL,
        prev_hash TEXT NOT NULL,
        hash TEXT NOT NULL
    );
    CREATE INDEX audit_log_action ON audit_log(action);
    CREATE TABLE handoffs (
        id TEXT PRIMARY KEY,
        profile_id TEXT NOT NULL,
        reason TEXT NOT NULL,
        state TEXT NOT NULL,
        created_at TEXT NOT NULL,
        expires_at TEXT NOT NULL,
        completed_at TEXT
    );
    """,
    """
    ALTER TABLE sites ADD COLUMN otp_senders_json TEXT NOT NULL DEFAULT '[]';
    CREATE TABLE runs (
        id TEXT PRIMARY KEY,
        kind TEXT NOT NULL,
        profile_id TEXT NOT NULL,
        params_json TEXT NOT NULL,
        status TEXT NOT NULL,
        created_at TEXT NOT NULL,
        started_at TEXT,
        finished_at TEXT,
        summary TEXT,
        result_json TEXT,
        steps INTEGER NOT NULL DEFAULT 0,
        cost_usd REAL NOT NULL DEFAULT 0
    );
    CREATE INDEX runs_profile ON runs(profile_id, created_at);
    CREATE TABLE run_steps (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        run_id TEXT NOT NULL REFERENCES runs(id) ON DELETE CASCADE,
        ts TEXT NOT NULL,
        kind TEXT NOT NULL,
        tool TEXT,
        args_json TEXT,
        result TEXT,
        state TEXT,
        screenshot_file_id TEXT
    );
    CREATE INDEX run_steps_run ON run_steps(run_id, id);
    CREATE TABLE approvals (
        id TEXT PRIMARY KEY,
        run_id TEXT,
        profile_id TEXT NOT NULL,
        action TEXT NOT NULL,
        details_json TEXT NOT NULL,
        status TEXT NOT NULL,
        created_at TEXT NOT NULL,
        expires_at TEXT NOT NULL,
        decided_at TEXT
    );
    CREATE TABLE otp_requests (
        id TEXT PRIMARY KEY,
        run_id TEXT,
        profile_id TEXT NOT NULL,
        channel TEXT NOT NULL,
        prompt TEXT NOT NULL,
        status TEXT NOT NULL,
        created_at TEXT NOT NULL,
        expires_at TEXT NOT NULL,
        resolved_at TEXT,
        resolved_via TEXT
    );
    CREATE TABLE diagnosis_reports (
        id TEXT PRIMARY KEY,
        run_id TEXT,
        profile_id TEXT NOT NULL,
        trigger_state TEXT NOT NULL,
        report_json TEXT NOT NULL,
        created_at TEXT NOT NULL
    );
    CREATE TABLE files (
        id TEXT PRIMARY KEY,
        profile_id TEXT,
        run_id TEXT,
        name TEXT NOT NULL,
        mime TEXT NOT NULL,
        size INTEGER NOT NULL,
        sha256 TEXT NOT NULL,
        kind TEXT NOT NULL,
        created_at TEXT NOT NULL
    );
    CREATE TABLE transactions (
        hash TEXT PRIMARY KEY,
        source TEXT NOT NULL,
        date TEXT NOT NULL,
        description TEXT NOT NULL,
        amount TEXT NOT NULL,
        currency TEXT NOT NULL,
        direction TEXT NOT NULL,
        balance TEXT,
        raw_ref TEXT NOT NULL,
        created_at TEXT NOT NULL
    );
    CREATE INDEX transactions_source_date ON transactions(source, date);
    CREATE TABLE site_notes (
        id TEXT PRIMARY KEY,
        site_id TEXT NOT NULL,
        kind TEXT NOT NULL,
        text TEXT NOT NULL,
        stale INTEGER NOT NULL DEFAULT 0,
        source_run TEXT,
        created_at TEXT NOT NULL,
        confirmed_at TEXT NOT NULL
    );
    CREATE TABLE site_backoff (
        site_id TEXT PRIMARY KEY,
        failures INTEGER NOT NULL DEFAULT 0,
        last_attempt_at TEXT,
        next_allowed_at TEXT
    );
    """,
    """
    CREATE TABLE chat_messages (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        created_at TEXT NOT NULL,
        sender TEXT NOT NULL,
        text TEXT NOT NULL,
        url TEXT,
        kind TEXT,
        ref TEXT,
        profile_id TEXT
    );
    """,
    """
    CREATE TABLE setup_requests (
        id TEXT PRIMARY KEY,
        status TEXT NOT NULL,
        site_json TEXT NOT NULL,
        site_is_new INTEGER NOT NULL,
        profile_id TEXT NOT NULL,
        fields_json TEXT NOT NULL,
        purpose TEXT NOT NULL,
        resume_json TEXT,
        created_at TEXT NOT NULL,
        expires_at TEXT NOT NULL,
        completed_at TEXT
    );
    """,
    """
    CREATE TABLE prefs (
        key TEXT PRIMARY KEY,
        value_json TEXT NOT NULL,
        updated_at TEXT NOT NULL
    );
    """,
    """
    CREATE TABLE sheet_rows (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        spreadsheet TEXT NOT NULL,
        tab TEXT NOT NULL,
        values_json TEXT NOT NULL,
        plan_id TEXT,
        created_at TEXT NOT NULL,
        removed_at TEXT
    );
    CREATE INDEX sheet_rows_tab ON sheet_rows(spreadsheet, tab);
    """,
    """
    CREATE TABLE memory (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        kind TEXT NOT NULL,
        subject TEXT,
        text TEXT NOT NULL,
        source TEXT NOT NULL,
        created_at TEXT NOT NULL,
        forgotten_at TEXT
    );
    """,
    """
    CREATE TABLE plans (
        id TEXT PRIMARY KEY,
        request TEXT NOT NULL,
        goal TEXT,
        status TEXT NOT NULL,
        state_json TEXT NOT NULL,
        created_at TEXT NOT NULL,
        updated_at TEXT NOT NULL
    );
    """,
    """
    CREATE TABLE schedules (
        id TEXT PRIMARY KEY,
        kind TEXT NOT NULL,
        text TEXT NOT NULL,
        spec_json TEXT NOT NULL,
        tz TEXT NOT NULL,
        next_run_at TEXT,
        last_run_at TEXT,
        runs INTEGER NOT NULL DEFAULT 0,
        created_at TEXT NOT NULL,
        cancelled_at TEXT
    );
    """,
    """
    -- Email-evidence expenses: 'bank' = confirmed from a statement/activity fetch (existing rows),
    -- 'email' = parsed from a bank/card/merchant email (unverified, exported as PENDING),
    -- 'confirmed' = an email row a later bank fetch matched (reconciled; not re-exported).
    ALTER TABLE transactions ADD COLUMN status TEXT NOT NULL DEFAULT 'bank';
    ALTER TABLE transactions ADD COLUMN evidence_ref TEXT;
    """,
    """
    -- In-chat vault-request cards: collect any missing secret straight into the vault. The field
    -- list and each secret's vault name are chosen in code (never by the model or owner free text);
    -- values never touch this table - only the field labels and vault names do.
    CREATE TABLE vault_requests (
        id TEXT PRIMARY KEY,
        status TEXT NOT NULL,
        request_id TEXT NOT NULL,
        title TEXT NOT NULL,
        purpose TEXT NOT NULL,
        fields_json TEXT NOT NULL,
        resume_json TEXT,
        created_at TEXT NOT NULL,
        expires_at TEXT NOT NULL,
        completed_at TEXT
    );
    """,
    """
    -- Pause/resume and watch (on_change) support for schedules.
    ALTER TABLE schedules ADD COLUMN paused_at TEXT;
    ALTER TABLE schedules ADD COLUMN notify TEXT NOT NULL DEFAULT 'always';
    -- Per-watch state: the last canonical value seen, and when it was last checked/changed. A watch
    -- stays quiet unless the value changes; this lets schedule_list show a silent watch is alive.
    -- last_value is already redacted by watch_report before it gets here; no secrets are stored.
    CREATE TABLE schedule_state (
        schedule_id TEXT PRIMARY KEY,
        last_value TEXT,
        last_summary TEXT,
        last_error TEXT,
        last_checked_at TEXT,
        last_changed_at TEXT
    );
    """,
    """
    -- One durable, redacted record per scheduled run (fire or run-now). Outcome is one of
    -- changed | no_change | no_change_missing_report | error | blocked | approval_denied |
    -- missing_model. `detail` holds the full redacted reply/summary/error (the chat store caps
    -- messages, this does not), so a long result survives restart and stays inspectable.
    CREATE TABLE schedule_runs (
        id TEXT PRIMARY KEY,
        schedule_id TEXT NOT NULL,
        outcome TEXT NOT NULL,
        detail TEXT,
        value TEXT,
        started_at TEXT NOT NULL,
        finished_at TEXT NOT NULL
    );
    CREATE INDEX schedule_runs_sid ON schedule_runs(schedule_id, started_at);
    """,
    """
    -- Task ledger: one durable row per owner-requested task (its plan id is the task id). Scheduled
    -- fires do not create task rows (they record schedule_runs); they still journal their side
    -- effects for crash safety. status: planned|running|waiting_on_me|blocked|interrupted|done|
    -- cancelled. All text is redacted before it lands here; no secrets.
    CREATE TABLE tasks (
        id TEXT PRIMARY KEY,
        request TEXT NOT NULL,
        origin TEXT NOT NULL,
        schedule_id TEXT,
        status TEXT NOT NULL,
        next_step TEXT,
        closeout TEXT,
        evidence_json TEXT,
        deadline_at TEXT,
        notified_at TEXT,
        created_at TEXT NOT NULL,
        updated_at TEXT NOT NULL
    );
    CREATE INDEX tasks_status ON tasks(status, updated_at);
    -- Side-effecting steps are journaled before (started) and after (finished), so a step that was
    -- in flight at a crash is detectable: a 'started' with no matching 'finished'. On resume such a
    -- step is never silently re-run; its tool re-checks state (read-back) and the owner is asked.
    CREATE TABLE task_journal (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        plan_id TEXT NOT NULL,
        step_id TEXT NOT NULL,
        tool TEXT NOT NULL,
        effect TEXT NOT NULL,
        phase TEXT NOT NULL,
        marker TEXT,
        ts TEXT NOT NULL
    );
    CREATE INDEX task_journal_plan ON task_journal(plan_id, step_id);
    """,
    """
    -- Durable model-usage meter: one row per model call, so a spend cap survives restarts and spans
    -- every caller (planner, chat, agent loop). `window_key` is the budget window the call fell in
    -- (a calendar month 'YYYY-MM', a day 'YYYY-MM-DD', or 'all'); summing cost_usd for the current
    -- key gives the spend to compare against the cap. No prompt or response text is stored - only the
    -- model name, the kind of caller, and the cost.
    CREATE TABLE usage_ledger (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        ts TEXT NOT NULL,
        window_key TEXT NOT NULL,
        model TEXT NOT NULL,
        kind TEXT NOT NULL,
        cost_usd REAL NOT NULL,
        input_tokens INTEGER NOT NULL DEFAULT 0,
        output_tokens INTEGER NOT NULL DEFAULT 0
    );
    CREATE INDEX usage_ledger_window ON usage_ledger(window_key);
    -- One row the first time a window crosses the warn threshold, so the 80% warning is sent once per
    -- window even across restarts.
    CREATE TABLE usage_notices (
        window_key TEXT PRIMARY KEY,
        warned_at TEXT NOT NULL
    );
    """,
    """
    -- Playbooks: owner-approved, versioned instructions for a repeatable task, plus a small header
    -- that can only NARROW what a run may do (allowed domains, allowed effect classes, required
    -- approvals, a success check). A playbook is created and edited only by an owner tap on an
    -- approval card (the tool needs owner_quote AND a card), never by a web page. `body` and the
    -- header are redacted before they land here.
    CREATE TABLE playbooks (
        id TEXT PRIMARY KEY,
        name TEXT NOT NULL UNIQUE,
        goal TEXT NOT NULL,
        body TEXT NOT NULL,
        allowed_domains_json TEXT NOT NULL DEFAULT '[]',
        allowed_effects_json TEXT NOT NULL DEFAULT '["read"]',
        required_approvals_json TEXT NOT NULL DEFAULT '[]',
        success_check TEXT,
        version INTEGER NOT NULL DEFAULT 1,
        last_good_version INTEGER,
        status TEXT NOT NULL DEFAULT 'active',
        source_run TEXT,
        created_at TEXT NOT NULL,
        updated_at TEXT NOT NULL
    );
    -- Full history: every save writes a version row, so a diff can be shown and a failed success
    -- check can roll back to the last-good version.
    CREATE TABLE playbook_versions (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        playbook_id TEXT NOT NULL,
        version INTEGER NOT NULL,
        goal TEXT NOT NULL,
        body TEXT NOT NULL,
        header_json TEXT NOT NULL,
        success_check TEXT,
        created_at TEXT NOT NULL,
        UNIQUE (playbook_id, version)
    );
    """,
    """
    -- Spend ledger: one row per attempted payment, bound to the approval that authorised it and the
    -- task it served. outcome: committed | failed | uncertain | refused | denied | void. A failed or
    -- uncertain payment is never retried automatically (the order page/email is checked and the owner
    -- asked first). Amounts and labels are redacted; no card number, CVV or full account id is stored.
    CREATE TABLE spend_ledger (
        id TEXT PRIMARY KEY,
        task_id TEXT,
        run_id TEXT,
        approval_id TEXT,
        merchant TEXT NOT NULL,
        total TEXT NOT NULL,
        currency TEXT NOT NULL,
        items TEXT,
        address_label TEXT,
        method_label TEXT,
        order_id TEXT,
        outcome TEXT NOT NULL,
        detail TEXT,
        created_at TEXT NOT NULL,
        updated_at TEXT NOT NULL
    );
    CREATE INDEX spend_ledger_day ON spend_ledger(currency, created_at);
    """,
    """
    -- Link a saved plan to the schedule that fired it, so a crash mid-scheduled-run can be mapped
    -- back to its schedule and recorded as a durable 'interrupted' schedule_runs row on recovery.
    ALTER TABLE plans ADD COLUMN schedule_id TEXT;
    """,
]


class Database:
    """Thin thread-safe wrapper around one SQLite connection."""

    def __init__(self, path: Path | str):
        self.path = str(path)
        if self.path != ":memory:":
            Path(self.path).parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(self.path, check_same_thread=False, isolation_level=None)
        self._conn.row_factory = sqlite3.Row
        self._lock = threading.RLock()
        self._conn.execute("PRAGMA foreign_keys = ON")
        # A second connection's BEGIN IMMEDIATE waits (up to 5s) for the current writer instead of
        # raising "database is locked"; this is what keeps the one-use grant consume safe when two
        # processes race for it.
        self._conn.execute("PRAGMA busy_timeout = 5000")
        if self.path != ":memory:":
            self._conn.execute("PRAGMA journal_mode = WAL")
        self._migrate()

    def _migrate(self) -> None:
        with self._lock:
            version = self._conn.execute("PRAGMA user_version").fetchone()[0]
            for i, script in enumerate(MIGRATIONS[version:], start=version + 1):
                self._conn.executescript(f"BEGIN;\n{script}\nPRAGMA user_version = {i};\nCOMMIT;")

    @contextmanager
    def tx(self) -> Iterator[sqlite3.Connection]:
        with self._lock:
            self._conn.execute("BEGIN IMMEDIATE")
            try:
                yield self._conn
            except BaseException:
                self._conn.execute("ROLLBACK")
                raise
            else:
                self._conn.execute("COMMIT")

    def execute(self, sql: str, params: Sequence[Any] = ()) -> int:
        with self.tx() as c:
            return c.execute(sql, params).rowcount

    def query(self, sql: str, params: Sequence[Any] = ()) -> list[sqlite3.Row]:
        with self._lock:
            return self._conn.execute(sql, params).fetchall()

    def query_one(self, sql: str, params: Sequence[Any] = ()) -> sqlite3.Row | None:
        with self._lock:
            return self._conn.execute(sql, params).fetchone()

    def close(self) -> None:
        with self._lock:
            self._conn.close()
