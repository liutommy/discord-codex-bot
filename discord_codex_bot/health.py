"""Container liveness: is the Bot's event loop still running, and is it connected to Discord?

The old healthcheck sent signal 0 to PID 1, which under `init: true` is tini, alive whatever the
Bot does: a hung loop or a gateway that never came back still read "healthy", so nothing that
restarts an unhealthy container ever fired.

Now a task on the Bot's own event loop rewrites HEARTBEAT every BEAT_SECONDS with how long the
gateway has been down (0 while connected). `python -m discord_codex_bot.health` (the compose
healthcheck) then fails when:
- the file has not been rewritten for STALL_SECONDS: the loop is stuck or the task died;
- the gateway has been down for OFFLINE_SECONDS. discord.py reconnects by itself, and a short
  drop (a resume, a Discord incident of a few minutes) stays healthy; a quarter of an hour
  without a connection means its own retries are not getting anywhere, and a restart is the next
  thing to try. If Discord itself is down, a restart does not help: the guard then restarts the
  container about every 20 minutes until Discord is back, which costs nothing but log lines.
Start-up counts as connected, so a slow login has the same quarter of an hour.

The downtime is counted beat by beat, never as "now minus a moment": this box is a VM that gets
frozen for half an hour at a time, and across a freeze the clocks jump. A single step longer than
a beat (plus slack) is the freeze itself, not time the Bot spent offline, so it counts as one
beat. After a freeze the first check may see a stale file once (the next beat is at most
BEAT_SECONDS away); Docker turns unhealthy only after three failures in a row.
"""

from __future__ import annotations

import asyncio
import logging
import os
import sys
import time
from collections.abc import Callable
from pathlib import Path

LOGGER = logging.getLogger(__name__)

HEARTBEAT = Path(os.environ.get("HEALTH_FILE", "/tmp/discord-codex-heartbeat"))
BEAT_SECONDS = 30
STALL_SECONDS = 180
OFFLINE_SECONDS = 900


def beat(path: Path, offline: float) -> None:
    """Rewrite the heartbeat: its mtime says the loop ran, its content how long Discord is down."""
    scratch = path.with_name(path.name + ".tmp")
    scratch.write_text(f"{offline:.0f}\n")
    os.replace(scratch, path)  # the checker never reads a half-written file


async def beat_forever(
    connected: Callable[[], bool],
    path: Path = HEARTBEAT,
    every: float = BEAT_SECONDS,
    clock: Callable[[], float] = time.monotonic,
) -> None:
    offline = 0.0
    last = clock()
    failing = False
    while True:
        now = clock()
        step = min(now - last, 2 * every)  # a longer step is a freeze, not downtime
        last = now
        offline = 0.0 if connected() else offline + step
        try:
            beat(path, offline)
        except OSError as error:
            if not failing:  # once, not every beat; the check turns unhealthy by itself
                LOGGER.warning("Cannot write the health heartbeat %s: %s", path, error)
                failing = True
        else:
            if failing:
                LOGGER.info("Health heartbeat %s is being written again", path)
                failing = False
        await asyncio.sleep(every)


def problem(path: Path = HEARTBEAT, now: float | None = None) -> str | None:
    """Why the Bot is not live, or None when it is."""
    now = time.time() if now is None else now
    try:
        silent = now - path.stat().st_mtime
        offline = float(path.read_text())
    except (OSError, ValueError) as error:
        return f"no readable heartbeat at {path}: {error}"
    if silent > STALL_SECONDS:
        return f"event loop silent for {silent:.0f}s"
    if offline > OFFLINE_SECONDS:
        return f"Discord gateway down for {offline:.0f}s"
    return None


def main() -> int:
    if reason := problem():
        print(reason)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
