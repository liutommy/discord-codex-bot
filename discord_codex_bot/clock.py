"""Waiting for a time of day on a host that gets paused.

asyncio.sleep runs on the monotonic clock, and the platform pauses this VM for minutes to hours
(boot_id unchanged): the monotonic clock stands still meanwhile. One long sleep to a target time
therefore ends late by every pause it spans — the first weekly digest ran 36 minutes late on
2026-10-04, a 7-hour pause on 2026-10-01 would have moved the nightly jobs by 7 hours. Short steps
checked against the wall clock end at most one step after the VM resumes.
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import Awaitable, Callable

STEP_SECONDS = 60.0


async def sleep_until(
    target: float,
    step: float = STEP_SECONDS,
    clock: Callable[[], float] = time.time,
    sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
) -> None:
    """Return once the wall clock (`clock`, a time.time() timestamp) has reached `target`.

    A target that a pause skipped over returns at the first check after it: the caller runs its
    job once and computes its next target from the new time, so missed runs are not replayed."""
    while (left := target - clock()) > 0:
        await sleep(min(step, left))


async def sleep_for(seconds: float, **kwargs) -> None:
    """sleep_until `seconds` of wall-clock time from now."""
    clock = kwargs.get("clock", time.time)
    await sleep_until(clock() + seconds, **kwargs)
