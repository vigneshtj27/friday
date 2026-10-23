"""Phase 7: encrypted backup, the restore test (integrity + wrong-key safety), and /healthz fields."""

from __future__ import annotations

import sqlite3

import pytest
from fastapi.testclient import TestClient

from friday.backup import make_backup, restore_backup, snapshot_bytes
from friday.db import Database
from friday.vault.crypto import Cipher
from friday.web.app import create_app

KEY = bytes(range(32))
OTHER_KEY = bytes(range(1, 33))


def _seed_db(path) -> Database:
    db = Database(path)
    db.execute("INSERT INTO prefs (key, value_json, updated_at) VALUES ('k', '\"v\"', '2026-10-23')")
    return db


# ---- backup / restore round trip ----------------------------------------------------------------


def test_backup_then_restore_preserves_data(tmp_path):
    src = tmp_path / "friday.db"
    db = _seed_db(src)
    db.close()
    blob = make_backup(src, Cipher(KEY))
    assert blob and isinstance(blob, bytes)

    dest = tmp_path / "restored.db"
    restore_backup(blob, dest, Cipher(KEY))
    con = sqlite3.connect(str(dest))
    try:
        assert con.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
        assert con.execute("SELECT value_json FROM prefs WHERE key = 'k'").fetchone()[0] == '"v"'
    finally:
        con.close()


def test_snapshot_is_consistent_while_open(tmp_path):
    # the online backup API works on a live connection, so a snapshot can be taken while friday runs
    src = tmp_path / "friday.db"
    db = _seed_db(src)
    data = snapshot_bytes(src)
    assert data[:16].startswith(b"SQLite format 3")
    db.close()


def test_restore_rejects_the_wrong_key_without_touching_the_target(tmp_path):
    src = tmp_path / "friday.db"
    _seed_db(src).close()
    blob = make_backup(src, Cipher(KEY))

    target = tmp_path / "live.db"
    target.write_bytes(b"EXISTING GOOD DB")
    with pytest.raises(ValueError, match="decrypt"):
        restore_backup(blob, target, Cipher(OTHER_KEY))
    assert target.read_bytes() == b"EXISTING GOOD DB"  # untouched


def test_restore_rejects_a_corrupt_blob(tmp_path):
    src = tmp_path / "friday.db"
    _seed_db(src).close()
    blob = bytearray(make_backup(src, Cipher(KEY)))
    blob[-5:] = b"xxxxx"  # corrupt the ciphertext tail
    target = tmp_path / "live.db"
    target.write_bytes(b"GOOD")
    with pytest.raises(ValueError):
        restore_backup(bytes(blob), target, Cipher(KEY))
    assert target.read_bytes() == b"GOOD"


# ---- /healthz carries operational facts ---------------------------------------------------------


def test_healthz_shows_version_last_run_and_channels(ctx):
    # a successful scheduled run should surface as last_successful_scheduled_run
    ctx.db.execute(
        "INSERT INTO schedule_runs (id, schedule_id, outcome, started_at, finished_at)"
        " VALUES ('r1', 's1', 'success', '2026-10-23T09:00:00Z', '2026-10-23T09:01:00Z')"
    )
    with TestClient(create_app(ctx), base_url="http://testserver") as c:
        h = c.get("/healthz").json()
    assert h["ok"] is True and "version" in h
    assert h["last_successful_scheduled_run"] == "2026-10-23T09:01:00Z"
    assert "channels" in h and isinstance(h["channels"], list)
