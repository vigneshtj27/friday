"""The browser and the owner's accounts as tools. A browser run is unchanged: it opens one account's
persistent profile and runs the agent loop under its own allowed domains, read-only policy and
approval gate. These tools start runs, wait for them with live progress, and read friday's state."""

from __future__ import annotations

import asyncio
import json
import time
from typing import Any
from urllib.parse import urlparse

from friday.runs import TERMINAL, RunStatus
from friday.tools.base import Call, Tool, ToolError

DATE = {"type": "string", "description": "YYYY-MM-DD"}
RUN_WAIT_SECONDS = 45 * 60

_PHRASES = {
    "extract_table": "reading a table of transactions",
    "download_file": "downloading a file",
    "request_otp": "waiting for a one-time code",
    "request_human": "waiting for you in the browser",
    "request_approval": "waiting for your approval",
}


def progress_line(step: dict[str, Any]) -> str | None:
    """One plain-language line for a run step, or None for noisy steps."""
    kind, tool = step.get("kind"), step.get("tool")
    if kind == "state" and step.get("state"):
        st = str(step["state"])
        return {"LOGGED_IN": "signed in", "NEEDS_OTP_EMAIL": "the site sent an emailed code",
                "NEEDS_OTP_SMS": "the site sent an SMS code", "NEEDS_TOTP": "the site wants an authenticator code",
                "LOGIN_FORM": "on the sign-in page"}.get(st, f"sign-in: {st.lower().replace('_', ' ')}")  # fmt: skip
    if kind == "handoff":
        return "waiting for you in the browser (see the card)"
    if kind == "tool" and tool == "goto":
        try:
            host = urlparse(json.loads(step.get("args_json") or "{}").get("url", "")).hostname
        except (ValueError, AttributeError):
            host = None
        return f"opening {host}" if host else None
    if kind == "tool" and tool in _PHRASES:
        return _PHRASES[tool]
    return None


def _assistant(call: Call) -> Any:
    a = call.ctx.chat.assistant
    if a is None:
        raise ToolError("the assistant isn't running")
    return a


class AccountsList(Tool):
    name = "accounts_list"
    description = "Accounts friday can sign in to (\"<site>.<name>\", e.g. amex.me), with status and last run."

    async def run(self, args: dict[str, Any], call: Call) -> Any:
        return await _assistant(call)._run_tool("list_accounts", {})


class BrowserRun(Tool):
    name = "browser_run"
    description = (
        "Run one job in the account's real browser and wait for it (minutes; progress is shown to the owner). kind: "
        "fetch_activity (recent/unbilled/current-cycle transactions from the activity page; start, end), fetch_statement "
        "(a billed statement file for a past period; start, end), task (anything else on the site; goal), login. "
        "account_type card|bank for fetches. Fetched transactions are stored; add them to a sheet with sheets_add_transactions."
    )
    schema = {"type": "object", "properties": {
        "account": {"type": "string"},
        "kind": {"type": "string", "enum": ["fetch_activity", "fetch_statement", "task", "login"]},
        "start": DATE, "end": DATE, "goal": {"type": "string"},
        "account_type": {"type": "string", "enum": ["card", "bank"]},
    }, "required": ["account", "kind"]}  # fmt: skip
    untrusted_output = True

    async def run(self, args: dict[str, Any], call: Call) -> Any:
        ctx, a = call.ctx, _assistant(call)
        out = await a._run_tool("start_run", {k: v for k, v in args.items() if v is not None})
        run_id = out["run_id"]
        await call.say(f"Started {out['kind'].replace('_', ' ')} on {args['account']}")
        return await wait_for_run(ctx, run_id, call, note=out.get("note"))


async def wait_for_run(ctx: Any, run_id: str, call: Call, *, note: str | None = None, poll: float = 1.0) -> dict[str, Any]:
    last_step, last_said = 0, None
    deadline = time.monotonic() + RUN_WAIT_SECONDS
    while True:
        for s in ctx.runs.steps(run_id, after_id=last_step):
            last_step = s["id"]
            line = progress_line({**s, "args_json": json.dumps(s.get("args") or {})})
            if line and line != last_said:  # repeated lines (e.g. two tables) are said once
                await call.say(line)
                last_said = line
        r = ctx.runs.get(run_id)
        if r and RunStatus(r["status"]) in TERMINAL:
            break
        if time.monotonic() > deadline:
            raise ToolError(f"run {run_id} is still going after {RUN_WAIT_SECONDS // 60} min; it will report in the chat")
        await asyncio.sleep(poll)
    result = r.get("result") or {}
    out = {"run_id": run_id, "kind": r["kind"], "account": r["profile_id"], "status": r["status"], "summary": r["summary"],
           "params": r["params"]}  # fmt: skip
    for k in ("stored", "totals", "counts", "verification", "files", "answer", "data", "tables", "source", "login_state", "diagnosis_report"):
        if k in result:
            out[k] = result[k]
    if note:
        out["note"] = note
    if r["status"] != RunStatus.SUCCEEDED.value:
        out["ok"] = False
    return out


class _Delegate(Tool):
    """Wraps one of the assistant's existing tool handlers (accounts, setup cards, run state)."""

    def __init__(self, name: str, legacy: str, description: str, schema: dict[str, Any], effect: str = "read",
                 untrusted: bool = False):  # fmt: skip
        self.name, self.legacy, self.description, self.schema, self.effect = name, legacy, description, schema, effect
        self.untrusted_output = untrusted

    async def run(self, args: dict[str, Any], call: Call) -> Any:
        try:
            return await _assistant(call)._run_tool(self.legacy, args)
        except ValueError as e:
            raise ToolError(str(e)) from e


def _obj(props: dict[str, Any], required: list[str] | None = None) -> dict[str, Any]:
    return {"type": "object", "properties": props, "required": required or []}


TOOLS: list[Tool] = [
    AccountsList(),
    BrowserRun(),
    _Delegate("run_status", "get_run", "Status, summary and recent steps of a run.", _obj({"run_id": {"type": "string"}}, ["run_id"]), untrusted=True),
    _Delegate("runs_list", "list_runs", "Recent runs, newest first.", _obj({"account": {"type": "string"}, "limit": {"type": "integer"}})),
    _Delegate("run_cancel", "cancel_run", "Cancel a queued or running run.", _obj({"run_id": {"type": "string"}}, ["run_id"]), effect="write"),
    _Delegate("transactions_query", "query_transactions",
              "Search stored transactions (from fetched statements/activity): rows newest first, and totals.",
              _obj({"account": {"type": "string"}, "start": DATE, "end": DATE, "contains": {"type": "string"},
                    "direction": {"type": "string", "enum": ["debit", "credit"]}, "limit": {"type": "integer"}}), untrusted=True),
    _Delegate("waiting_items", "whats_waiting", "Code requests, approvals, setup cards and handoffs waiting on the owner; runs in progress.", _obj({})),
    _Delegate("site_find", "find_site", "Look up the official sign-in page of a provider friday doesn't know (web search, checked).",
              _obj({"provider": {"type": "string"}}, ["provider"]), untrusted=True),
    _Delegate("account_setup", "request_setup",
              "Post a secure setup card: the owner confirms the site and types credentials into a form that goes straight "
              "to the vault (never to you). pass site_id (known site) or proposal_id (from site_find), account, purpose.",
              _obj({"site_id": {"type": "string"}, "proposal_id": {"type": "string"}, "account": {"type": "string"},
                    "purpose": {"type": "string"},
                    "then": {"type": "object", "description": "job to start once the card is submitted",
                             "properties": {"kind": {"type": "string", "enum": ["fetch_statement", "fetch_activity", "task", "login"]},
                                            "start": DATE, "end": DATE, "goal": {"type": "string"},
                                            "account_type": {"type": "string", "enum": ["card", "bank"]}},
                             "required": ["kind"]}}, ["purpose"]), effect="write"),
    _Delegate("manual_login", "manual_login", "Open the account's browser for the owner to sign in by hand once.",
              _obj({"account": {"type": "string"}}, ["account"])),
    _Delegate("google_connect", "connect_google",
              "Post a link for the owner to connect Google: workspace (Sheets, Gmail, Drive) or gmail (read codes only).",
              _obj({"purpose": {"type": "string", "enum": ["workspace", "gmail", "sheets"]}}, ["purpose"])),
]
