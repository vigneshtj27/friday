"""Model access. `LLM` is the seam: the agent loop only needs `complete` and `json`.

AnthropicLLM uses the official SDK with adaptive thinking, an explicit effort,
automatic prompt caching, and server-side refusal fallbacks.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from typing import Any, Protocol

log = logging.getLogger(__name__)

# USD per million tokens: (input, output, cache_read, cache_write_5m)
PRICING: dict[str, tuple[float, float, float, float]] = {
    "claude-fable-5-1": (10.0, 50.0, 0.25, 12.5),
    "claude-opus-5-5": (4.0, 20.0, 0.20, 5.0),
    "claude-opus-5": (5.0, 25.0, 0.50, 6.25),
    "claude-sonnet-5-5": (2.0, 10.0, 0.20, 2.5),
    "claude-haiku-5-5": (0.10, 0.50, 0.01, 0.125),
}
FALLBACK_MODELS = {"claude-fable-5-1", "claude-opus-5-5", "claude-opus-5", "claude-sonnet-5-5"}


class LLMRefusal(RuntimeError):
    pass


@dataclass
class ToolUse:
    id: str
    name: str
    input: dict[str, Any]


@dataclass
class LLMResponse:
    content: list[Any]  # blocks to append verbatim as the assistant turn
    tool_uses: list[ToolUse]
    text: str
    stop_reason: str
    usage: dict[str, int] = field(default_factory=dict)
    cost_usd: float = 0.0


def cost_of(model: str, usage: dict[str, int]) -> float:
    pin, pout, pread, pwrite = PRICING.get(model, PRICING["claude-opus-5-5"])
    return (
        usage.get("input_tokens", 0) * pin
        + usage.get("output_tokens", 0) * pout
        + usage.get("cache_read_input_tokens", 0) * pread
        + usage.get("cache_creation_input_tokens", 0) * pwrite
    ) / 1_000_000


class LLM(Protocol):
    model: str

    async def complete(self, system: str, messages: list[dict[str, Any]], tools: list[dict[str, Any]]) -> LLMResponse: ...

    async def json(self, system: str, content: list[dict[str, Any]] | str, schema: dict[str, Any], **kw: Any) -> tuple[dict[str, Any], float]: ...


def _block_get(block: Any, key: str, default: Any = None) -> Any:
    return block.get(key, default) if isinstance(block, dict) else getattr(block, key, default)


def parse_blocks(content: list[Any]) -> tuple[list[ToolUse], str]:
    uses, texts = [], []
    for b in content:
        t = _block_get(b, "type")
        if t == "tool_use":
            raw = _block_get(b, "input") or {}
            if isinstance(raw, str):
                raw = json.loads(raw)
            uses.append(ToolUse(_block_get(b, "id"), _block_get(b, "name"), dict(raw)))
        elif t == "text":
            texts.append(_block_get(b, "text") or "")
    return uses, "\n".join(texts)


class AnthropicLLM:
    def __init__(
        self, model: str, *, effort: str = "medium", fallbacks: bool = True, max_tokens: int = 16000, client: Any = None
    ):
        import anthropic

        self._anthropic = anthropic
        self.client = client or anthropic.AsyncAnthropic()
        self.model = model
        self.effort = effort
        self.fallbacks = fallbacks and model in FALLBACK_MODELS
        self.max_tokens = max_tokens

    def _extra(self) -> dict[str, Any]:
        if self.fallbacks:
            return {"betas": ["server-side-fallback-2026-07-01"], "fallbacks": "default"}
        return {}

    @staticmethod
    def _usage(resp: Any) -> dict[str, int]:
        u = resp.usage
        return {
            k: int(getattr(u, k, 0) or 0)
            for k in ("input_tokens", "output_tokens", "cache_read_input_tokens", "cache_creation_input_tokens")
        }

    async def complete(self, system: str, messages: list[dict[str, Any]], tools: list[dict[str, Any]]) -> LLMResponse:
        resp = await self.client.beta.messages.create(
            model=self.model,
            max_tokens=self.max_tokens,
            system=[{"type": "text", "text": system}],
            tools=tools,
            tool_choice={"type": "auto"},
            messages=messages,
            thinking={"type": "adaptive"},
            output_config={"effort": self.effort},
            cache_control={"type": "ephemeral"},
            **self._extra(),
        )
        if resp.stop_reason == "refusal":
            details = getattr(resp, "stop_details", None)
            raise LLMRefusal(f"model declined: {getattr(details, 'category', None)}")
        usage = self._usage(resp)
        uses, text = parse_blocks(resp.content)
        return LLMResponse(list(resp.content), uses, text, resp.stop_reason, usage, cost_of(resp.model, usage))

    async def json(
        self, system: str, content: list[dict[str, Any]] | str, schema: dict[str, Any], *, effort: str = "low", max_tokens: int = 4000
    ) -> tuple[dict[str, Any], float]:
        resp = await self.client.beta.messages.create(
            model=self.model,
            max_tokens=max_tokens,
            system=system,
            messages=[{"role": "user", "content": content}],
            thinking={"type": "adaptive"},
            output_config={"effort": effort, "format": {"type": "json_schema", "schema": schema}},
            **self._extra(),
        )
        if resp.stop_reason == "refusal":
            raise LLMRefusal("model declined the classification request")
        _, text = parse_blocks(resp.content)
        return json.loads(text), cost_of(resp.model, self._usage(resp))


def default_llm(settings: Any) -> LLM:
    return AnthropicLLM(settings.model, effort=settings.model_effort, fallbacks=settings.model_fallbacks)
