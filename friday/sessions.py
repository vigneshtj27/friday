"""Long-lived browser sessions that wait on a human (stage 1: profile seeding).

The agent loop (stage 2) will park its own open profile on a handoff the same
way: create a handoff with the display's VNC target, await it, then continue.
"""

from __future__ import annotations

import asyncio
import logging

from friday.handoff.manager import Handoff, HandoffManager, HandoffState
from friday.profiles.manager import ProfileBusy, ProfileManager

log = logging.getLogger(__name__)


class SessionSupervisor:
    def __init__(self, profiles: ProfileManager, handoffs: HandoffManager):
        self._profiles = profiles
        self._handoffs = handoffs
        self._tasks: dict[str, asyncio.Task[None]] = {}

    def running(self) -> list[str]:
        return [pid for pid, t in self._tasks.items() if not t.done()]

    async def start_seed(self, profile_id: str, *, actor: str = "admin") -> Handoff:
        """Open the profile headed, point it at the login page, and hand it to the owner."""
        if (t := self._tasks.get(profile_id)) and not t.done():
            raise ProfileBusy(f"profile {profile_id} already has a running session")
        ready: asyncio.Future[Handoff] = asyncio.get_running_loop().create_future()
        self._tasks[profile_id] = asyncio.create_task(self._seed(profile_id, ready, actor))
        return await ready

    async def _seed(self, profile_id: str, ready: asyncio.Future[Handoff], actor: str) -> None:
        try:
            async with self._profiles.open(profile_id, purpose="seed", actor=actor) as op:
                page = await op.session.page()
                if op.site.login_url:
                    try:
                        await page.goto(op.site.login_url, wait_until="domcontentloaded", timeout=45_000)
                    except Exception as e:
                        log.warning("could not open login page for %s: %s", profile_id, e)
                reason = "Log in manually to seed this profile, then click Done."
                if op.warnings:
                    reason += " Warnings: " + "; ".join(op.warnings)
                h = self._handoffs.create(profile_id, reason, op.display.vnc)
                ready.set_result(h)
                log.info("handoff ready for %s: %s", profile_id, self._handoffs.link(h))
                state = await self._handoffs.wait(h.id)
                if state == HandoffState.DONE:
                    self._profiles.set_status(profile_id, "seeded")
        except BaseException as e:
            if not ready.done():
                ready.set_exception(e)
            elif not isinstance(e, asyncio.CancelledError):
                log.exception("seed session for %s failed", profile_id)
            if isinstance(e, asyncio.CancelledError):
                raise

    async def shutdown(self) -> None:
        for t in self._tasks.values():
            t.cancel()
        await asyncio.gather(*self._tasks.values(), return_exceptions=True)
