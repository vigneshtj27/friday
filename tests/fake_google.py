"""An in-memory Google (Sheets, Drive, Gmail) behind httpx.MockTransport, for tool and planner tests.

Like a real sheet in an en-IN locale, values written with USER_ENTERED come back formatted:
dates as dd/mm/yyyy, numbers with thousands separators, a leading apostrophe hidden.
"""

from __future__ import annotations

import base64
import json
import re
from decimal import Decimal
from email import message_from_bytes
from typing import Any
from urllib.parse import unquote

import httpx

SID = "1AbCdEfGhIjKlMnOpQrStUvWxYz0123456789"


def display(v: Any) -> str:
    v = str(v)
    if v.startswith("'"):
        return v[1:]
    m = re.fullmatch(r"(\d{4})-(\d{2})-(\d{2})", v)
    if m:
        return f"{m.group(3)}/{m.group(2)}/{m.group(1)}"
    if re.fullmatch(r"-?\d+(\.\d+)?", v):
        return f"{Decimal(v):,.2f}"
    return v


class FakeGoogle:
    def __init__(self, sheets: dict[str, dict[str, list[list[str]]]] | None = None, titles: dict[str, str] | None = None,
                 shared: set[str] | None = None, mail: list[dict[str, Any]] | None = None):  # fmt: skip
        self.sheets = {sid: {t: [list(r) for r in rows] for t, rows in tabs.items()} for sid, tabs in (sheets or {}).items()}
        self.titles = titles or {}
        self.shared = shared or set()
        self.mail = mail or []  # {"id","threadId","from","to","subject","date","body"}
        self.drafts: dict[str, dict[str, Any]] = {}
        self.sent: list[dict[str, Any]] = []
        self.calls: list[tuple[str, str]] = []
        self.files: dict[str, dict[str, Any]] = {}

    @property
    def transport(self) -> httpx.MockTransport:
        return httpx.MockTransport(self.handler)

    def _sheet_ids(self, sid: str) -> dict[str, int]:
        return {t: i for i, t in enumerate(self.sheets[sid])}

    def handler(self, request: httpx.Request) -> httpx.Response:
        assert request.headers["authorization"] == "Bearer tok"
        path = unquote(request.url.path)
        self.calls.append((request.method, path))
        # ---- Drive
        if path == "/drive/v3/files":
            q = request.url.params.get("q", "")
            m = re.search(r"name contains '([^']*)'", q)
            files = [{"id": sid, "name": self.titles.get(sid, sid), "mimeType": "application/vnd.google-apps.spreadsheet",
                      "modifiedTime": "2026-10-19T10:00:00Z"} for sid in self.sheets]  # fmt: skip
            files += list(self.files.values())
            if m:
                files = [f for f in files if m.group(1).lower() in f["name"].lower()]
            return httpx.Response(200, json={"files": files})
        m = re.match(r"/drive/v3/files/([^/]+)/permissions$", path)
        if m:
            if request.method == "POST":
                self.shared.add(m.group(1))
                return httpx.Response(200, json={"id": "perm1"})
            perms = [{"role": "owner", "type": "user"}] + ([{"role": "writer", "type": "user"}] if m.group(1) in self.shared else [])
            return httpx.Response(200, json={"permissions": perms})
        if path == "/upload/drive/v3/files":
            fid = f"f{len(self.files) + 1}"
            meta = json.loads(request.content.split(b"\r\n\r\n", 1)[1].split(b"\r\n", 1)[0])
            self.files[fid] = {"id": fid, "name": meta["name"], "mimeType": "text/csv", "parents": meta.get("parents")}
            return httpx.Response(200, json={"id": fid, "name": meta["name"], "webViewLink": f"https://drive/{fid}"})
        # ---- Sheets
        m = re.match(r"/v4/spreadsheets/([^/:]+)/values/'(.+?)'(!A1)?(:append)?$", path)
        if m:
            sid, tab = m.group(1), m.group(2).replace("''", "'")
            if sid not in self.sheets or tab not in self.sheets[sid]:
                return httpx.Response(404)
            rows = self.sheets[sid][tab]
            if m.group(4):
                assert request.url.params["insertDataOption"] == "INSERT_ROWS"
                new = json.loads(request.content)["values"]
                last = max((i for i, r in enumerate(rows) if any(str(c).strip() for c in r)), default=-1)
                rows[last + 1 : last + 1] = [[display(c) for c in r] for r in new]
                return httpx.Response(200, json={"updates": {"updatedRows": len(new)}})
            return httpx.Response(200, json={"values": rows})
        m = re.match(r"/v4/spreadsheets/([^/:]+):batchUpdate$", path)
        if m:
            sid = m.group(1)
            replies = []
            for req in json.loads(request.content)["requests"]:
                if "addSheet" in req:
                    title = req["addSheet"]["properties"]["title"]
                    self.sheets[sid][title] = []
                    replies.append({"addSheet": {"properties": {"title": title, "sheetId": self._sheet_ids(sid)[title]}}})
                elif "deleteDimension" in req:
                    r = req["deleteDimension"]["range"]
                    tab = next(t for t, i in self._sheet_ids(sid).items() if i == r["sheetId"])
                    del self.sheets[sid][tab][r["startIndex"] : r["endIndex"]]
                    replies.append({})
            return httpx.Response(200, json={"replies": replies})
        m = re.match(r"/v4/spreadsheets/([^/:]+)$", path)
        if m:
            sid = m.group(1)
            if sid not in self.sheets:
                return httpx.Response(404)
            ids = self._sheet_ids(sid)
            return httpx.Response(200, json={"properties": {"title": self.titles.get(sid, sid)},
                                             "sheets": [{"properties": {"title": t, "sheetId": ids[t], "gridProperties": {"rowCount": 1000}}}
                                                        for t in self.sheets[sid]]})  # fmt: skip
        # ---- Gmail
        if path == "/gmail/v1/users/me/messages":
            q = request.url.params.get("q", "").lower()
            # from:(a OR b) -> match any of those senders; after:/before:/newer_than: are ignored
            # (tests control the mail set); remaining free words must all appear.
            senders: list[str] = []
            fm = re.search(r"from:\(([^)]*)\)|from:(\S+)", q)
            if fm:
                senders = re.split(r"\s+or\s+", (fm.group(1) or fm.group(2)))
            rest = re.sub(r"(from:\([^)]*\)|from:\S+|subject:|after:\S+|before:\S+|newer_than:\S+)", " ", q)
            words = [w for w in re.findall(r"[a-z0-9]+", rest) if len(w) > 2]
            hits = []
            for x in self.mail:
                frm = x["from"].lower()
                if senders and not any(s.strip() and s.strip() in frm for s in senders):
                    continue
                if not all(w in (x["from"] + x["subject"] + x["body"]).lower() for w in words):
                    continue
                hits.append(x)
            return httpx.Response(200, json={"messages": [{"id": x["id"], "threadId": x["threadId"]} for x in hits]})
        m = re.match(r"/gmail/v1/users/me/messages/([^/]+)$", path)
        if m:
            x = next(x for x in self.mail if x["id"] == m.group(1))
            return httpx.Response(200, json=self._gmsg(x))
        m = re.match(r"/gmail/v1/users/me/threads/([^/]+)$", path)
        if m:
            msgs = [self._gmsg(x) for x in self.mail if x["threadId"] == m.group(1)]
            return httpx.Response(200, json={"id": m.group(1), "messages": msgs})
        if path == "/gmail/v1/users/me/drafts" and request.method == "POST":
            raw = json.loads(request.content)["message"]["raw"]
            msg = message_from_bytes(base64.urlsafe_b64decode(raw))
            did = f"d{len(self.drafts) + 1}"
            self.drafts[did] = {"to": msg["To"], "cc": msg["Cc"] or "", "subject": msg["Subject"], "body": msg.get_payload()}
            return httpx.Response(200, json={"id": did})
        m = re.match(r"/gmail/v1/users/me/drafts/([^/]+)$", path)
        if m and m.group(1) != "send":
            d = self.drafts[m.group(1)]
            return httpx.Response(200, json={"id": m.group(1), "message": self._gmsg(
                {"id": "dm", "threadId": "dt", "from": "me", "to": d["to"], "subject": d["subject"], "body": d["body"], "date": ""})})
        if path == "/gmail/v1/users/me/drafts/send":
            did = json.loads(request.content)["id"]
            self.sent.append(self.drafts.pop(did))
            return httpx.Response(200, json={"id": "sent1", "threadId": "t-sent"})
        return httpx.Response(404)

    @staticmethod
    def _gmsg(x: dict[str, Any]) -> dict[str, Any]:
        data = base64.urlsafe_b64encode(x["body"].encode()).decode()
        headers = [{"name": "From", "value": x["from"]}, {"name": "To", "value": x.get("to", "me@example.com")},
                   {"name": "Subject", "value": x["subject"]}, {"name": "Date", "value": x.get("date", "")}]  # fmt: skip
        return {"id": x["id"], "threadId": x["threadId"], "snippet": x["body"][:80], "internalDate": "0",
                "payload": {"mimeType": "text/plain", "headers": headers, "body": {"data": data}}}


class FakeAuth:
    def __init__(self, can: bool = True):
        self._can = can
        self.available = True

    def connected(self, purpose: str) -> bool:
        return self._can

    def can(self, capability: str) -> bool:
        return self._can

    async def access_token(self, purpose: str, http: Any) -> str:
        return "tok"

    async def token_for(self, capability: str, http: Any) -> str:
        if not self._can:
            raise PermissionError("Google isn't connected")
        return "tok"


def install(ctx: Any, fake: FakeGoogle) -> None:
    """Point ctx's Sheets, Gmail and Drive clients at the fake."""
    from types import SimpleNamespace

    from friday.sheets import SheetsClient
    from friday.workspace import Drive, Gmail, GoogleAPI

    auth = FakeAuth()
    ctx.google = auth
    ctx.sheets = SheetsClient(auth, transport=fake.transport)
    api = GoogleAPI(auth, transport=fake.transport)
    ctx.workspace = SimpleNamespace(api=api, gmail=Gmail(api), drive=Drive(api))
