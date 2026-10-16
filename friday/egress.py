"""Home egress agent: a tiny authenticated HTTP proxy (CONNECT tunnels + plain HTTP).

Run it on a machine at home, reachable only over your VPN (Tailscale/WireGuard):

    FRIDAY_EGRESS_PASSWORD=... friday egress-proxy --listen 100.64.0.5:3128

then set the profile's proxy to http://100.64.0.5:3128 with the username/password
stored in the vault. Sites then see your home IP. Bind to the VPN address only;
it refuses to start on 0.0.0.0 unless --allow-any-interface is given.
"""

from __future__ import annotations

import asyncio
import base64
import contextlib
import hmac
import ipaddress
import logging
import socket
from urllib.parse import urlsplit

log = logging.getLogger(__name__)

HEADER_LIMIT = 64 * 1024


class EgressProxy:
    def __init__(self, username: str, password: str, *, block_private: bool = True):
        self._expected = "Basic " + base64.b64encode(f"{username}:{password}".encode()).decode()
        self.block_private = block_private
        self.connections = 0

    def _authorized(self, headers: dict[str, str]) -> bool:
        return hmac.compare_digest(headers.get("proxy-authorization", ""), self._expected)

    async def _target_allowed(self, host: str, port: int) -> bool:
        if not self.block_private:
            return True
        # Don't let the proxy become a door into the home LAN.
        try:
            infos = await asyncio.get_running_loop().getaddrinfo(host, port, type=socket.SOCK_STREAM)
        except OSError:
            return False
        for info in infos:
            ip = ipaddress.ip_address(info[4][0])
            if ip.is_private or ip.is_loopback or ip.is_link_local or ip.is_reserved or ip.is_multicast:
                return False
        return True

    async def handle(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        self.connections += 1
        try:
            head = await asyncio.wait_for(reader.readuntil(b"\r\n\r\n"), 30)
        except (asyncio.IncompleteReadError, asyncio.LimitOverrunError, TimeoutError):
            writer.close()
            return
        lines = head.decode("latin-1").split("\r\n")
        try:
            method, target, version = lines[0].split(" ", 2)
        except ValueError:
            writer.close()
            return
        headers = {}
        for line in lines[1:]:
            if ":" in line:
                k, v = line.split(":", 1)
                headers[k.strip().lower()] = v.strip()
        if not self._authorized(headers):
            writer.write(b'HTTP/1.1 407 Proxy Authentication Required\r\nProxy-Authenticate: Basic realm="friday"\r\nContent-Length: 0\r\n\r\n')
            await writer.drain()
            writer.close()
            return
        if method.upper() == "CONNECT":
            host, _, port_s = target.rpartition(":")
            host, port = host.strip("[]"), int(port_s or 443)
            rest = b""
        else:
            u = urlsplit(target)
            if u.scheme != "http" or not u.hostname:
                writer.write(b"HTTP/1.1 400 Bad Request\r\nContent-Length: 0\r\n\r\n")
                await writer.drain()
                writer.close()
                return
            host, port = u.hostname, u.port or 80
            path = (u.path or "/") + (f"?{u.query}" if u.query else "")
            kept = [line for line in lines[1:] if line and not line.lower().startswith(("proxy-authorization:", "proxy-connection:"))]
            rest = f"{method} {path} {version}\r\n".encode("latin-1") + "\r\n".join(kept).encode("latin-1") + b"\r\n\r\n"
        if not await self._target_allowed(host, port):
            writer.write(b"HTTP/1.1 403 Forbidden\r\nContent-Length: 0\r\n\r\n")
            await writer.drain()
            writer.close()
            return
        try:
            up_reader, up_writer = await asyncio.wait_for(asyncio.open_connection(host, port), 20)
        except (OSError, TimeoutError):
            writer.write(b"HTTP/1.1 502 Bad Gateway\r\nContent-Length: 0\r\n\r\n")
            await writer.drain()
            writer.close()
            return
        if method.upper() == "CONNECT":
            writer.write(b"HTTP/1.1 200 Connection established\r\n\r\n")
            await writer.drain()
        else:
            up_writer.write(rest)
            await up_writer.drain()

        async def pipe(r: asyncio.StreamReader, w: asyncio.StreamWriter) -> None:
            try:
                while data := await r.read(65536):
                    w.write(data)
                    await w.drain()
            except (ConnectionError, OSError):
                pass
            finally:
                with contextlib.suppress(Exception):
                    w.close()

        await asyncio.gather(pipe(reader, up_writer), pipe(up_reader, writer))


async def serve(host: str, port: int, username: str, password: str, *, block_private: bool = True) -> asyncio.Server:
    proxy = EgressProxy(username, password, block_private=block_private)
    return await asyncio.start_server(proxy.handle, host, port, limit=HEADER_LIMIT)
