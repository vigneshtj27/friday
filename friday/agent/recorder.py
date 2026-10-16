"""Keeps the last navigation statuses, console errors and failed requests of a browser context."""

from __future__ import annotations

import asyncio
from collections import deque
from typing import Any

from friday.vault.masking import Redactor


class PageRecorder:
    def __init__(self, redactor: Redactor, size: int = 30):
        self.redactor = redactor
        self.navigations: deque[dict[str, Any]] = deque(maxlen=size)
        self.console_errors: deque[str] = deque(maxlen=size)
        self.failed_requests: deque[dict[str, str]] = deque(maxlen=size)
        self._tasks: set[asyncio.Task[Any]] = set()

    def attach(self, context: Any) -> None:
        for page in context.pages:
            self._attach_page(page)
        context.on("page", self._attach_page)

    def _attach_page(self, page: Any) -> None:
        page.on("response", self._on_response)
        page.on("console", self._on_console)
        page.on("requestfailed", self._on_failed)

    def _on_response(self, response: Any) -> None:
        try:
            req = response.request
            if req.resource_type != "document" or req.frame.parent_frame is not None:
                return
        except Exception:
            return
        entry = {"url": self.redactor.redact(response.url)[:300], "status": response.status, "set_cookie": None}
        self.navigations.append(entry)

        async def cookie() -> None:
            try:
                entry["set_cookie"] = (await response.header_value("set-cookie")) is not None
            except Exception:
                pass

        t = asyncio.ensure_future(cookie())
        self._tasks.add(t)
        t.add_done_callback(self._tasks.discard)

    def _on_console(self, msg: Any) -> None:
        if msg.type == "error":
            self.console_errors.append(self.redactor.redact(msg.text)[:300])

    def _on_failed(self, request: Any) -> None:
        self.failed_requests.append(
            {"url": self.redactor.redact(request.url)[:200], "error": str(request.failure or "")[:120], "type": request.resource_type}
        )

    @property
    def last_status(self) -> int | None:
        return self.navigations[-1]["status"] if self.navigations else None

    def snapshot(self) -> dict[str, Any]:
        return {
            "navigations": list(self.navigations)[-10:],
            "console_errors": list(self.console_errors)[-15:],
            "failed_requests": list(self.failed_requests)[-15:],
        }
