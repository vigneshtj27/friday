"""Single-user auth: one argon2-hashed password, a signed session cookie,
and same-origin checks on anything state-changing (including WebSockets)."""

from __future__ import annotations

import asyncio
import time
from urllib.parse import urlparse

from argon2 import PasswordHasher
from argon2.exceptions import InvalidHashError, VerificationError
from fastapi import HTTPException, Request, WebSocket
from itsdangerous import BadSignature, URLSafeTimedSerializer

from friday.config import ConfigError, Settings
from friday.vault.crypto import Cipher

COOKIE = "friday_session"
_hasher = PasswordHasher()


def hash_password(password: str) -> str:
    return _hasher.hash(password)


class Auth:
    def __init__(self, settings: Settings, cipher: Cipher):
        if settings.admin_password_hash is None:
            raise ConfigError("FRIDAY_ADMIN_PASSWORD_HASH is not set; run `friday hash-password`")
        self._hash = settings.admin_password_hash.get_secret_value()
        self._ttl = settings.session_ttl_seconds
        self.cookie_secure = settings.cookie_secure
        # The session-signing key is derived from the master key; nothing extra to configure.
        self._signer = URLSafeTimedSerializer(cipher.derive_bytes("web-session").hex(), salt="friday-session")
        base = urlparse(settings.public_base_url)
        self.allowed_origins = {f"{base.scheme}://{base.netloc}", f"http://{settings.bind_host}:{settings.port}"}
        self._failures = 0
        self._last_failure = 0.0

    async def check_password(self, password: str) -> bool:
        # Exponential backoff after failures (single user, so a global counter is fine).
        if self._failures:
            wait = min(2 ** self._failures, 60) - (time.monotonic() - self._last_failure)
            if wait > 0:
                await asyncio.sleep(wait)
        try:
            ok = _hasher.verify(self._hash, password)
        except (VerificationError, InvalidHashError):
            ok = False
        if ok:
            self._failures = 0
        else:
            self._failures += 1
            self._last_failure = time.monotonic()
        return ok

    def issue(self) -> str:
        return self._signer.dumps({"u": "owner"})

    def valid(self, token: str | None) -> bool:
        if not token:
            return False
        try:
            self._signer.loads(token, max_age=self._ttl)
        except BadSignature:
            return False
        return True

    def origin_ok(self, origin: str | None, host: str | None) -> bool:
        if origin is None:
            return True  # non-browser client; the cookie is SameSite=Strict anyway
        if origin in self.allowed_origins:
            return True
        return host is not None and urlparse(origin).netloc == host

    # ---- FastAPI dependencies ----

    def require(self, request: Request) -> None:
        if not self.valid(request.cookies.get(COOKIE)):
            raise HTTPException(401, "login required")
        if request.method not in ("GET", "HEAD", "OPTIONS") and not self.origin_ok(
            request.headers.get("origin"), request.headers.get("host")
        ):
            raise HTTPException(403, "cross-origin request refused")

    def websocket_ok(self, ws: WebSocket) -> bool:
        return self.valid(ws.cookies.get(COOKIE)) and self.origin_ok(
            ws.headers.get("origin"), ws.headers.get("host")
        )
