"""Google OAuth for the owner, from the portal (no terminal on the server needed).

Grants (each feature uses the first connected grant that covers it, see CAPABILITIES):
  * workspace - the general assistant: Sheets, Gmail read/compose/send (send is approval-gated in
                code), Drive read + files friday creates
  * gmail     - gmail.readonly only, for emailed one-time codes (friday.otp.gmail)
  * sheets    - spreadsheets + drive.metadata.readonly (the narrower grant from before)

Refresh tokens live in the vault under system/google/..., a namespace no site
profile can reach (the "system" site id is reserved), so the injector can never
type one into a page, and they never enter model context.

Flow: /oauth/google/start (logged-in owner) -> Google consent -> /oauth/google/callback.
The callback arrives as a cross-site navigation, so the SameSite=Strict session
cookie is not sent; it is authorised by the single-use, short-lived state value
that only the logged-in owner's /start could have produced. PKCE binds the code
to this server.
"""

from __future__ import annotations

import secrets
import time
from dataclasses import dataclass
from typing import Any

import httpx

from friday.otp import gmail as gmail_mod
from friday.vault.store import SecretKind, SecretStore

TOKEN_URL = gmail_mod.TOKEN_URL
AUTH_URL = gmail_mod.AUTH_URL

PURPOSES: dict[str, dict[str, Any]] = {
    "gmail": {"scopes": [gmail_mod.SCOPE], "secret": gmail_mod.REFRESH_SECRET, "label": "Gmail (read-only, for codes)"},
    "sheets": {
        "scopes": ["https://www.googleapis.com/auth/spreadsheets", "https://www.googleapis.com/auth/drive.metadata.readonly"],
        "secret": "system/google/sheets-refresh-token",
        "label": "Google Sheets",
    },
    "workspace": {
        "scopes": [
            "https://www.googleapis.com/auth/spreadsheets",
            "https://www.googleapis.com/auth/gmail.modify",
            "https://www.googleapis.com/auth/drive.readonly",
            "https://www.googleapis.com/auth/drive.file",
        ],
        "secret": "system/google/oauth-token",
        "label": "Google (Sheets, Gmail, Drive)",
    },
}
# capability -> grants that cover it, preferred first
CAPABILITIES: dict[str, list[str]] = {
    "sheets": ["workspace", "sheets"],
    "drive.list": ["workspace", "sheets"],
    "drive": ["workspace"],
    "gmail.read": ["workspace", "gmail"],
    "gmail.write": ["workspace"],
}
STATE_TTL = 600


@dataclass
class Pending:
    purpose: str
    verifier: str
    redirect_uri: str
    created: float


class GoogleAuth:
    def __init__(self, client_id: str | None, client_secret: str | None, vault: SecretStore):
        self.client_id = client_id
        self.client_secret = client_secret
        self.vault = vault
        self._pending: dict[str, Pending] = {}
        self._tokens: dict[str, tuple[str, float]] = {}

    @property
    def available(self) -> bool:
        return bool(self.client_id)

    def connected(self, purpose: str) -> bool:
        return bool(self.client_id and self.vault.exists(PURPOSES[purpose]["secret"]))

    def grant_for(self, capability: str) -> str | None:
        """The first connected grant that covers a capability (see CAPABILITIES), or None."""
        return next((g for g in CAPABILITIES[capability] if self.connected(g)), None)

    def can(self, capability: str) -> bool:
        return self.grant_for(capability) is not None

    async def token_for(self, capability: str, http: httpx.AsyncClient) -> str:
        grant = self.grant_for(capability)
        if grant is None:
            raise PermissionError(f"Google isn't connected for {capability}: connect Google (workspace) first")
        return await self.access_token(grant, http)

    def start(self, purpose: str, redirect_uri: str) -> str:
        if purpose not in PURPOSES:
            raise ValueError(f"unknown purpose {purpose!r}")
        if not self.client_id:
            raise ValueError("set FRIDAY_GOOGLE_CLIENT_ID and FRIDAY_GOOGLE_CLIENT_SECRET first")
        now = time.time()
        self._pending = {k: v for k, v in self._pending.items() if now - v.created < STATE_TTL}
        verifier, challenge = gmail_mod.pkce_pair()
        state = secrets.token_urlsafe(24)
        self._pending[state] = Pending(purpose, verifier, redirect_uri, now)
        from urllib.parse import urlencode

        return AUTH_URL + "?" + urlencode({
            "client_id": self.client_id, "redirect_uri": redirect_uri, "response_type": "code",
            "scope": " ".join(PURPOSES[purpose]["scopes"]), "access_type": "offline", "prompt": "consent",
            "include_granted_scopes": "false", "code_challenge": challenge, "code_challenge_method": "S256", "state": state,
        })  # fmt: skip

    async def finish(self, state: str, code: str, *, http: httpx.AsyncClient | None = None) -> str:
        """Exchange the code; returns the purpose that was connected."""
        p = self._pending.pop(state, None)
        if p is None or time.time() - p.created > STATE_TTL:
            raise ValueError("this sign-in link expired or was already used; start again")
        own = http is None
        http = http or httpx.AsyncClient(timeout=30)
        try:
            r = await http.post(TOKEN_URL, data={
                "code": code, "client_id": self.client_id, "client_secret": self.client_secret or "",
                "redirect_uri": p.redirect_uri, "grant_type": "authorization_code", "code_verifier": p.verifier,
            })  # fmt: skip
        finally:
            if own:
                await http.aclose()
        r.raise_for_status()
        data = r.json()
        granted = set(data.get("scope", "").split())
        missing = [s for s in PURPOSES[p.purpose]["scopes"] if s not in granted]
        if missing:
            raise ValueError(f"Google did not grant {', '.join(missing)}; tick every box on the consent screen")
        if "refresh_token" not in data:
            raise ValueError("Google returned no refresh token; remove friday at myaccount.google.com/permissions and connect again")
        self.vault.put(PURPOSES[p.purpose]["secret"], data["refresh_token"], kind=SecretKind.OTHER, actor="admin")
        self._tokens.pop(p.purpose, None)
        return p.purpose

    async def access_token(self, purpose: str, http: httpx.AsyncClient) -> str:
        tok = self._tokens.get(purpose)
        if tok and tok[1] > time.time() + 60:
            return tok[0]
        refresh = self.vault.reveal(PURPOSES[purpose]["secret"], purpose=f"google.{purpose}.refresh", actor="system")
        r = await http.post(TOKEN_URL, data={
            "client_id": self.client_id, "client_secret": self.client_secret or "",
            "refresh_token": refresh, "grant_type": "refresh_token",
        })  # fmt: skip
        r.raise_for_status()
        data = r.json()
        self._tokens[purpose] = (data["access_token"], time.time() + int(data.get("expires_in", 3600)))
        self.vault._redactor.register(f"system/google/{purpose}-access-token", data["access_token"])
        return data["access_token"]
