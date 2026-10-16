"""Where the headed Chrome window lives.

* NativeDisplay: the host's own screen (macOS dev, or a Linux desktop).
* XvfbDisplay: a private virtual X display per open profile, plus an x11vnc
  server bound to localhost so the remote-view page can show it.
"""

from __future__ import annotations

import asyncio
import logging
import os
import secrets
import shutil
import sys
import tempfile
from abc import ABC, abstractmethod
from dataclasses import dataclass
from pathlib import Path

from friday.config import ConfigError, Settings

log = logging.getLogger(__name__)


@dataclass(frozen=True)
class VncTarget:
    host: str
    port: int
    password: str


class Display(ABC):
    kind: str
    env: dict[str, str]
    vnc: VncTarget | None = None

    @abstractmethod
    async def start(self) -> None: ...

    @abstractmethod
    async def stop(self) -> None: ...


class NativeDisplay(Display):
    kind = "native"

    def __init__(self) -> None:
        self.env = {}

    async def start(self) -> None:
        pass

    async def stop(self) -> None:
        pass


async def _wait_for(predicate, timeout: float, what: str) -> None:
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    while not predicate():
        if loop.time() > deadline:
            raise RuntimeError(f"timed out waiting for {what}")
        await asyncio.sleep(0.05)


async def _port_open(host: str, port: int) -> bool:
    try:
        _, w = await asyncio.open_connection(host, port)
    except OSError:
        return False
    w.close()
    return True


class XvfbDisplay(Display):
    kind = "xvfb"

    def __init__(self, number: int, width: int, height: int, vnc_port: int | None):
        self.number = number
        self.width = width
        self.height = height
        self.vnc_port = vnc_port
        self.env = {"DISPLAY": f":{number}"}
        self._xvfb: asyncio.subprocess.Process | None = None
        self._vnc: asyncio.subprocess.Process | None = None
        self._tmpdir: Path | None = None

    @property
    def socket_path(self) -> Path:
        return Path(f"/tmp/.X11-unix/X{self.number}")

    async def start(self) -> None:
        self._xvfb = await asyncio.create_subprocess_exec(
            "Xvfb", f":{self.number}", "-screen", "0", f"{self.width}x{self.height}x24",
            "-nolisten", "tcp", "-dpi", "96",
            stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.DEVNULL,
        )  # fmt: skip
        await _wait_for(self.socket_path.exists, 10, f"Xvfb :{self.number}")
        if self.vnc_port is None:
            return
        self._tmpdir = Path(tempfile.mkdtemp(prefix="friday-vnc-"))
        passwd_file = self._tmpdir / "passwd"
        password = secrets.token_urlsafe(6)[:8]  # RFB auth uses at most 8 chars
        store = await asyncio.create_subprocess_exec(
            "x11vnc", "-storepasswd", password, str(passwd_file),
            stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.DEVNULL,
        )  # fmt: skip
        await store.wait()
        self._vnc = await asyncio.create_subprocess_exec(
            "x11vnc", "-display", f":{self.number}", "-localhost", "-rfbport", str(self.vnc_port),
            "-rfbauth", str(passwd_file), "-forever", "-shared", "-noxdamage", "-quiet",
            stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.DEVNULL,
        )  # fmt: skip
        loop = asyncio.get_running_loop()
        deadline = loop.time() + 10
        while not await _port_open("127.0.0.1", self.vnc_port):
            if loop.time() > deadline or self._vnc.returncode is not None:
                raise RuntimeError(f"x11vnc did not start on port {self.vnc_port}")
            await asyncio.sleep(0.1)
        self.vnc = VncTarget("127.0.0.1", self.vnc_port, password)

    async def stop(self) -> None:
        for proc in (self._vnc, self._xvfb):
            if proc and proc.returncode is None:
                proc.terminate()
                try:
                    await asyncio.wait_for(proc.wait(), 5)
                except TimeoutError:
                    proc.kill()
        if self._tmpdir:
            shutil.rmtree(self._tmpdir, ignore_errors=True)
        self.vnc = None


class DisplayAllocator:
    """Hands out display numbers / VNC ports unique within this process and host."""

    FIRST, LAST = 90, 189

    def __init__(self, settings: Settings):
        self.settings = settings
        backend = settings.display_backend
        if backend == "auto":
            backend = "native" if sys.platform == "darwin" else "xvfb"
        self.backend = backend
        self._in_use: set[int] = set()
        self._lock = asyncio.Lock()

    def check_available(self) -> None:
        if self.backend == "xvfb":
            missing = [b for b in ("Xvfb", "x11vnc") if shutil.which(b) is None]
            if missing:
                raise ConfigError(f"display backend 'xvfb' needs {', '.join(missing)} on PATH")
        elif self.backend == "native" and sys.platform.startswith("linux") and not os.environ.get("DISPLAY"):
            raise ConfigError("display backend 'native' on Linux needs $DISPLAY")

    async def acquire(self, *, with_vnc: bool = True) -> Display:
        if self.backend == "native":
            return NativeDisplay()
        self.check_available()
        async with self._lock:
            for n in range(self.FIRST, self.LAST + 1):
                if n in self._in_use or Path(f"/tmp/.X11-unix/X{n}").exists():
                    continue
                self._in_use.add(n)
                break
            else:
                raise RuntimeError("no free X display numbers")
        port = self.settings.vnc_port_base + n if with_vnc else None
        display = XvfbDisplay(n, self.settings.screen_width, self.settings.screen_height, port)
        try:
            await display.start()
        except BaseException:
            await display.stop()
            self._in_use.discard(n)
            raise
        return display

    async def release(self, display: Display) -> None:
        await display.stop()
        if isinstance(display, XvfbDisplay):
            self._in_use.discard(display.number)
