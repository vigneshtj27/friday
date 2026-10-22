"""Gmail and Drive for the general assistant (Sheets lives in friday.sheets).

Thin async clients over the REST APIs, authorised with the owner's Google grant
(friday.google_oauth). Message bodies and file contents are returned to the caller
only; nothing is logged.
"""

from __future__ import annotations

import base64
import re
from email.message import EmailMessage
from typing import Any

import httpx

from friday.otp.gmail import message_text

GMAIL = "https://gmail.googleapis.com/gmail/v1/users/me"
DRIVE = "https://www.googleapis.com/drive/v3/files"
DRIVE_UPLOAD = "https://www.googleapis.com/upload/drive/v3/files"
EMAIL_RE = re.compile(r"^[^@\s<>,;]+@[^@\s<>,;]+\.[^@\s<>,;]+$")


class GoogleAPI:
    def __init__(self, auth: Any, *, transport: httpx.AsyncBaseTransport | None = None):
        self.auth = auth
        self._transport = transport

    async def req(self, capability: str, method: str, url: str, **kw: Any) -> Any:
        async with httpx.AsyncClient(timeout=60, transport=self._transport) as http:
            token = await self.auth.token_for(capability, http)
            r = await http.request(method, url, headers={"Authorization": f"Bearer {token}", **kw.pop("headers", {})}, **kw)
        if r.status_code == 404:
            raise LookupError("Google says it doesn't exist (or friday can't see it)")
        if r.status_code == 403:
            raise PermissionError("Google refused access; the grant may not cover this (reconnect Google)")
        r.raise_for_status()
        if not r.content:
            return {}
        return r.json() if "json" in r.headers.get("content-type", "") else r.content


def _header(msg: dict[str, Any], name: str) -> str:
    for h in (msg.get("payload") or {}).get("headers", []):
        if h.get("name", "").lower() == name.lower():
            return h.get("value", "")
    return ""


class Gmail:
    def __init__(self, api: GoogleAPI):
        self.api = api

    async def search(self, query: str, limit: int = 10) -> list[dict[str, Any]]:
        data = await self.api.req("gmail.read", "GET", f"{GMAIL}/messages", params={"q": query, "maxResults": min(limit, 25)})
        out = []
        for m in data.get("messages", [])[:limit]:
            full = await self.api.req("gmail.read", "GET", f"{GMAIL}/messages/{m['id']}",
                                      params={"format": "metadata", "metadataHeaders": ["From", "To", "Subject", "Date"]})  # fmt: skip
            out.append({"id": m["id"], "thread_id": full.get("threadId"), "from": _header(full, "From"),
                        "to": _header(full, "To"), "subject": _header(full, "Subject"), "date": _header(full, "Date"),
                        "snippet": full.get("snippet", "")})  # fmt: skip
        return out

    async def scan(self, query: str, limit: int = 50) -> list[dict[str, Any]]:
        """Read-only (gmail.read): full messages for a query, with plain-text bodies, for evidence
        parsing. Bodies are returned to the caller to parse in code; never sent to the model."""
        data = await self.api.req("gmail.read", "GET", f"{GMAIL}/messages", params={"q": query, "maxResults": min(limit, 100)})
        out = []
        for m in data.get("messages", [])[:limit]:
            full = await self.api.req("gmail.read", "GET", f"{GMAIL}/messages/{m['id']}", params={"format": "full"})
            out.append({"id": m["id"], "thread_id": full.get("threadId"), "from": _header(full, "From"),
                        "subject": _header(full, "Subject"), "date": _header(full, "Date"),
                        "internal_date": int(full.get("internalDate", 0)), "body": message_text(full)})  # fmt: skip
        return out

    async def read_thread(self, thread_id: str, max_chars: int = 12000) -> dict[str, Any]:
        data = await self.api.req("gmail.read", "GET", f"{GMAIL}/threads/{thread_id}", params={"format": "full"})
        msgs = []
        budget = max_chars
        for m in data.get("messages", []):
            body = message_text(m)
            body = re.sub(r"<[^>]+>", " ", body) if "<html" in body.lower() or "</div>" in body.lower() else body
            body = re.sub(r"[ \t\r\f\v]+", " ", body).strip()[: max(budget, 0)]
            budget -= len(body)
            msgs.append({"id": m["id"], "from": _header(m, "From"), "to": _header(m, "To"), "subject": _header(m, "Subject"),
                         "date": _header(m, "Date"), "body": body})  # fmt: skip
        return {"thread_id": thread_id, "messages": msgs}

    async def create_draft(self, to: list[str], subject: str, body: str, *, thread_id: str | None = None,
                           cc: list[str] | None = None) -> dict[str, Any]:  # fmt: skip
        for addr in [*to, *(cc or [])]:
            if not EMAIL_RE.match(addr):
                raise ValueError(f"{addr!r} is not an email address")
        msg = EmailMessage()
        msg["To"] = ", ".join(to)
        if cc:
            msg["Cc"] = ", ".join(cc)
        msg["Subject"] = subject
        msg.set_content(body)
        raw = base64.urlsafe_b64encode(msg.as_bytes()).decode()
        payload: dict[str, Any] = {"message": {"raw": raw}}
        if thread_id:
            payload["message"]["threadId"] = thread_id
        d = await self.api.req("gmail.write", "POST", f"{GMAIL}/drafts", json=payload)
        return {"draft_id": d["id"], "to": to, "cc": cc or [], "subject": subject}

    async def get_draft(self, draft_id: str) -> dict[str, Any]:
        d = await self.api.req("gmail.write", "GET", f"{GMAIL}/drafts/{draft_id}", params={"format": "full"})
        m = d.get("message") or {}
        return {"draft_id": draft_id, "to": _header(m, "To"), "cc": _header(m, "Cc"), "subject": _header(m, "Subject"),
                "body": message_text(m)[:2000]}  # fmt: skip

    async def send_draft(self, draft_id: str) -> dict[str, Any]:
        d = await self.api.req("gmail.write", "POST", f"{GMAIL}/drafts/send", json={"id": draft_id})
        return {"sent": True, "message_id": d.get("id"), "thread_id": d.get("threadId")}


class Drive:
    def __init__(self, api: GoogleAPI):
        self.api = api

    async def search(self, query: str | None = None, limit: int = 20) -> list[dict[str, Any]]:
        q = "trashed=false"
        if query:
            q += " and name contains '" + query.replace("\\", "").replace("'", "\\'") + "'"
        data = await self.api.req("drive", "GET", DRIVE, params={
            "q": q, "orderBy": "modifiedTime desc", "pageSize": min(limit, 50),
            "fields": "files(id,name,mimeType,modifiedTime,size,shared)"})  # fmt: skip
        return data.get("files", [])

    async def shared_with_others(self, file_id: str) -> bool:
        """True if anyone besides the owner can see the file (people, groups, domain or link)."""
        data = await self.api.req("drive.list", "GET", f"{DRIVE}/{file_id}/permissions",
                                  params={"fields": "permissions(role,type)"})  # fmt: skip
        return any(p.get("role") != "owner" for p in data.get("permissions", []))

    async def download(self, file_id: str) -> tuple[str, str, bytes]:
        meta = await self.api.req("drive", "GET", f"{DRIVE}/{file_id}", params={"fields": "name,mimeType"})
        mime = meta.get("mimeType", "")
        exports = {
            "application/vnd.google-apps.spreadsheet": ("text/csv", ".csv"),
            "application/vnd.google-apps.document": ("text/plain", ".txt"),
            "application/vnd.google-apps.presentation": ("application/pdf", ".pdf"),
        }
        if mime in exports:
            out_mime, ext = exports[mime]
            data = await self.api.req("drive", "GET", f"{DRIVE}/{file_id}/export", params={"mimeType": out_mime})
            return meta["name"] + ext, out_mime, data if isinstance(data, bytes) else str(data).encode()
        data = await self.api.req("drive", "GET", f"{DRIVE}/{file_id}", params={"alt": "media"})
        return meta["name"], mime, data if isinstance(data, bytes) else str(data).encode()

    async def upload(self, name: str, mime: str, data: bytes, *, folder_id: str | None = None) -> dict[str, Any]:
        meta: dict[str, Any] = {"name": name}
        if folder_id:
            meta["parents"] = [folder_id]
        boundary = "friday-upload-boundary"
        import json as _json

        body = (
            f"--{boundary}\r\nContent-Type: application/json; charset=UTF-8\r\n\r\n{_json.dumps(meta)}\r\n"
            f"--{boundary}\r\nContent-Type: {mime}\r\n\r\n"
        ).encode() + data + f"\r\n--{boundary}--".encode()
        d = await self.api.req("drive", "POST", DRIVE_UPLOAD, params={"uploadType": "multipart", "fields": "id,name,webViewLink"},
                               content=body, headers={"Content-Type": f"multipart/related; boundary={boundary}"})  # fmt: skip
        return {"file_id": d["id"], "name": d.get("name"), "link": d.get("webViewLink")}

    async def share(self, file_id: str, email: str, role: str) -> dict[str, Any]:
        if not EMAIL_RE.match(email):
            raise ValueError(f"{email!r} is not an email address")
        await self.api.req("drive", "POST", f"{DRIVE}/{file_id}/permissions", params={"sendNotificationEmail": "true"},
                           json={"type": "user", "role": role, "emailAddress": email})  # fmt: skip
        return {"shared": True, "file_id": file_id, "with": email, "role": role}
