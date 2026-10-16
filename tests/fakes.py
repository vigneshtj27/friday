"""Test doubles: a generic heuristic 'model' that drives pages from read_page output alone,
plus helpers to run the mock bank. No per-site logic: it looks for generic controls."""

from __future__ import annotations

import json
import re
import socket
import threading
import time
from typing import Any

import uvicorn

from friday.agent.llm import LLMResponse, parse_blocks

EL = re.compile(r'^\s*\[(\w+)\] (\S+) ("(?:[^"\\]|\\.)*")(.*)$')


def parse_elements(text: str) -> list[dict[str, str]]:
    out = []
    for line in text.splitlines():
        m = EL.match(line)
        if m:
            out.append({"ref": m.group(1), "role": m.group(2), "name": json.loads(m.group(3)), "rest": m.group(4)})
    return out


def _text_of(content: Any) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return " ".join(b.get("text", "") if isinstance(b, dict) else "" for b in content)
    return ""


class HeuristicLLM:
    model = "fake-heuristic"

    def __init__(self, *, dates: tuple[str, str] | None = None, script: list[tuple[str, dict]] | None = None):
        self.n = 0
        self.dates = dates
        self.script = list(script or [])
        self.done: set[str] = set()
        self.calls = 0
        self.json_calls = 0

    def _use(self, name: str, args: dict[str, Any]) -> LLMResponse:
        self.n += 1
        block = {"type": "tool_use", "id": f"tu_{self.n}", "name": name, "input": args}
        uses, text = parse_blocks([block])
        return LLMResponse([block], uses, text, "tool_use", {"input_tokens": 100, "output_tokens": 20}, 0.0001)

    async def complete(self, system: str, messages: list[dict[str, Any]], tools: list[dict[str, Any]]) -> LLMResponse:
        self.calls += 1
        if self.script:
            name, args = self.script.pop(0)
            return self._use(name, args)
        goal = messages[0]["content"]
        instructions = " ".join(m["content"] for m in messages if m["role"] == "system")
        last_user = next((m for m in reversed(messages) if m["role"] == "user"), None)
        results = last_user["content"] if last_user and isinstance(last_user["content"], list) else []
        all_results = [_text_of(r.get("content")) for m in messages if m["role"] == "user" and isinstance(m["content"], list) for r in m["content"]]
        page = next((_text_of(r.get("content")) for r in results if "Interactive elements:" in _text_of(r.get("content"))), None)
        if page is None:
            return self._use("read_page", {})
        els = parse_elements(page)

        def find(role: str | None = None, pattern: str = ".") -> dict[str, str] | None:
            return next((e for e in els if (role is None or e["role"] == role) and re.search(pattern, e["name"], re.I)), None)

        def once(key: str, name: str, args: dict[str, Any]) -> LLMResponse | None:
            if key in self.done:
                return None
            self.done.add(key)
            return self._use(name, args)

        url = re.search(r"URL: (\S+)", page).group(1)
        if "Download the account statement" in goal:
            stored = [r for r in all_results if "Downloaded and stored" in r]
            if stored:
                ids = [json.loads(r.split("stored: ", 1)[1])["file_id"] for r in stored]
                return self._use("finish", {"status": "success", "summary": "Downloaded the statement.", "data": {"file_ids": ids},
                                            "notes": [{"kind": "statement_path", "text": "Statements link on the account summary page; date-range form offers CSV."}]})
            if any(r.startswith("No download started") for r in all_results):
                return self._use("finish", {"status": "failed", "summary": "Download did not start."})
            frm, to = find("date", "from"), find("date", "to")
            if frm and to and self.dates:
                for key, el, val in (("from", frm, self.dates[0]), ("to", to, self.dates[1])):
                    r = once(f"{url}:{key}", "type", {"ref": el["ref"], "text": val})
                    if r:
                        return r
                fmt = find("combobox", "format")
                r = fmt and once(f"{url}:fmt", "select", {"ref": fmt["ref"], "option": "CSV"})
                if r:
                    return r
                btn = find("button", "download")
                return self._use("download_file", {"ref": btn["ref"]})
            link = find("link", "statement")
            if link:
                return once(f"{url}:stmt", "click", {"ref": link["ref"]}) or self._use("read_page", {})
            return self._use("finish", {"status": "failed", "summary": "No statements section found."})
        if "Transfer" in goal:
            btn = find("button", "pay now")
            if btn:
                if "did not approve" in " ".join(all_results):
                    return self._use("finish", {"status": "blocked", "summary": "Owner denied the transfer."})
                return self._use("click", {"ref": btn["ref"]})
            link = find("link", "transfer")
            if link:
                return once("transfer", "click", {"ref": link["ref"]}) or self._use("read_page", {})
            return self._use("finish", {"status": "failed", "summary": "no transfer page"})
        # Sign-in goal.
        if find("link", "sign out"):
            return self._use("finish", {"status": "success", "summary": "Signed in.",
                                        "notes": [{"kind": "login_flow", "text": "Customer ID and password on one page."}]})
        otp_ref = re.search(r"type (secret:\S+otp)", instructions, re.I)
        code_field = find(None, "one-time|code")
        if code_field and otp_ref:
            key = f"otp:{instructions.count('one-time code is ready')}"
            r = once(key + ":type", "type", {"ref": code_field["ref"], "text": otp_ref.group(1).rstrip(".")})
            if r:
                return r
            btn = find("button", "verify|submit|continue")
            r = btn and once(key + ":submit", "click", {"ref": btn["ref"]})
            if r:
                return r
        pw = find("password")
        user = find("textbox", "customer|user|login|email")
        if pw and user:
            attempt = sum(1 for r in all_results if r.startswith("Clicked") and "Sign in" in r)
            key = f"login{attempt}"
            for k, el, val in ((key + "u", user, "secret:{p}username"), (key + "p", pw, "secret:{p}password")):
                if k not in self.done:
                    prefix = re.search(r"secret:(\S+/)username", goal).group(1)
                    self.done.add(k)
                    return self._use("type", {"ref": el["ref"], "text": val.format(p=prefix)})
            btn = find("button", "sign in|log in|login")
            if btn and key + "s" not in self.done:
                self.done.add(key + "s")
                return self._use("click", {"ref": btn["ref"]})
        if re.search(r"check your phone|approve", page, re.I):
            return self._use("wait_for", {"seconds": 2})
        return self._use("wait_for", {"seconds": 1})

    async def json(self, system: str, content: Any, schema: dict[str, Any]) -> tuple[dict[str, Any], float]:
        """Classification stand-in: trust a Sign-out link, otherwise say UNKNOWN."""
        self.json_calls += 1
        text = content if isinstance(content, str) else " ".join(b.get("text", "") for b in content if isinstance(b, dict))
        if "state" in json.dumps(schema):
            if re.search(r"sign ?out|log ?out", text, re.I):
                return {"state": "LOGGED_IN", "reason": "sign-out visible"}, 0.0
            return {"state": "UNKNOWN", "reason": "fake"}, 0.0
        return {}, 0.0


def free_port() -> int:
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


class MockBankServer:
    def __init__(self) -> None:
        from friday.mock_site import create_mock_app

        self.port = free_port()
        self.app = create_mock_app()
        self.server = uvicorn.Server(uvicorn.Config(self.app, host="127.0.0.1", port=self.port, log_level="warning"))
        self.thread = threading.Thread(target=self.server.run, daemon=True)

    def __enter__(self) -> MockBankServer:
        self.thread.start()
        deadline = time.time() + 10
        while not self.server.started and time.time() < deadline:
            time.sleep(0.05)
        return self

    def __exit__(self, *a: Any) -> None:
        self.server.should_exit = True
        self.thread.join(5)

    def url(self, path: str) -> str:
        return f"http://localhost:{self.port}{path}"
