"""Run friday's own page scripts in an isolated JavaScript world.

Playwright's frame.evaluate runs in the page's main world through the page's
global `eval`. Some sites (American Express, for one) replace or disable
`eval` and other globals as a hardening measure, so every read friday made of
such a page failed and the page looked blank. An isolated world (the same
mechanism browser extensions use) shares the page's DOM but has its own fresh
globals, so the page's scripts can neither break nor observe friday's reads.

Falls back to frame.evaluate where CDP can't reach (cross-process iframes, or
a non-Chromium driver).
"""

from __future__ import annotations

import json
import logging
import weakref
from typing import Any

log = logging.getLogger(__name__)

WORLD = "friday"


class _PageWorlds:
    def __init__(self, cdp: Any):
        self.cdp = cdp
        self.contexts: dict[tuple[str, str], int] = {}  # (frameId, loaderId) -> executionContextId


_sessions: "weakref.WeakKeyDictionary[Any, _PageWorlds]" = weakref.WeakKeyDictionary()


async def _worlds(page: Any) -> _PageWorlds:
    w = _sessions.get(page)
    if w is None:
        w = _PageWorlds(await page.context.new_cdp_session(page))
        _sessions[page] = w
    return w


def _walk(tree: dict[str, Any]) -> list[dict[str, Any]]:
    out = [tree["frame"]]
    for child in tree.get("childFrames") or []:
        out.extend(_walk(child))
    return out


async def _frame_ids(w: _PageWorlds, frame: Any) -> tuple[str, str]:
    tree = (await w.cdp.send("Page.getFrameTree"))["frameTree"]
    frames = _walk(tree)
    if frame.parent_frame is None:
        f = frames[0]
    else:
        matches = [f for f in frames[1:] if f.get("url") == frame.url and f.get("name", "") == (frame.name or "")]
        if not matches:
            raise LookupError("frame not in this page's process")
        f = matches[0]
    return f["id"], f.get("loaderId", "")


async def _context_id(w: _PageWorlds, key: tuple[str, str], fresh: bool = False) -> int:
    if fresh or key not in w.contexts:
        r = await w.cdp.send("Page.createIsolatedWorld", {"frameId": key[0], "worldName": WORLD, "grantUniveralAccess": False})
        w.contexts = {k: v for k, v in w.contexts.items() if k[0] != key[0]}  # one live world per frame
        w.contexts[key] = r["executionContextId"]
    return w.contexts[key]


async def evaluate(frame: Any, fn_source: str, arg: Any = None) -> Any:
    """Call the JS function `fn_source` with `arg` (JSON) in an isolated world of `frame`."""
    try:
        page = frame.page
        w = await _worlds(page)
        key = await _frame_ids(w, frame)
    except Exception as e:
        log.debug("isolated world unavailable (%s); using the main world", type(e).__name__)
        return await frame.evaluate(fn_source, arg)
    expression = f"({fn_source})({json.dumps(arg)})"
    for attempt in range(2):
        ctx_id = await _context_id(w, key, fresh=attempt == 1)
        try:
            r = await w.cdp.send(
                "Runtime.evaluate",
                {"expression": expression, "contextId": ctx_id, "returnByValue": True, "awaitPromise": True},
            )
        except Exception as e:  # the document changed under us: its world is gone
            if attempt == 1:
                raise RuntimeError(f"isolated evaluate failed: {e}") from None
            continue
        if "exceptionDetails" in r:
            d = r["exceptionDetails"]
            msg = (d.get("exception") or {}).get("description") or d.get("text") or "script error"
            raise RuntimeError(msg.splitlines()[0][:300])
        return r.get("result", {}).get("value")
    return None
