"""Encrypted-at-rest file store for downloaded statements and run screenshots."""

from __future__ import annotations

import hashlib
import mimetypes
import secrets
from dataclasses import dataclass
from pathlib import Path

from friday.audit import AuditLog
from friday.db import Database
from friday.util import iso
from friday.vault.crypto import Cipher


@dataclass(frozen=True)
class StoredFile:
    id: str
    profile_id: str | None
    run_id: str | None
    name: str
    mime: str
    size: int
    sha256: str
    kind: str
    created_at: str


class FileStore:
    def __init__(self, root: Path, db: Database, cipher: Cipher, audit: AuditLog):
        self.root = root
        self.root.mkdir(parents=True, exist_ok=True, mode=0o700)
        self._db = db
        self._cipher = cipher.subcipher("files")
        self._audit = audit

    def put(
        self,
        data: bytes,
        *,
        name: str,
        kind: str,
        profile_id: str | None = None,
        run_id: str | None = None,
        mime: str | None = None,
    ) -> StoredFile:
        file_id = secrets.token_hex(12)
        mime = mime or mimetypes.guess_type(name)[0] or "application/octet-stream"
        (self.root / file_id).write_bytes(self._cipher.encrypt(data, aad=file_id.encode()))
        (self.root / file_id).chmod(0o600)
        f = StoredFile(
            file_id, profile_id, run_id, Path(name).name[:200], mime, len(data),
            hashlib.sha256(data).hexdigest(), kind, iso(),
        )  # fmt: skip
        self._db.execute(
            "INSERT INTO files (id, profile_id, run_id, name, mime, size, sha256, kind, created_at)"
            " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (f.id, f.profile_id, f.run_id, f.name, f.mime, f.size, f.sha256, f.kind, f.created_at),
        )
        if kind != "screenshot":
            self._audit.record("file.store", target=f.id, name=f.name, size=f.size, profile=profile_id, run=run_id)
        return f

    def import_path(self, path: Path, **kwargs) -> StoredFile:
        """Encrypt a plaintext file (e.g. a fresh browser download) into the store and delete the original."""
        data = path.read_bytes()
        f = self.put(data, name=kwargs.pop("name", path.name), **kwargs)
        path.unlink(missing_ok=True)
        return f

    def meta(self, file_id: str) -> StoredFile | None:
        r = self._db.query_one("SELECT * FROM files WHERE id = ?", (file_id,))
        return StoredFile(**dict(r)) if r else None

    def read(self, file_id: str) -> bytes:
        if not all(c in "0123456789abcdef" for c in file_id):
            raise KeyError(file_id)
        return self._cipher.decrypt((self.root / file_id).read_bytes(), aad=file_id.encode())

    def list(self, *, profile_id: str | None = None, kind: str | None = None, limit: int = 200) -> list[StoredFile]:
        sql, params = "SELECT * FROM files WHERE 1=1", []
        if profile_id:
            sql += " AND profile_id = ?"
            params.append(profile_id)
        if kind:
            sql += " AND kind = ?"
            params.append(kind)
        sql += " ORDER BY created_at DESC LIMIT ?"
        params.append(limit)
        return [StoredFile(**dict(r)) for r in self._db.query(sql, params)]

    def delete_for_profile(self, profile_id: str) -> int:
        rows = self._db.query("SELECT id FROM files WHERE profile_id = ?", (profile_id,))
        for r in rows:
            (self.root / r["id"]).unlink(missing_ok=True)
        self._db.execute("DELETE FROM files WHERE profile_id = ?", (profile_id,))
        return len(rows)
