"""Read-only Gmail access (scope gmail.readonly) to pick up emailed one-time codes.

OAuth: installed-app flow with PKCE and a loopback redirect, run once from the
CLI (`friday gmail connect`). The refresh token lives in the vault. Message
bodies are read only to extract the code and are not stored or logged.
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import logging
import secrets
import time
from datetime import datetime
from typing import Any
from urllib.parse import urlencode

import httpx

from friday.otp.extract import extract_otp
from friday.vault.store import SecretKind, SecretStore

log = logging.getLogger(__name__)

SCOPE = "https://www.googleapis.com/auth/gmail.readonly"
AUTH_URL = "https://accounts.google.com/o/oauth2/v2/auth"
TOKEN_URL = "https://oauth2.googleapis.com/token"
API = "https://gmail.googleapis.com/gmail/v1/users/me"
REFRESH_SECRET = "system/gmail/refresh-token"


def pkce_pair() -> tuple[str, str]:
    verifier = secrets.token_urlsafe(64)[:96]
    challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode()
    return verifier, challenge


def auth_url(client_id: str, redirect_uri: str, challenge: str, state: str) -> str:
    return AUTH_URL + "?" + urlencode(
        {
            "client_id": client_id,
            "redirect_uri": redirect_uri,
            "response_type": "code",
            "scope": SCOPE,
            "access_type": "offline",
            "prompt": "consent",
            "code_challenge": challenge,
            "code_challenge_method": "S256",
            "state": state,
        }
    )


def _walk_parts(payload: dict[str, Any]) -> list[tuple[str, str]]:
    out = []
    stack = [payload]
    while stack:
        p = stack.pop()
        stack.extend(p.get("parts") or [])
        data = (p.get("body") or {}).get("data")
        if data:
            raw = base64.urlsafe_b64decode(data + "=" * (-len(data) % 4)).decode("utf-8", "replace")
            out.append((p.get("mimeType", ""), raw))
    return out


def message_text(msg: dict[str, Any]) -> str:
    parts = _walk_parts(msg.get("payload") or {})
    plain = [t for m, t in parts if m == "text/plain"]
    if plain:
        return "\n".join(plain)
    return "\n".join(t for m, t in parts if m == "text/html") or msg.get("snippet", "")


class GmailClient:
    def __init__(self, client_id: str | None, client_secret: str | None, vault: SecretStore):
        self.client_id = client_id
        self.client_secret = client_secret
        self.vault = vault
        self._token: tuple[str, float] | None = None

    google: Any = None  # friday.google_oauth.GoogleAuth, set by the app context: a workspace grant also reads mail

    @property
    def configured(self) -> bool:
        if self.google is not None and self.google.can("gmail.read"):
            return True
        return bool(self.client_id and self.vault.exists(REFRESH_SECRET))

    async def exchange_code(self, code: str, verifier: str, redirect_uri: str) -> None:
        async with httpx.AsyncClient(timeout=30) as http:
            r = await http.post(
                TOKEN_URL,
                data={
                    "code": code,
                    "client_id": self.client_id,
                    "client_secret": self.client_secret or "",
                    "redirect_uri": redirect_uri,
                    "grant_type": "authorization_code",
                    "code_verifier": verifier,
                },
            )
        r.raise_for_status()
        data = r.json()
        if SCOPE not in data.get("scope", "").split():
            raise RuntimeError(f"Google granted {data.get('scope')!r}, expected {SCOPE}")
        if "refresh_token" not in data:
            raise RuntimeError("Google returned no refresh token; revoke the app's access and connect again")
        self.vault.put(REFRESH_SECRET, data["refresh_token"], kind=SecretKind.OTHER, actor="admin")

    async def _access_token(self, http: httpx.AsyncClient) -> str:
        if not self.vault.exists(REFRESH_SECRET) and self.google is not None:
            return await self.google.token_for("gmail.read", http)
        if self._token and self._token[1] > time.time() + 60:
            return self._token[0]
        refresh = self.vault.reveal(REFRESH_SECRET, purpose="gmail.refresh", actor="system")
        r = await http.post(
            TOKEN_URL,
            data={
                "client_id": self.client_id,
                "client_secret": self.client_secret or "",
                "refresh_token": refresh,
                "grant_type": "refresh_token",
            },
        )
        r.raise_for_status()
        data = r.json()
        self._token = (data["access_token"], time.time() + int(data.get("expires_in", 3600)))
        self.vault._redactor.register("system/gmail/access-token", data["access_token"])
        return self._token[0]

    async def wait_for_code(
        self, *, senders: list[str], since: datetime, timeout: float, fallback_terms: list[str] | None = None
    ) -> str | None:
        """Poll for a message from `senders` received after `since` and return its code."""
        if senders:
            query = "newer_than:1d from:(" + " OR ".join(senders) + ")"
        elif fallback_terms:
            query = "newer_than:1d (" + " OR ".join(f'"{t}"' for t in fallback_terms) + ") (code OR OTP OR verification)"
        else:
            return None
        since_ms = int(since.timestamp() * 1000) - 5000  # small clock-skew allowance
        seen: set[str] = set()
        deadline = time.monotonic() + timeout
        async with httpx.AsyncClient(timeout=20) as http:
            while time.monotonic() < deadline:
                try:
                    token = await self._access_token(http)
                    headers = {"Authorization": f"Bearer {token}"}
                    r = await http.get(f"{API}/messages", params={"q": query, "maxResults": 10}, headers=headers)
                    r.raise_for_status()
                    for item in r.json().get("messages", []):
                        if item["id"] in seen:
                            continue
                        seen.add(item["id"])
                        m = await http.get(f"{API}/messages/{item['id']}", params={"format": "full"}, headers=headers)
                        m.raise_for_status()
                        msg = m.json()
                        if int(msg.get("internalDate", 0)) < since_ms:
                            continue
                        code = extract_otp(message_text(msg))
                        del msg  # discard the email content
                        if code:
                            return code
                except httpx.HTTPError as e:
                    log.warning("gmail poll failed: %s", type(e).__name__)
                await asyncio.sleep(4)
        return None
