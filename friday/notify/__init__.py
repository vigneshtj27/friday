"""Owner notifications and inbound replies (for SMS codes).

Outbound text always passes through the redactor. Inbound replies are only
accepted from the configured chat/number and only resolve pending OTP requests.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
from collections.abc import Awaitable, Callable
from typing import Any

import httpx

from friday.config import Settings
from friday.vault.masking import Redactor

log = logging.getLogger(__name__)

ReplyHandler = Callable[[str, str], Awaitable[bool]]  # (text, via) -> handled


class Notifier:
    name = "base"

    async def send(self, text: str, url: str | None = None, *, meta: dict[str, Any] | None = None) -> None:
        raise NotImplementedError

    async def start(self, on_reply: ReplyHandler) -> None:
        pass

    async def stop(self) -> None:
        pass


class LogNotifier(Notifier):
    name = "log"

    def __init__(self) -> None:
        self.sent: list[tuple[str, str | None]] = []

    async def send(self, text: str, url: str | None = None, *, meta: dict[str, Any] | None = None) -> None:
        self.sent.append((text, url))
        log.info("NOTIFY: %s %s", text, url or "")


class TelegramNotifier(Notifier):
    name = "telegram"

    def __init__(self, token: str, chat_id: str):
        self.base = f"https://api.telegram.org/bot{token}"
        self.chat_id = str(chat_id)
        self._task: asyncio.Task[None] | None = None

    async def send(self, text: str, url: str | None = None, *, meta: dict[str, Any] | None = None) -> None:
        body = text + (f"\n{url}" if url else "")
        async with httpx.AsyncClient(timeout=20) as http:
            r = await http.post(f"{self.base}/sendMessage", json={"chat_id": self.chat_id, "text": body})
            r.raise_for_status()

    async def start(self, on_reply: ReplyHandler) -> None:
        self._task = asyncio.create_task(self._poll(on_reply))

    async def _poll(self, on_reply: ReplyHandler) -> None:
        offset = None
        async with httpx.AsyncClient(timeout=40) as http:
            while True:
                try:
                    params: dict[str, Any] = {"timeout": 30, "allowed_updates": json.dumps(["message"])}
                    if offset is not None:
                        params["offset"] = offset
                    r = await http.get(f"{self.base}/getUpdates", params=params)
                    r.raise_for_status()
                    for upd in r.json().get("result", []):
                        offset = upd["update_id"] + 1
                        msg = upd.get("message") or {}
                        if str((msg.get("chat") or {}).get("id")) != self.chat_id:
                            continue  # only the owner's chat
                        if msg.get("text"):
                            await on_reply(msg["text"], "telegram")
                except asyncio.CancelledError:
                    raise
                except Exception as e:
                    log.warning("telegram poll failed: %s", type(e).__name__)
                    await asyncio.sleep(10)

    async def stop(self) -> None:
        if self._task:
            self._task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._task


class NtfyNotifier(Notifier):
    name = "ntfy"

    def __init__(self, server: str, topic: str, reply_topic: str | None, token: str | None):
        self.server = server.rstrip("/")
        self.topic = topic
        self.reply_topic = reply_topic
        self.headers = {"Authorization": f"Bearer {token}"} if token else {}
        self._task: asyncio.Task[None] | None = None

    async def send(self, text: str, url: str | None = None, *, meta: dict[str, Any] | None = None) -> None:
        headers = dict(self.headers, Title="friday")
        if url:
            headers["Click"] = url
        async with httpx.AsyncClient(timeout=20) as http:
            r = await http.post(f"{self.server}/{self.topic}", content=text.encode(), headers=headers)
            r.raise_for_status()

    async def start(self, on_reply: ReplyHandler) -> None:
        if self.reply_topic:
            self._task = asyncio.create_task(self._listen(on_reply))

    async def _listen(self, on_reply: ReplyHandler) -> None:
        while True:
            try:
                async with httpx.AsyncClient(timeout=None) as http:
                    async with http.stream("GET", f"{self.server}/{self.reply_topic}/json", headers=self.headers) as r:
                        async for line in r.aiter_lines():
                            if not line:
                                continue
                            ev = json.loads(line)
                            if ev.get("event") == "message" and ev.get("message"):
                                await on_reply(ev["message"], "ntfy")
            except asyncio.CancelledError:
                raise
            except Exception as e:
                log.warning("ntfy listen failed: %s", type(e).__name__)
                await asyncio.sleep(10)

    async def stop(self) -> None:
        if self._task:
            self._task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._task


class WhatsAppNotifier(Notifier):
    """WhatsApp Business Cloud API. Replies arrive on the /hooks/whatsapp webhook."""

    name = "whatsapp"

    def __init__(self, token: str, phone_number_id: str, to: str):
        self.url = f"https://graph.facebook.com/v21.0/{phone_number_id}/messages"
        self.token = token
        self.to = to

    async def send(self, text: str, url: str | None = None, *, meta: dict[str, Any] | None = None) -> None:
        body = {"messaging_product": "whatsapp", "to": self.to, "type": "text", "text": {"body": text + (f"\n{url}" if url else "")}}
        async with httpx.AsyncClient(timeout=20) as http:
            r = await http.post(self.url, json=body, headers={"Authorization": f"Bearer {self.token}"})
            r.raise_for_status()


class MultiNotifier(Notifier):
    name = "multi"

    def __init__(self, notifiers: list[Notifier], redactor: Redactor):
        self.notifiers = notifiers
        self.redactor = redactor

    async def send(self, text: str, url: str | None = None, *, meta: dict[str, Any] | None = None) -> None:
        text = self.redactor.redact(text)
        for n in self.notifiers:
            try:
                await n.send(text, url, meta=meta)
            except Exception as e:
                log.warning("%s notification failed: %s", n.name, type(e).__name__)

    async def start(self, on_reply: ReplyHandler) -> None:
        for n in self.notifiers:
            await n.start(on_reply)

    async def stop(self) -> None:
        for n in self.notifiers:
            await n.stop()


def build_notifier(settings: Settings, redactor: Redactor, chat: Notifier | None = None) -> MultiNotifier:
    """The built-in chat (when given) is always on; external channels are opt-in."""
    wanted = {x.strip() for x in settings.notify.split(",") if x.strip() and x.strip() not in ("none", "chat")}
    out: list[Notifier] = [LogNotifier()] + ([chat] if chat else [])
    for secret in (settings.telegram_bot_token, settings.ntfy_token, settings.whatsapp_token):
        if secret:
            redactor.register("config/notify-token", secret.get_secret_value())
    if "telegram" in wanted and settings.telegram_bot_token and settings.telegram_chat_id:
        out.append(TelegramNotifier(settings.telegram_bot_token.get_secret_value(), settings.telegram_chat_id))
    if "ntfy" in wanted and settings.ntfy_topic:
        tok = settings.ntfy_token.get_secret_value() if settings.ntfy_token else None
        out.append(NtfyNotifier(settings.ntfy_server, settings.ntfy_topic, settings.ntfy_reply_topic, tok))
    if "whatsapp" in wanted and settings.whatsapp_token and settings.whatsapp_phone_number_id and settings.whatsapp_to:
        out.append(
            WhatsAppNotifier(settings.whatsapp_token.get_secret_value(), settings.whatsapp_phone_number_id, settings.whatsapp_to)
        )
    missing = wanted - {n.name for n in out}
    if missing:
        log.warning("notification channel(s) %s requested but not fully configured", sorted(missing))
    return MultiNotifier(out, redactor)
