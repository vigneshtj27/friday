"""A durable model-spend meter and the LLM wrapper that enforces it.

Every model call friday makes - planning, chat replies, the browser agent loop, classification -
goes through a `MeteredLLM`, which records the call's cost in `usage_ledger` and refuses to start a
call once the configured cap for the current window is reached. The cap is cumulative and spans all
callers, and because it lives in the database it survives restarts: a crash mid-month does not reset
the running total.

Nothing about the prompt or the response is stored - only the model name, the kind of caller and the
cost. The cap is configuration (friday/config.py), never set by the model or by owner free text, so
no web page or email can raise it.
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime
from typing import Any

from friday.util import iso

log = logging.getLogger(__name__)


class BudgetExceeded(RuntimeError):
    """Raised before a model call that would run while the spend cap is already reached."""


def window_key(window: str, now: datetime | None = None) -> str:
    now = now or datetime.now(UTC)
    if window == "day":
        return now.strftime("%Y-%m-%d")
    if window == "total":
        return "all"
    return now.strftime("%Y-%m")  # month (default)


class UsageMeter:
    def __init__(self, ctx: Any):
        self.ctx = ctx

    # ---- config ------------------------------------------------------------------------------

    @property
    def cap(self) -> float:
        return float(getattr(self.ctx.settings, "model_budget_usd", 0.0) or 0.0)

    @property
    def window(self) -> str:
        return getattr(self.ctx.settings, "model_budget_window", "month")

    @property
    def warn_fraction(self) -> float:
        return float(getattr(self.ctx.settings, "model_budget_warn_fraction", 0.8) or 0.8)

    # ---- accounting --------------------------------------------------------------------------

    def key(self, now: datetime | None = None) -> str:
        return window_key(self.window, now)

    def spent(self, key: str | None = None) -> float:
        k = key or self.key()
        row = self.ctx.db.query_one("SELECT COALESCE(SUM(cost_usd), 0) AS s FROM usage_ledger WHERE window_key = ?", (k,))
        return float(row["s"]) if row else 0.0

    def fraction(self) -> float:
        return (self.spent() / self.cap) if self.cap > 0 else 0.0

    def over(self) -> bool:
        """True once the cap is reached. A disabled cap (0) is never over."""
        return self.cap > 0 and self.spent() >= self.cap

    def guard(self) -> None:
        if self.over():
            raise BudgetExceeded(
                f"model budget cap of ${self.cap:.2f} for this {self.window} is reached "
                f"(spent ${self.spent():.2f}); no new model calls will run until it resets or is raised."
            )

    def record(self, model: str, cost_usd: float, kind: str, usage: dict[str, int] | None = None) -> None:
        """Record one model call's cost, then warn once if this window just crossed the threshold."""
        k = self.key()
        u = usage or {}
        self.ctx.db.execute(
            "INSERT INTO usage_ledger (ts, window_key, model, kind, cost_usd, input_tokens, output_tokens)"
            " VALUES (?, ?, ?, ?, ?, ?, ?)",
            (iso(), k, model, kind, float(cost_usd or 0.0), int(u.get("input_tokens", 0)), int(u.get("output_tokens", 0))),
        )
        self._maybe_warn(k)

    def _maybe_warn(self, k: str) -> None:
        if self.cap <= 0:
            return
        if self.spent(k) < self.warn_fraction * self.cap or self.over():
            # Below the warn line, or already at the hard stop (the stop message speaks for itself).
            return
        if self.ctx.db.query_one("SELECT 1 FROM usage_notices WHERE window_key = ?", (k,)):
            return  # warned this window already
        self.ctx.db.execute("INSERT OR IGNORE INTO usage_notices (window_key, warned_at) VALUES (?, ?)", (k, iso()))
        pct = int(round(100 * self.spent(k) / self.cap))
        msg = (f"Heads up: model spend this {self.window} is ${self.spent(k):.2f} of the "
               f"${self.cap:.2f} cap ({pct}%). At the cap friday pauses new model calls until it resets.")  # fmt: skip
        try:
            self.ctx.chat.store.add("friday", msg)
        except Exception:
            log.warning("could not post the usage warning to chat")

    # ---- reporting ---------------------------------------------------------------------------

    def status(self) -> dict[str, Any]:
        k = self.key()
        return {"window": self.window, "window_key": k, "cap_usd": round(self.cap, 4),
                "spent_usd": round(self.spent(k), 4), "fraction": round(self.fraction(), 4),
                "over": self.over(), "enabled": self.cap > 0}  # fmt: skip


class MeteredLLM:
    """Wraps an LLM, recording every call's cost and refusing to start one once the cap is reached.

    Implements the same surface the agent loop and planner use (`model`, `complete`, `json`).
    """

    def __init__(self, inner: Any, meter: UsageMeter, kind: str):
        self._inner = inner
        self._meter = meter
        self._kind = kind
        self.model = inner.model

    async def complete(self, system: str, messages: list[dict[str, Any]], tools: list[dict[str, Any]]) -> Any:
        self._meter.guard()
        resp = await self._inner.complete(system, messages, tools)
        self._meter.record(self.model, getattr(resp, "cost_usd", 0.0), self._kind, getattr(resp, "usage", None))
        return resp

    async def json(self, system: str, content: Any, schema: dict[str, Any], **kw: Any) -> tuple[dict[str, Any], float]:
        self._meter.guard()
        out, cost = await self._inner.json(system, content, schema, **kw)
        self._meter.record(self.model, cost, self._kind)
        return out, cost


def metered(factory: Any, meter: UsageMeter, kind: str) -> Any:
    """Wrap an llm_factory so every LLM it produces is metered. Returns a new factory."""
    if factory is None or factory is False:
        return factory
    return lambda: MeteredLLM(factory(), meter, kind)
