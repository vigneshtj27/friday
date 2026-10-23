"""Encrypted backup and restore of the SQLite database.

A backup is a consistent snapshot of the live database (taken with SQLite's online backup API, so it
is safe while friday is running), encrypted with the same master key the vault uses, written as a
single opaque blob. Nothing in a backup is readable without that key - it already holds encrypted
secrets, and this wraps the whole file again. A restore decrypts the blob, checks its integrity, and
only then puts it in place, so a truncated or wrong-key download can never overwrite a good database.

The shell wrappers (scripts/backup.sh, scripts/restore.sh) move the blob to and from a private
bucket; this module does the snapshot, the crypto and the integrity check, all testable offline.
"""

from __future__ import annotations

import logging
import sqlite3
import tempfile
from pathlib import Path

from friday.vault.crypto import Cipher

log = logging.getLogger(__name__)

AAD = b"friday-db-backup-v1"


def snapshot_bytes(db_path: str | Path) -> bytes:
    """A consistent copy of the database's bytes, taken with the online backup API."""
    src = sqlite3.connect(str(db_path))
    try:
        with tempfile.TemporaryDirectory() as d:
            tmp = Path(d) / "snap.db"
            dst = sqlite3.connect(str(tmp))
            try:
                src.backup(dst)  # consistent even while the source is being written
            finally:
                dst.close()
            return tmp.read_bytes()
    finally:
        src.close()


def make_backup(db_path: str | Path, cipher: Cipher) -> bytes:
    """Return the encrypted backup blob for the database at db_path."""
    return cipher.encrypt(snapshot_bytes(db_path), aad=AAD)


def _integrity_ok(db_bytes: bytes) -> bool:
    with tempfile.TemporaryDirectory() as d:
        tmp = Path(d) / "check.db"
        tmp.write_bytes(db_bytes)
        con = sqlite3.connect(str(tmp))
        try:
            row = con.execute("PRAGMA integrity_check").fetchone()
            return bool(row) and row[0] == "ok"
        except sqlite3.DatabaseError:
            return False
        finally:
            con.close()


def restore_backup(blob: bytes, db_path: str | Path, cipher: Cipher) -> None:
    """Decrypt, verify integrity, then atomically replace the database at db_path.

    Raises ValueError if the blob can't be decrypted or the decrypted database fails its integrity
    check, without touching the existing file.
    """
    try:
        data = cipher.decrypt(blob, aad=AAD)
    except Exception as e:
        raise ValueError("backup could not be decrypted (wrong key or corrupt file)") from e
    if not _integrity_ok(data):
        raise ValueError("decrypted backup failed its SQLite integrity check; not restoring")
    db_path = Path(db_path)
    db_path.parent.mkdir(parents=True, exist_ok=True)
    tmp = db_path.with_suffix(db_path.suffix + ".restore-tmp")
    tmp.write_bytes(data)
    tmp.replace(db_path)  # atomic on the same filesystem
    # WAL side files from the old database must not shadow the restored one.
    for side in ("-wal", "-shm"):
        p = Path(str(db_path) + side)
        if p.exists():
            p.unlink()
    log.info("restored database to %s (%d bytes)", db_path, len(data))


def _cipher_and_db() -> tuple[Cipher, Path]:
    from friday.config import get_settings
    from friday.vault.crypto import load_master_key

    settings = get_settings()
    return Cipher(load_master_key(settings)), settings.resolved_db_path


def main(argv: list[str] | None = None) -> int:
    """CLI for the shell wrappers: `python -m friday.backup backup <out>` / `restore <in>`."""
    import sys

    args = argv if argv is not None else sys.argv[1:]
    if len(args) != 2 or args[0] not in ("backup", "restore"):
        print("usage: python -m friday.backup backup <out-file> | restore <in-file>", file=sys.stderr)
        return 2
    cmd, path = args
    cipher, db_path = _cipher_and_db()
    if cmd == "backup":
        Path(path).write_bytes(make_backup(db_path, cipher))
        print(f"wrote encrypted backup of {db_path} to {path}")
    else:
        restore_backup(Path(path).read_bytes(), db_path, cipher)
        print(f"restored {db_path} from {path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
