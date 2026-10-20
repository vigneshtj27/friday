"""Sheets, Gmail and Drive as tools. Reads are free; writes follow friday.tools.base."""

from __future__ import annotations

import json
from datetime import date
from typing import Any

from friday.sheets import (
    SheetsError,
    cells_match,
    export_transactions,
    find_header,
    map_columns,
    safe_text,
    spreadsheet_id,
)
from friday.tools.base import Call, Tool, ToolError
from friday.util import iso

SID = {"type": "string", "description": "spreadsheet id or URL (sheets_list finds it by name)"}
DATE = {"type": "string", "description": "YYYY-MM-DD"}


def _sid(args: dict[str, Any]) -> str:
    try:
        return spreadsheet_id(str(args["spreadsheet"]))
    except SheetsError as e:
        raise ToolError(str(e)) from e


async def sheet_write_check(sid: str, call: Call) -> str | None:
    """Approval needed when the sheet isn't one the owner named or is shared with others."""
    ctx = call.ctx
    try:
        title = await ctx.sheets.title(sid)
    except Exception:
        title = ""
    reasons = []
    if not call.is_trusted(sid, title):
        reasons.append(f"write to the spreadsheet '{title or sid}', which you didn't name")
    try:
        shared = await ctx.workspace.drive.shared_with_others(sid)
    except Exception:
        shared = None  # couldn't check: ask rather than assume private
    if shared is None:
        reasons.append(f"write to '{title or sid}' (couldn't check whether it is shared)")
    elif shared:
        reasons.append(f"write to '{title or sid}', which other people can see")
    return "; ".join(reasons) or None


class SheetsList(Tool):
    name = "sheets_list"
    description = "The owner's Google spreadsheets, newest first."
    schema = {"type": "object", "properties": {"query": {"type": "string", "description": "part of the name"}}}
    untrusted_output = True

    async def run(self, args: dict[str, Any], call: Call) -> Any:
        return {"spreadsheets": await call.ctx.sheets.list_spreadsheets(args.get("query"))}


class SheetsRead(Tool):
    name = "sheets_read"
    description = ("A spreadsheet's tabs (no tab given), or a tab's rows: header, row count and up to `limit` data rows "
                   "(all rows if limit is 0). Use table_compute for totals.")
    schema = {"type": "object", "properties": {"spreadsheet": SID, "tab": {"type": "string"},
                                               "limit": {"type": "integer"}}, "required": ["spreadsheet"]}  # fmt: skip
    untrusted_output = True

    async def run(self, args: dict[str, Any], call: Call) -> Any:
        sid = _sid(args)
        if not args.get("tab"):
            return {"spreadsheet": sid, "title": await call.ctx.sheets.title(sid), "tabs": await call.ctx.sheets.tabs(sid)}
        values = await call.ctx.sheets.read(sid, args["tab"])
        try:
            h = find_header(values)
        except SheetsError:
            h = 0
        data = [r for r in values[h + 1 :] if any(str(c).strip() for c in r)]
        limit = int(args.get("limit", 30))
        return {"spreadsheet": sid, "tab": args["tab"], "header": values[h] if values else [], "data_rows": len(data),
                "rows": data if limit == 0 else data[-limit:]}  # fmt: skip


class SheetsAppendRows(Tool):
    name = "sheets_append_rows"
    description = ("Append rows below a tab's table. Each row is an object keyed by the tab's column names; columns not "
                   "given stay blank. Never overwrites; note/comment columns can't be written. Reads the tab back to verify.")
    schema = {"type": "object", "properties": {
        "spreadsheet": SID, "tab": {"type": "string"},
        "rows": {"type": "array", "items": {"type": "object", "additionalProperties": {"type": ["string", "number", "null"]}}},
    }, "required": ["spreadsheet", "tab", "rows"]}  # fmt: skip
    effect = "write"

    async def precheck(self, args: dict[str, Any], call: Call) -> str | None:
        return await sheet_write_check(_sid(args), call)

    async def run(self, args: dict[str, Any], call: Call) -> Any:
        ctx, sid, tab = call.ctx, _sid(args), args["tab"]
        if not args["rows"]:
            raise ToolError("no rows to append")
        values = await ctx.sheets.read(sid, tab)
        h = find_header(values)
        header = [str(c).strip() for c in values[h]]
        lower = {c.lower(): i for i, c in enumerate(header) if c}
        notes = {c.lower() for c in header if any(k in c.lower() for k in ("note", "comment", "remark"))}
        out_rows = []
        for r in args["rows"]:
            row = [""] * len(header)
            for k, v in r.items():
                key = k.strip().lower()
                if key not in lower:
                    raise ToolError(f"the tab has no column {k!r}; its columns are {header}")
                if key in notes:
                    raise ToolError(f"{k!r} is a note/comment column; friday leaves those to you")
                row[lower[key]] = "" if v is None else (v if isinstance(v, int | float) else safe_text(str(v)))
            out_rows.append(row)
        before = sum(1 for r in values[h + 1 :] if any(str(c).strip() for c in r))
        await ctx.sheets.append(sid, tab, out_rows)
        for row in out_rows:
            ctx.db.execute("INSERT INTO sheet_rows (spreadsheet, tab, values_json, plan_id, created_at) VALUES (?, ?, ?, ?, ?)",
                           (sid, tab, json.dumps(row), call.plan_id, iso()))  # fmt: skip
        ctx.audit.record("sheets.append", actor="assistant", target=sid, tab=tab, rows=len(out_rows))
        after = await ctx.sheets.read(sid, tab)
        data_after = [r for r in after[h + 1 :] if any(str(c).strip() for c in r)]
        found = sum(1 for row in out_rows if any(cells_match(row, shown) for shown in data_after))
        ok = found == len(out_rows) and len(data_after) >= before + len(out_rows)
        return {"appended": len(out_rows), "found_after": found, "rows_before": before, "rows_after": len(data_after), "ok": ok,
                "verified": [f"read back {found} of {len(out_rows)} new row(s) in '{tab}' ({before} -> {len(data_after)} rows)"] if ok else []}  # fmt: skip


class SheetsRemoveAppended(Tool):
    name = "sheets_remove_appended"
    description = ("Remove rows that friday itself appended to a tab (the most recent `count`, default 1). Rows are deleted "
                   "only if they still hold exactly what friday wrote; nothing else can be removed.")
    schema = {"type": "object", "properties": {"spreadsheet": SID, "tab": {"type": "string"}, "count": {"type": "integer"}},
              "required": ["spreadsheet", "tab"]}  # fmt: skip
    effect = "write"

    async def precheck(self, args: dict[str, Any], call: Call) -> str | None:
        return await sheet_write_check(_sid(args), call)

    async def run(self, args: dict[str, Any], call: Call) -> Any:
        ctx, sid, tab = call.ctx, _sid(args), args["tab"]
        n = max(1, int(args.get("count") or 1))
        ledger = ctx.db.query("SELECT id, values_json FROM sheet_rows WHERE spreadsheet = ? AND tab = ? AND removed_at IS NULL"
                              " ORDER BY id DESC LIMIT ?", (sid, tab, n))  # fmt: skip
        if not ledger:
            raise ToolError("friday hasn't appended any rows to that tab that are still there")
        values = await ctx.sheets.read(sid, tab)
        tabs = {t["title"]: t for t in await ctx.sheets.tabs(sid)}
        if tab not in tabs:
            raise ToolError(f"no tab {tab!r}")
        taken: set[int] = set()
        to_delete, done = [], []
        for entry in ledger:
            written = json.loads(entry["values_json"])
            idx = next((i for i in range(len(values) - 1, 0, -1) if i not in taken and cells_match(written, values[i])), None)
            if idx is None:
                continue  # edited or already gone: leave it
            taken.add(idx)
            to_delete.append(idx)
            done.append(entry["id"])
        if not to_delete:
            raise ToolError("the rows friday appended were changed or removed already; nothing deleted")
        await ctx.sheets.delete_rows(sid, int(tabs[tab]["sheet_id"]), to_delete)
        for i in done:
            ctx.db.execute("UPDATE sheet_rows SET removed_at = ? WHERE id = ?", (iso(), i))
        ctx.audit.record("sheets.remove_appended", actor="assistant", target=sid, tab=tab, rows=len(to_delete))
        after = await ctx.sheets.read(sid, tab)
        ok = len(after) == len(values) - len(to_delete)
        return {"removed": len(to_delete), "rows_before": len(values), "rows_after": len(after), "ok": ok,
                "skipped_changed": len(ledger) - len(done),
                "verified": [f"'{tab}' read back with {len(to_delete)} fewer row(s)"] if ok else []}  # fmt: skip


class SheetsCreateTab(Tool):
    name = "sheets_create_tab"
    description = "Add a new tab to a spreadsheet, optionally with a header row."
    schema = {"type": "object", "properties": {"spreadsheet": SID, "tab": {"type": "string"},
                                               "header": {"type": "array", "items": {"type": "string"}}},
              "required": ["spreadsheet", "tab"]}  # fmt: skip
    effect = "write"

    async def precheck(self, args: dict[str, Any], call: Call) -> str | None:
        return await sheet_write_check(_sid(args), call)

    async def run(self, args: dict[str, Any], call: Call) -> Any:
        sid = _sid(args)
        if args["tab"] in {t["title"] for t in await call.ctx.sheets.tabs(sid)}:
            raise ToolError(f"there is already a tab called {args['tab']!r}")
        out = await call.ctx.sheets.add_tab(sid, args["tab"])
        if args.get("header"):
            await call.ctx.sheets.append(sid, args["tab"], [[safe_text(h) for h in args["header"]]])
        return {**out, "created": True}


class SheetsAddTransactions(Tool):
    name = "sheets_add_transactions"
    description = ("Append an account's stored transactions for a date range to a tab, matching its columns, skipping rows "
                   "already there, charges only unless include_credits. Defaults to the saved expense sheet. Reads back "
                   "and reports row count and total.")
    schema = {"type": "object", "properties": {
        "account": {"type": "string"}, "start": DATE, "end": DATE, "spreadsheet": SID, "tab": {"type": "string"},
        "include_credits": {"type": "boolean"},
    }, "required": ["account", "start", "end"]}  # fmt: skip
    effect = "write"

    def _target(self, args: dict[str, Any], call: Call) -> dict[str, Any]:
        saved = call.ctx.prefs.get("expense_sheet") or {}
        if args.get("spreadsheet"):
            return {"spreadsheet": _sid(args), "tab": args.get("tab") or saved.get("tab"), "column_map": None,
                    "include_credits": args.get("include_credits", False)}  # fmt: skip
        if not saved:
            raise ToolError("no expense sheet saved yet: find it with sheets_list and save it with sheets_set_expense_sheet")
        return {**saved, "include_credits": args.get("include_credits", saved.get("include_credits", False))}

    async def precheck(self, args: dict[str, Any], call: Call) -> str | None:
        return await sheet_write_check(self._target(args, call)["spreadsheet"], call)

    async def run(self, args: dict[str, Any], call: Call) -> Any:
        t = self._target(args, call)
        if not t.get("tab"):
            raise ToolError("which tab? pass tab")
        account = call.ctx.chat.assistant._profile(args["account"]).id if call.ctx.chat.assistant else args["account"]
        await call.say(f"Writing {account}'s transactions to '{t['tab']}'")
        v = await export_transactions(call.ctx, spreadsheet=t["spreadsheet"], tab=t["tab"], account=account,
                                      start=date.fromisoformat(args["start"]), end=date.fromisoformat(args["end"]),
                                      include_credits=t.get("include_credits", False), column_map=t.get("column_map"))  # fmt: skip
        if v["ok"]:
            v["verified"] = [
                f"read back all {v['appended']} appended row(s) in '{t['tab']}' ({v['rows_before']} -> {v['rows_after']} rows)",
                f"{v['rows_in_range']} rows dated {args['start']}..{args['end']} in the tab total {v['total_in_range']}",
            ]
        return v


class SheetsSetExpenseSheet(Tool):
    name = "sheets_set_expense_sheet"
    description = "Save which tab expenses go to (checked: it needs Date, Description and Amount columns)."
    schema = {"type": "object", "properties": {"spreadsheet": SID, "tab": {"type": "string"}, "include_credits": {"type": "boolean"},
                                               "column_map": {"type": "object", "additionalProperties": {"type": "string"}}},
              "required": ["spreadsheet", "tab"]}  # fmt: skip
    effect = "write"
    needs_owner_quote = True

    async def run(self, args: dict[str, Any], call: Call) -> Any:
        ctx, sid = call.ctx, _sid(args)
        values = await ctx.sheets.read(sid, args["tab"])
        h = find_header(values)
        mapping = map_columns([str(c) for c in values[h]], args.get("column_map"))
        target = {"spreadsheet": sid, "tab": args["tab"], "include_credits": bool(args.get("include_credits")),
                  "column_map": args.get("column_map") or None}  # fmt: skip
        ctx.prefs.set("expense_sheet", target)
        title = await ctx.sheets.title(sid)
        ctx.memory.remember(f"The expense sheet is '{title}' (spreadsheet {sid}), tab '{args['tab']}'.", kind="fact",
                            subject="expense sheet", source="owner")  # fmt: skip
        return {"saved": target, "title": title, "columns": {str(values[h][i]): f for i, f in mapping.items()}}


# ---- Gmail ------------------------------------------------------------------------------------


class GmailSearch(Tool):
    name = "gmail_search"
    description = "Search the owner's mail with Gmail syntax (from:, subject:, newer_than:7d, ...). Returns headers and snippets."
    schema = {"type": "object", "properties": {"query": {"type": "string"}, "limit": {"type": "integer"}}, "required": ["query"]}
    untrusted_output = True

    async def run(self, args: dict[str, Any], call: Call) -> Any:
        return {"messages": await call.ctx.workspace.gmail.search(args["query"], int(args.get("limit") or 10))}


class GmailRead(Tool):
    name = "gmail_read_thread"
    description = "Read a thread's messages (plain text, trimmed)."
    schema = {"type": "object", "properties": {"thread_id": {"type": "string"}}, "required": ["thread_id"]}
    untrusted_output = True

    async def run(self, args: dict[str, Any], call: Call) -> Any:
        return await call.ctx.workspace.gmail.read_thread(args["thread_id"])


class GmailDraft(Tool):
    name = "gmail_create_draft"
    description = "Create a draft in the owner's Gmail (private; nothing is sent). Use gmail_send to send it."
    schema = {"type": "object", "properties": {
        "to": {"type": "array", "items": {"type": "string"}}, "cc": {"type": "array", "items": {"type": "string"}},
        "subject": {"type": "string", "maxLength": 300}, "body": {"type": "string", "maxLength": 20000},
        "thread_id": {"type": "string", "description": "to reply in a thread"},
    }, "required": ["to", "subject", "body"]}  # fmt: skip
    effect = "write"

    async def run(self, args: dict[str, Any], call: Call) -> Any:
        try:
            return await call.ctx.workspace.gmail.create_draft(args["to"], args["subject"], args["body"],
                                                             thread_id=args.get("thread_id"), cc=args.get("cc"))  # fmt: skip
        except ValueError as e:
            raise ToolError(str(e)) from e


class GmailSend(Tool):
    name = "gmail_send"
    description = "Send a draft as the owner. Always waits for the owner's approval card first."
    schema = {"type": "object", "properties": {"draft_id": {"type": "string"}}, "required": ["draft_id"]}
    effect = "send"

    async def precheck(self, args: dict[str, Any], call: Call) -> str | None:
        d = await call.ctx.workspace.gmail.get_draft(args["draft_id"])
        body = " ".join(d["body"].split())[:160]
        return f"Send email to {d['to']}{' cc ' + d['cc'] if d['cc'] else ''}, subject \"{d['subject']}\": \"{body}\""

    async def run(self, args: dict[str, Any], call: Call) -> Any:
        return await call.ctx.workspace.gmail.send_draft(args["draft_id"])


# ---- Drive ------------------------------------------------------------------------------------


class DriveSearch(Tool):
    name = "drive_search"
    description = "Find files in the owner's Google Drive by name."
    schema = {"type": "object", "properties": {"query": {"type": "string"}, "limit": {"type": "integer"}}}
    untrusted_output = True

    async def run(self, args: dict[str, Any], call: Call) -> Any:
        return {"files": await call.ctx.workspace.drive.search(args.get("query"), int(args.get("limit") or 20))}


class DriveDownload(Tool):
    name = "drive_download"
    description = "Copy a Drive file into friday's encrypted file store (Google Sheets/Docs are exported). Returns file_id for files_read."
    schema = {"type": "object", "properties": {"drive_file_id": {"type": "string"}}, "required": ["drive_file_id"]}

    async def run(self, args: dict[str, Any], call: Call) -> Any:
        name, mime, data = await call.ctx.workspace.drive.download(args["drive_file_id"])
        f = call.ctx.files.put(data, name=name, kind="drive", mime=mime)
        return {"file_id": f.id, "name": f.name, "mime": f.mime, "size": f.size}


class DriveUpload(Tool):
    name = "drive_upload"
    description = "Upload a file from friday's file store to the owner's Drive (private unless the folder is shared)."
    schema = {"type": "object", "properties": {"file_id": {"type": "string"}, "name": {"type": "string"},
                                               "folder_id": {"type": "string"}}, "required": ["file_id"]}  # fmt: skip
    effect = "write"

    async def precheck(self, args: dict[str, Any], call: Call) -> str | None:
        if not args.get("folder_id"):
            return None  # My Drive root: private
        try:
            shared = await call.ctx.workspace.drive.shared_with_others(args["folder_id"])
        except Exception:
            shared = True
        return "upload into a Drive folder other people can see" if shared else None

    async def run(self, args: dict[str, Any], call: Call) -> Any:
        meta = call.ctx.files.meta(args["file_id"])
        if meta is None:
            raise ToolError("no such stored file")
        return await call.ctx.workspace.drive.upload(args.get("name") or meta.name, meta.mime, call.ctx.files.read(meta.id),
                                                     folder_id=args.get("folder_id"))  # fmt: skip


class DriveShare(Tool):
    name = "drive_share"
    description = "Share a Drive file with someone (they get an email). Always waits for the owner's approval."
    schema = {"type": "object", "properties": {"drive_file_id": {"type": "string"}, "email": {"type": "string"},
                                               "role": {"type": "string", "enum": ["reader", "commenter", "writer"]}},
              "required": ["drive_file_id", "email", "role"]}  # fmt: skip
    effect = "share"

    def approval(self, args: dict[str, Any], call: Call) -> str | None:
        return f"Share Drive file {args['drive_file_id']} with {args['email']} as {args['role']}"

    async def run(self, args: dict[str, Any], call: Call) -> Any:
        try:
            return await call.ctx.workspace.drive.share(args["drive_file_id"], args["email"], args["role"])
        except ValueError as e:
            raise ToolError(str(e)) from e


TOOLS = [SheetsList(), SheetsRead(), SheetsAppendRows(), SheetsRemoveAppended(), SheetsCreateTab(), SheetsAddTransactions(),
         SheetsSetExpenseSheet(), GmailSearch(), GmailRead(), GmailDraft(), GmailSend(), DriveSearch(), DriveDownload(),
         DriveUpload(), DriveShare()]  # fmt: skip
