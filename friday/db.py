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
