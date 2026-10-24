#!/usr/bin/env python3
"""A real crash test for the task ledger: SIGKILL the process mid-write, restart, prove no duplicate.

Unlike tests/test_tasks.py (which *simulates* the interruption), this is killed for real by
scripts/crash_test.sh. It uses the real SQLite journal and a durable file-backed "sheet" with the
same read-back idempotency the Sheets writer uses, so both the journal and the write target survive
the kill.

Subcommands (sharing $CRASH_DIR):
  write          open the ledger, journal a side-effecting step 'started', write the row to the
                 durable sheet, then sleep (the kill window) BEFORE journalling 'finished' - i.e. a
                 crash after the write landed but before it was confirmed, the hardest case.
  check-written  exit 0 once the row is on the sheet and the journal shows a started-not-finished step.
  resume         restart: recover() surfaces the interrupted task, then re-run the same append; the
                 idempotent writer skips the row already there. Asserts exactly one row; exit 0/1.
"""

from __future__ import annotations

import json
import os
import sys
import time
from pathlib import Path

from friday.audit import AuditLog
from friday.db import Database
from friday.tasks import TaskLedger
from friday.vault.masking import Redactor

ROW_KEY = "2026-10-20|COFFEE|5"
ROW = {"date": "2026-10-20", "description": "COFFEE", "amount": "5"}


class _Ctx:
    """The minimal surface TaskLedger needs."""

    def __init__(self, d: Path):
        self.db = Database(str(d / "friday.db"))
        self.redactor = Redactor()
        self.audit = AuditLog(self.db, self.redactor)


def _dir() -> Path:
    return Path(os.environ["CRASH_DIR"])


def _sheet() -> Path:
    return _dir() / "sheet.json"


def _read_sheet() -> list[dict]:
    p = _sheet()
    return json.loads(p.read_text()) if p.exists() else []


def append_idempotent(key: str, row: dict) -> bool:
    """Append unless a row with this key is already present (read-back dedup). Returns True if added."""
    rows = _read_sheet()
    if any(r["key"] == key for r in rows):
        return False
    rows.append({"key": key, "row": row})
    _sheet().write_text(json.dumps(rows))
    return True


def do_write() -> None:
    ctx = _Ctx(_dir())
    ledger = TaskLedger(ctx)
    ledger.open("t-crash", "add my coffee expense")
    ledger.running("t-crash", "appending to the sheet")
    ledger.journal_start("t-crash", "s1", "sheets_append_rows", "write")
    append_idempotent(ROW_KEY, ROW)  # the row lands here
    (_dir() / "written.flag").write_text("1")
    time.sleep(60)  # the kill window: a crash AFTER the write, BEFORE journal_finish
    ledger.journal_finish("t-crash", "s1", "sheets_append_rows", "write")  # never reached when killed


def check_written() -> int:
    ctx = _Ctx(_dir())
    ledger = TaskLedger(ctx)
    wrote = any(r["key"] == ROW_KEY for r in _read_sheet())
    uncertain = bool(ledger.uncertain_steps("t-crash"))
    return 0 if (wrote and uncertain) else 1


def do_resume() -> int:
    ctx = _Ctx(_dir())
    ledger = TaskLedger(ctx)
    before_rows = len(_read_sheet())
    uncertain_before = ledger.uncertain_steps("t-crash")
    interrupted = ledger.recover()
    recovered = [t["id"] for t in interrupted]
    # resume: re-run the same append. The durable sheet is read back; the row already there is skipped.
    added = append_idempotent(ROW_KEY, ROW)
    ledger.journal_finish("t-crash", "s1", "sheets_append_rows", "write")
    after_rows = len(_read_sheet())
    ok = (before_rows == 1 and len(uncertain_before) == 1 and "t-crash" in recovered
          and added is False and after_rows == 1)  # fmt: skip
    print(json.dumps({
        "rows_before_resume": before_rows, "uncertain_steps_at_crash": len(uncertain_before),
        "recovered_interrupted": recovered, "row_added_on_resume": added, "rows_after_resume": after_rows,
        "no_duplicate": after_rows == 1, "ok": ok,
    }, indent=2))  # fmt: skip
    return 0 if ok else 1


def main() -> int:
    cmd = sys.argv[1] if len(sys.argv) > 1 else ""
    if cmd == "write":
        do_write()
        return 0
    if cmd == "check-written":
        return check_written()
    if cmd == "resume":
        return do_resume()
    print("usage: crash_test.py write|check-written|resume", file=sys.stderr)
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
