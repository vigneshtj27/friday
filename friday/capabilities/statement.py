"""Capability: fetch a statement for a date range, store the file encrypted, parse and
normalize its transactions, verify them, and dedupe into the transactions table."""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import date
from typing import Any

from friday.agent.loop import AgentLoop, AgentTask
from friday.capabilities.transactions import (
    Transaction,
    parse_csv,
    parse_pdf,
    parse_xlsx,
    verify,
)

log = logging.getLogger(__name__)


@dataclass
class CapabilityResult:
    status: str  # succeeded | failed | needs_attention
    summary: str
    result: dict[str, Any]
    notes: list[dict[str, str]]
    contradicted_notes: list[str]


def _kind(name: str, mime: str, data: bytes) -> str:
    n = name.lower()
    if data[:5] == b"%PDF-" or n.endswith(".pdf") or "pdf" in mime:
        return "pdf"
    if data[:2] == b"PK" or n.endswith(".xlsx"):
        return "xlsx"
    if n.endswith(".xls") or data[:8] == b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1":
        return "xls"
    return "csv"


class FetchStatement:
    name = "fetch_statement"

    def __init__(self, ctx: Any, llm: Any):
        self.ctx = ctx
        self.llm = llm

    @staticmethod
    def validate(params: dict[str, Any]) -> dict[str, Any]:
        start, end = date.fromisoformat(params["start"]), date.fromisoformat(params["end"])
        if end < start:
            raise ValueError("end is before start")
        if (end - start).days > 400:
            raise ValueError("ranges over ~13 months: run one job per year")
        return {"start": start.isoformat(), "end": end.isoformat(), "currency": params.get("currency")}

    async def run(self, op: Any, ex: Any, run_id: str, params: dict[str, Any]) -> CapabilityResult:
        profile, site = op.profile, op.site
        start, end = date.fromisoformat(params["start"]), date.fromisoformat(params["end"])
        currency = (params.get("currency") or self.ctx.settings.default_currency).upper()
        task = AgentTask(
            goal=(
                f"You are signed in. Download the account statement for account '{profile.account}' covering "
                f"{start.isoformat()} to {end.isoformat()} (inclusive). Prefer CSV or Excel if offered, otherwise PDF. "
                "Use download_file for the download. If the site only allows shorter periods, download each part. "
                "Do not change any settings or preferences. When the file(s) are downloaded, call finish with "
                "status success and data.file_ids. If the site offers e-statements by email or an official export/API, "
                "add an official_export note."
            ),
            run_id=run_id,
            profile_id=profile.id,
            site_name=site.name,
            allowed_domains=site.all_domains,
            secret_names=[],
            site_notes=self.ctx.sitenotes.render(site.id),
            max_steps=self.ctx.settings.max_steps,
            max_wall_seconds=self.ctx.settings.max_wall_seconds,
            max_cost_usd=self.ctx.settings.max_cost_usd,
        )
        outcome = await AgentLoop(self.llm, ex, self.ctx.runs).run(task)
        files = list(ex.downloads)  # what actually landed, not what the model claims
        base = {"agent_status": outcome.status, "agent_summary": outcome.summary, "files": files}
        if not files:
            return CapabilityResult("failed", f"No statement was downloaded. {outcome.summary}", base, outcome.notes, outcome.contradicted_notes)

        txns: list[Transaction] = []
        per_file = []
        stated: dict[str, Any] = {}
        dayfirst = not profile.config.locale.lower().startswith("en-us")
        for f in files:
            data = self.ctx.files.read(f["file_id"])
            kind = _kind(f["name"], f["mime"], data)
            try:
                if kind == "csv":
                    got = parse_csv(data, source=profile.id, raw_ref=f["file_id"], currency=currency, dayfirst=dayfirst)
                elif kind == "xlsx":
                    got = parse_xlsx(data, source=profile.id, raw_ref=f["file_id"], currency=currency, dayfirst=dayfirst)
                elif kind == "pdf":
                    got, st, cost = await parse_pdf(data, self.llm, source=profile.id, raw_ref=f["file_id"], currency=currency)
                    self.ctx.runs.add_cost(run_id, cost)
                    if len(files) == 1:
                        stated = st
                else:
                    raise ValueError("legacy .xls is not supported; ask the site for CSV/XLSX/PDF")
                txns += got
                per_file.append({"file_id": f["file_id"], "kind": kind, "rows": len(got)})
            except Exception as e:
                per_file.append({"file_id": f["file_id"], "kind": kind, "error": f"{type(e).__name__}: {e}"[:300]})
        check = verify(txns, start=start, end=end, stated=stated)
        result = {**base, "parsed": per_file, "verification": check}
        if not check["ok"]:
            return CapabilityResult(
                "needs_attention",
                "Downloaded, but the data did not pass checks: " + "; ".join(check["problems"]),
                result, outcome.notes, outcome.contradicted_notes,
            )  # fmt: skip
        new, dupes = self.ctx.transactions.add(txns)
        result["stored"] = {"new": new, "duplicates": dupes}
        summary = (
            f"Statement {start}..{end}: {check['count']} transactions ({new} new, {dupes} already stored); "
            f"debits {check['total_debits']}, credits {check['total_credits']}."
        )
        if check["warnings"]:
            summary += " Warnings: " + "; ".join(check["warnings"])
        return CapabilityResult("succeeded", summary, result, outcome.notes, outcome.contradicted_notes)
