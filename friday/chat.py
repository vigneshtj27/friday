"""The built-in chat: the owner's delivery channel inside the app.

Every notification lands here as a message from friday. A message can carry a
card (a code request, an approval, a remote-view handoff, a finished run) that
the chat page renders with its own buttons. The owner's replies go to the same
handler as the external channels, so typing a code resolves the pending request.

Codes are never stored: an owner message that delivered a code, or that looks
like one, is kept masked.
"""

from __future__ import annotations

import asyncio
import logging
import re
from typing import Any

from friday.db import Database
from friday.notify import Notifier, ReplyHandler
from friday.util import iso
from friday.vault.masking import Redactor

log = logging.getLogger(__name__)

CARD_KINDS = ("otp", "approval", "handoff", "run", "setup")
# A whole message that is just a code ("284113", "284 113", "G-482913").
_CODE_MESSAGE = re.compile(r"[A-Za-z]{0,2}-?\d[\d -]{2,10}\d")
# Code-like numbers inside a sentence: 5-8 digits, or 3+3 digits. Dates, years, amounts with
# separators (12,480.00) and card endings (4417) stay readable.
_CODE_IN_TEXT = re.compile(r"(?<![\w.,/:-])(?:\d{5,8}|\d{3} \d{3})(?![\w.,/:-])")
HELP = (
    "I post here whenever a run needs you: a one-time code, an approval, or a step only you can do in the browser. "
    "Reply with just the code when I ask for one. Type \"status\" to see what is waiting."
)


def looks_like_code(text: str) -> bool:
    return bool(_CODE_MESSAGE.fullmatch(text.strip()))


def mask_codes(text: str) -> str:
    if looks_like_code(text):
        return "•" * min(len(text.strip().replace(" ", "")), 8)
    return _CODE_IN_TEXT.sub(lambda m: "•" * len(m.group(0).replace(" ", "")), text)


class ChatStore:
    def __init__(self, db: Database, redactor: Redactor):
        self._db = db
        self._redactor = redactor
        self._changed = asyncio.Event()

    def add(
        self, sender: str, text: str, *, url: str | None = None, kind: str | None = None,
        ref: str | None = None, profile_id: str | None = None,
    ) -> dict[str, Any]:  # fmt: skip
        if sender not in ("friday", "owner"):
            raise ValueError(sender)
        if kind is not None and kind not in CARD_KINDS:
            kind = None
        text = self._redactor.redact(text)[:4000]
        with self._db.tx() as c:
            cur = c.execute(
                "INSERT INTO chat_messages (created_at, sender, text, url, kind, ref, profile_id) VALUES (?, ?, ?, ?, ?, ?, ?)",
                (iso(), sender, text, url, kind, ref, profile_id),
            )
            msg_id = cur.lastrowid
        self.touch()
        return self.get(msg_id)  # type: ignore[arg-type]

    def get(self, msg_id: int) -> dict[str, Any] | None:
        row = self._db.query_one("SELECT * FROM chat_messages WHERE id = ?", (msg_id,))
        return dict(row) if row else None

    def list(self, *, after: int = 0, before: int | None = None, limit: int = 200) -> list[dict[str, Any]]:
        if before is not None:
            rows = self._db.query("SELECT * FROM chat_messages WHERE id < ? ORDER BY id DESC LIMIT ?", (before, limit))
            return [dict(r) for r in reversed(rows)]
        if after:
            rows = self._db.query("SELECT * FROM chat_messages WHERE id > ? ORDER BY id LIMIT ?", (after, limit))
            return [dict(r) for r in rows]
        rows = self._db.query("SELECT * FROM chat_messages ORDER BY id DESC LIMIT ?", (limit,))
        return [dict(r) for r in reversed(rows)]

    def has_card(self, kind: str, ref: str) -> bool:
        return self._db.query_one("SELECT 1 FROM chat_messages WHERE kind = ? AND ref = ? LIMIT 1", (kind, ref)) is not None

    def has_before(self, msg_id: int) -> bool:
        return self._db.query_one("SELECT 1 FROM chat_messages WHERE id < ? LIMIT 1", (msg_id,)) is not None

    def last_id(self) -> int:
        row = self._db.query_one("SELECT MAX(id) AS m FROM chat_messages")
        return int(row["m"] or 0) if row else 0

    def clear(self) -> None:
        self._db.execute("DELETE FROM chat_messages")
        self.touch()

    def touch(self) -> None:
        """Wake anyone waiting for news (a message, or a card's state changing)."""
        self._changed.set()
        self._changed = asyncio.Event()

    async def wait(self, timeout: float) -> None:
        ev = self._changed
        try:
            await asyncio.wait_for(ev.wait(), timeout)
        except TimeoutError:
            pass


class ChatNotifier(Notifier):
    name = "chat"

    def __init__(self, store: ChatStore):
        self.store = store
        self._on_reply: ReplyHandler | None = None
        self.assistant: Any = None  # friday.assistant.ChatAssistant, set by the app context
        self._tasks: set[asyncio.Task[None]] = set()

    async def send(self, text: str, url: str | None = None, *, meta: dict[str, Any] | None = None) -> None:
        meta = meta or {}
        kind, ref = meta.get("kind"), meta.get("ref")
        if kind and ref and self.store.has_card(kind, ref):
            kind = ref = None  # the card already in the chat updates live; this is just the news
        self.store.add("friday", text, url=url, kind=kind, ref=ref, profile_id=meta.get("profile_id"))

    async def start(self, on_reply: ReplyHandler) -> None:
        self._on_reply = on_reply

    async def owner_says(self, text: str, status: Any = None) -> dict[str, Any]:
        """Store the owner's message and act on it.

        A code goes to the waiting sign-in and never reaches the model. Anything else goes to the
        assistant, which answers in the background (the page shows it typing). Without an
        assistant, a few fixed replies cover "status" and "help". `status` gives the one-line summary.
        """
        text = text.strip()
        handled = bool(self._on_reply) and await self._on_reply(text, "chat")  # type: ignore[misc]
        if handled:
            mine = self.store.add("owner", "•" * min(max(len(text), 4), 8))
            self.store.add("friday", "Got it. The code went to the sign-in.")
            return mine
        mine = self.store.add("owner", mask_codes(text))
        if looks_like_code(text):
            self.store.add("friday", "Nothing is waiting for a code right now, so I discarded that.")
        elif self.assistant is not None:
            task = asyncio.create_task(self._answer(text))
            self._tasks.add(task)
            task.add_done_callback(self._tasks.discard)
        else:
            self.store.add("friday", self._fixed_reply(text, status))
        return mine

    async def _answer(self, text: str) -> None:
        try:
            reply = await self.assistant.reply(text)  # type: ignore[union-attr]
        except Exception as e:
            log.warning("chat assistant failed: %s: %s", type(e).__name__, e)
            reply = "Sorry, I couldn't reach the model just now. Try again in a moment; codes and the cards still work."
        self.store.add("friday", reply)

    @staticmethod
    def _fixed_reply(text: str, status: Any) -> str:
        word = text.lower().strip(" ?!.")
        if word in ("status", "what's waiting", "whats waiting", "pending") and status:
            return status()
        if word in ("help", "hi", "hello", "hey", "?"):
            return HELP
        return (
            "Free-text requests need the model, which isn't configured (set ANTHROPIC_API_KEY). Codes and the "
            "cards still work. Type \"status\" to see what is waiting."
        )
