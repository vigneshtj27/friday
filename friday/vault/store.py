"""Encrypted credential store.

Values are encrypted per row with the name as associated data. Nothing here
returns a value except `reveal`, which audits the use and registers the value
with the redactor before handing it over. Only the injector and the driver
(proxy auth) are meant to call `reveal`.
"""

from __future__ import annotations

import re
import time
from dataclasses import dataclass
from enum import StrEnum

from friday.audit import AuditLog
from friday.db import Database
from friday.util import iso
from friday.vault.crypto import Cipher
from friday.vault.masking import Redactor

REF_PREFIX = "secret:"
NAME_RE = re.compile(r"^[a-z0-9][a-z0-9_.-]*(/[a-z0-9][a-z0-9_.-]*){0,4}$")


class SecretKind(StrEnum):
    USERNAME = "username"
    PASSWORD = "password"
    TOTP_SEED = "totp_seed"
    PIN = "pin"
    PROXY = "proxy"
    OTHER = "other"


class SecretNotFound(KeyError):
    pass


class InvalidSecretName(ValueError):
    pass


def validate_name(name: str) -> str:
    if len(name) > 160 or not NAME_RE.match(name):
        raise InvalidSecretName(f"invalid secret name {name!r} (use lowercase path like 'site/account/password')")
    return name


@dataclass(frozen=True)
class SecretRef:
    name: str

    @classmethod
    def parse(cls, text: str) -> SecretRef | None:
        """'secret:hdfc/me/password' -> SecretRef; anything else -> None."""
        if not text.startswith(REF_PREFIX):
            return None
        return cls(validate_name(text[len(REF_PREFIX) :]))

    @classmethod
    def coerce(cls, value: SecretRef | str) -> SecretRef:
        if isinstance(value, SecretRef):
            return value
        ref = cls.parse(value)
        if ref is None:
            raise InvalidSecretName(f"expected 'secret:<name>', got a literal string")
        return ref

    def __str__(self) -> str:
        return REF_PREFIX + self.name


@dataclass(frozen=True)
class SecretMeta:
    name: str
    kind: SecretKind
    created_at: str
    updated_at: str
    last_used_at: str | None


class SecretStore:
    def __init__(self, db: Database, cipher: Cipher, audit: AuditLog, redactor: Redactor):
        self._db = db
        self._cipher = cipher.subcipher("secrets")
        self._audit = audit
        self._redactor = redactor
        self._ephemeral: dict[str, tuple[str, float]] = {}  # name -> (value, expires monotonic)
        redactor.add_names(*(r["name"] for r in db.query("SELECT name FROM secrets")))

    def put_ephemeral(self, name: str, value: str, *, ttl_seconds: float = 300, actor: str = "system") -> None:
        """Hold a short-lived value (an OTP code) in memory only. It can be revealed once."""
        validate_name(name)
        self._redactor.register(name, value)
        self._redactor.add_names(name)
        self._ephemeral[name] = (value, time.monotonic() + ttl_seconds)
        self._audit.record("secret.ephemeral_put", actor=actor, target=name, ttl_seconds=ttl_seconds)

    def has_ephemeral(self, name: str) -> bool:
        item = self._ephemeral.get(name)
        return item is not None and item[1] > time.monotonic()

    def discard_ephemeral(self, name: str) -> None:
        self._ephemeral.pop(name, None)

    def put(self, name: str, value: str, *, kind: SecretKind = SecretKind.OTHER, actor: str = "admin") -> None:
        validate_name(name)
        if not value:
            raise ValueError("empty secret")
        self._redactor.register(name, value)
        self._redactor.add_names(name)
        blob = self._cipher.encrypt(value.encode(), aad=name.encode())
        now = iso()
        self._db.execute(
            "INSERT INTO secrets (name, kind, ciphertext, created_at, updated_at) VALUES (?, ?, ?, ?, ?)"
            " ON CONFLICT(name) DO UPDATE SET kind=excluded.kind, ciphertext=excluded.ciphertext,"
            " updated_at=excluded.updated_at",
            (name, SecretKind(kind).value, blob, now, now),
        )
        self._audit.record("secret.put", actor=actor, target=name, kind=SecretKind(kind).value)

    def exists(self, name: str) -> bool:
        return self._db.query_one("SELECT 1 FROM secrets WHERE name = ?", (name,)) is not None

    def list(self, prefix: str | None = None) -> list[SecretMeta]:
        if prefix:
            rows = self._db.query(
                "SELECT * FROM secrets WHERE substr(name, 1, ?) = ? ORDER BY name", (len(prefix), prefix)
            )
        else:
            rows = self._db.query("SELECT * FROM secrets ORDER BY name")
        return [
            SecretMeta(r["name"], SecretKind(r["kind"]), r["created_at"], r["updated_at"], r["last_used_at"])
            for r in rows
        ]

    def delete(self, name: str, *, actor: str = "admin") -> bool:
        n = self._db.execute("DELETE FROM secrets WHERE name = ?", (name,))
        self._redactor.forget(name)
        if n:
            self._audit.record("secret.delete", actor=actor, target=name)
        return bool(n)

    def delete_prefix(self, prefix: str, *, actor: str = "admin") -> int:
        if not prefix.endswith("/"):
            raise ValueError("prefix must end with '/'")
        names = [m.name for m in self.list(prefix)]
        for name in names:
            self.delete(name, actor=actor)
        return len(names)

    def reveal(self, name: str, *, purpose: str, actor: str = "agent") -> str:
        item = self._ephemeral.pop(name, None)  # single use: never reuse a code
        if item is not None:
            value, expires = item
            if expires > time.monotonic():
                self._audit.record("secret.use", actor=actor, target=name, purpose=purpose, ephemeral=True)
                return value
            self._audit.record("secret.expired", actor=actor, target=name, purpose=purpose)
            raise SecretNotFound(name)
        row = self._db.query_one("SELECT ciphertext FROM secrets WHERE name = ?", (name,))
        if row is None:
            self._audit.record("secret.missing", actor=actor, target=name, purpose=purpose)
            raise SecretNotFound(name)
        value = self._cipher.decrypt(row["ciphertext"], aad=name.encode()).decode()
        self._redactor.register(name, value)
        self._db.execute("UPDATE secrets SET last_used_at = ? WHERE name = ?", (iso(), name))
        self._audit.record("secret.use", actor=actor, target=name, purpose=purpose)
        return value
