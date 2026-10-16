"""The exact request AnthropicLLM sends, through the real SDK, against a local fake API.
(No network, no API key. The real model itself is not exercised here.)"""

from __future__ import annotations

import json

import anthropic
import pytest
from fastapi import FastAPI, Request

from friday.agent.llm import AnthropicLLM, LLMRefusal
from friday.agent.tools import TOOLS

from .fakes import MockBankServer


class FakeAPI(MockBankServer):
    def __init__(self, replies):
        super().__init__()
        self.seen: list[dict] = []
        app = FastAPI()

        @app.post("/v1/messages")
        async def messages(request: Request):
            self.seen.append({"body": await request.json(), "headers": dict(request.headers)})
            return replies.pop(0)

        self.app = app
        import uvicorn

        self.server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=self.port, log_level="warning"))
        import threading

        self.thread = threading.Thread(target=self.server.run, daemon=True)


def msg(content, stop="tool_use", **extra):
    return {"id": "msg_1", "type": "message", "role": "assistant", "model": "claude-opus-5-5", "content": content,
            "stop_reason": stop, "stop_sequence": None, "usage": {"input_tokens": 1000, "output_tokens": 200, "cache_read_input_tokens": 500}, **extra}


async def test_agent_request_shape_and_parsing():
    replies = [
        msg([{"type": "thinking", "thinking": "", "signature": "sig"}, {"type": "tool_use", "id": "tu_1", "name": "read_page", "input": {}}]),
        msg([{"type": "text", "text": json.dumps({"state": "LOGGED_IN", "reason": "sign out link"})}], stop="end_turn"),
        msg([], stop="refusal", stop_details={"type": "refusal", "category": "cyber", "explanation": None}),
    ]
    with FakeAPI(replies) as api:
        client = anthropic.AsyncAnthropic(api_key="test-key", base_url=f"http://127.0.0.1:{api.port}", max_retries=0)
        llm = AnthropicLLM("claude-opus-5-5", effort="high", client=client)
        resp = await llm.complete("SYSTEM", [{"role": "user", "content": "go"}], TOOLS)
        assert [t.name for t in resp.tool_uses] == ["read_page"]
        assert resp.content[0].type == "thinking"  # kept verbatim for the append-only history
        assert resp.cost_usd == pytest.approx((1000 * 4 + 200 * 20 + 500 * 0.2) / 1e6)
        body, headers = api.seen[0]["body"], api.seen[0]["headers"]
        assert body["model"] == "claude-opus-5-5"
        assert body["thinking"] == {"type": "adaptive"}
        assert body["output_config"] == {"effort": "high"}
        assert body["tool_choice"] == {"type": "auto"}
        assert body["cache_control"] == {"type": "ephemeral"}
        assert body["fallbacks"] == "default"
        assert "server-side-fallback-2026-07-01" in headers["anthropic-beta"]
        assert "budget_tokens" not in json.dumps(body) and "temperature" not in body
        assert {t["name"] for t in body["tools"]} >= {"goto", "click", "type", "finish", "request_approval"}

        data, cost = await llm.json("classify", "page text", {"type": "object", "properties": {"state": {"type": "string"}}, "required": ["state"], "additionalProperties": False})
        assert data["state"] == "LOGGED_IN"
        assert api.seen[1]["body"]["output_config"]["format"]["type"] == "json_schema"

        with pytest.raises(LLMRefusal, match="cyber"):
            await llm.complete("SYSTEM", [{"role": "user", "content": "go"}], TOOLS)
