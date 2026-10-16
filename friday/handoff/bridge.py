"""WebSocket <-> TCP relay (what websockify does), so the VNC server stays on localhost
and the only way in is through the authenticated web app."""

from __future__ import annotations

import asyncio
import contextlib

from starlette.websockets import WebSocket, WebSocketDisconnect


async def bridge(ws: WebSocket, host: str, port: int) -> None:
    reader, writer = await asyncio.open_connection(host, port)

    async def ws_to_tcp() -> None:
        while True:
            msg = await ws.receive()
            if msg["type"] == "websocket.disconnect":
                return
            data = msg.get("bytes")
            if data is None and msg.get("text") is not None:
                data = msg["text"].encode()
            if data:
                writer.write(data)
                await writer.drain()

    async def tcp_to_ws() -> None:
        while data := await reader.read(65536):
            await ws.send_bytes(data)

    tasks = [asyncio.create_task(ws_to_tcp()), asyncio.create_task(tcp_to_ws())]
    try:
        _, pending = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
        for t in pending:
            t.cancel()
        for t in tasks:
            with contextlib.suppress(asyncio.CancelledError, WebSocketDisconnect, ConnectionError):
                await t
    finally:
        writer.close()
        with contextlib.suppress(Exception):
            await writer.wait_closed()
        with contextlib.suppress(Exception):
            await ws.close()
