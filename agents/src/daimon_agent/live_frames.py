"""Live screenshot frames — port of `run.ts`'s LIVE_FRAME_INTERVAL_MS poller.

Best-effort by contract: a failed or absent frame never fails the turn. Gated
by `DAIMON_LIVE_FRAMES` (on by default) and only runs when a browser client
exists. `start(emit)` returns a task the turn loop cancels in its `finally` —
the frame stream is strictly turn-scoped.
"""

from __future__ import annotations

import asyncio
from typing import Any

from .events import live_frame_event

LIVE_FRAME_INTERVAL_S = 1.5


async def _poller(emit, browser, frame_delay: float) -> None:
    while True:
        try:
            frame = await browser.screenshot_base64()
            if frame:
                emit(live_frame_event(frame))
        except Exception:
            pass  # best-effort: never fail a turn for frames
        await asyncio.sleep(frame_delay)


def start(emit, browser, settings: Any) -> asyncio.Task | None:
    """Start the frame poller, or return None when live frames are disabled."""
    if not getattr(settings, "live_frames", False) or browser is None:
        return None
    return asyncio.create_task(_poller(emit, browser, LIVE_FRAME_INTERVAL_S))
