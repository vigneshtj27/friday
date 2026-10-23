"""Data-in: import a transaction file the owner uploaded, and reconcile email hints against it.

File intake is read-only of the file: friday parses a CSV, XLSX or PDF the owner put in the chat and
stores the rows as bank-confirmed transactions. A password-protected file is reported so the owner
can save the password via a credential card (never typed in chat); a scanned/image-only PDF is
reported unsupported. Sheet writes from these rows stay approval-gated with read-back, as before.

Reconciliation matches still-PENDING email evidence one-to-one against the imported rows (amount,
date window, merchant), marks the matches confirmed, and lists what is unmatched on both sides. Email
evidence is a hint, never statement truth.
"""

from __future__ import annotations

from typing import Any

from friday.capabilities.transactions import (
    parse_csv,
    parse_xlsx,
    pdf_text,
    rows_to_transactions,
)
from friday.tools.base import Call, Tool, ToolError

MAX_FILE = 8_000_000


def _resolve_file(ctx: Any, ref: str) -> Any:
    meta = ctx.files.meta(ref)
    if meta is not None:
        return meta
    matches = [f for f in ctx.files.list(limit=200) if ref.lower() in f.name.lower() and f.kind != "screenshot"]
    if len(matches) == 1:
        return matches[0]
    raise ToolError(f"no single uploaded file matches {ref!r}; upload it in the chat first")


class ImportTransactionsFile(Tool):
    name = "import_transactions_file"
    description = ("Import an uploaded statement file (CSV, XLSX or text PDF) as transactions for an account. "
                   "Password-protected files are reported so you can save the password; scanned/image-only PDFs are "
                   "unsupported. Use for \"import this statement into my HDFC account\".")
    schema = {"type": "object", "properties": {
        "file": {"type": "string", "description": "the uploaded file's id or name"},
        "account": {"type": "string", "description": "the account label these rows belong to, e.g. hdfc.me"},
        "currency": {"type": "string"}, "card": {"type": "boolean", "description": "true for a card account's view"},
    }, "required": ["file", "account"]}  # fmt: skip
    effect = "write"

    async def run(self, args: dict[str, Any], call: Call) -> Any:
        ctx = call.ctx
        meta = _resolve_file(ctx, str(args["file"]))
        if meta.size > MAX_FILE:
            raise ToolError("that file is too large to import")
        data = ctx.files.read(meta.id)
        name = meta.name.lower()
        currency = (args.get("currency") or ctx.settings.default_currency or "INR").upper()
        kw = {"source": str(args["account"]), "raw_ref": meta.name, "currency": currency, "card": bool(args.get("card"))}
        try:
            if name.endswith(".csv") or "csv" in meta.mime:
                txns = parse_csv(data, **kw)
            elif name.endswith((".xlsx", ".xlsm")) or "sheet" in meta.mime or "excel" in meta.mime:
                txns = self._xlsx(data, kw)
            elif name.endswith(".pdf") or "pdf" in meta.mime:
                txns = self._pdf(data, kw)
            else:
                raise ToolError("unsupported file type; upload a CSV, XLSX or text PDF")
        except ValueError as e:
            msg = str(e)
            if "password" in msg.lower() or "encrypted" in msg.lower():
                raise ToolError(f"this file is password-protected. Save its password with a credential card "
                                f"(name it {args['account'].replace('.', '/')}/statement-password), then ask me to retry.") from e  # fmt: skip
            raise ToolError(f"couldn't read that file: {msg}") from e
        if not txns:
            raise ToolError("no transactions found in that file (a scanned/image-only PDF isn't supported; "
                            "ask the bank for a CSV or a text PDF)")
        new, dup = ctx.transactions.add(txns)
        return {"imported": new, "already_known": dup, "account": args["account"],
                "verified": [f"imported {new} new row(s) into {args['account']} ({dup} already known)"]}  # fmt: skip

    @staticmethod
    def _xlsx(data: bytes, kw: dict[str, Any]) -> Any:
        try:
            return parse_xlsx(data, **kw)
        except Exception as e:
            if "password" in str(e).lower() or "encrypted" in str(e).lower() or "not supported" in str(e).lower():
                raise ValueError("password-protected or unreadable workbook") from e
            raise ValueError(str(e)) from e

    @staticmethod
    def _pdf(data: bytes, kw: dict[str, Any]) -> Any:
        # Only the text layer is read here (deterministic). pdf_text raises on an encrypted PDF; an
        # image-only (scanned) PDF yields no text, so no header row is found and it is reported
        # unsupported. The model-assisted PDF path (parse_pdf) is used elsewhere for messy layouts.
        text = pdf_text(data)
        if not text.strip():
            return []
        rows = [line.split() for line in text.splitlines()]
        try:
            return rows_to_transactions(rows, **kw)
        except ValueError:
            return []  # couldn't find a table in the text -> treat as unsupported layout


class ReconcileEvidence(Tool):
    name = "reconcile_evidence"
    description = ("Match pending email-expense evidence against imported/bank transactions one-to-one (amount, date "
                   "window, merchant), mark the matches confirmed, and list what's unmatched on each side. Email "
                   "evidence is a hint, not statement truth.")
    schema = {"type": "object", "properties": {
        "start": {"type": "string"}, "end": {"type": "string"}, "window_days": {"type": "integer"},
    }}  # fmt: skip

    async def run(self, args: dict[str, Any], call: Call) -> Any:
        window = min(max(int(args.get("window_days") or 3), 0), 14)
        report = call.ctx.transactions.reconcile(window_days=window, start=args.get("start"), end=args.get("end"))
        report["verified"] = [f"confirmed {report['confirmed']} email hint(s) against bank rows; "
                              f"{len(report['unmatched_email'])} email and {len(report['unmatched_bank'])} bank left unmatched"]  # fmt: skip
        return report


TOOLS: list[Tool] = [ImportTransactionsFile(), ReconcileEvidence()]
