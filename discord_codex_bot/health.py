"""Container liveness: is the Bot's event loop still running, and is it connected to Discord?

The old healthcheck sent signal 0 to PID 1, which under `init: true` is tini, alive whatever the
Bot does: a hung loop or a gateway that never came back still read "healthy", so nothing that
restarts an unhealthy container ever fired.

Now a task on the Bot's own event loop rewrites HEARTBEAT every BEAT_SECONDS with the last time
it saw the gateway connected. `python -m discord_codex_bot.health` (the compose healthcheck) then
fails when:
- the file has not been rewritten for STALL_SECONDS: the loop is stuck or the task died;
- the gateway has been down for OFFLINE_SECONDS. discord.py reconnects by itself, and a short
  drop (a resume, a Discord incident of a few minutes) stays healthy; a quarter of an hour
  without a connection means its own retries are not getting anywhere, and a restart is the next
  thing to try.
Start-up counts as connected from the moment the task starts, so a slow login has the same
quarter of an hour.
"""

from __future__ import annotations

import asyncio
import os
import sys
import time
from collections.abc import Callable
from pathlib import Path

HEARTBEAT = Path(os.environ.get("HEALTH_FILE", "/tmp/discord-codex-heartbeat"))
BEAT_SECONDS = 30
STALL_SECONDS = 180
OFFLINE_SECONDS = 900


def beat(path: Path, last_online: float) -> None:
    """Rewrite the heartbeat: its mtime says the loop ran, its content when Discord was last up."""
    scratch = path.with_name(path.name + ".tmp")
    scratch.write_text(f"{last_online:.0f}\n")
    os.replace(scratch, path)  # the checker never reads a half-written file


async def beat_forever(
    connected: Callable[[], bool], path: Path = HEARTBEAT, every: float = BEAT_SECONDS
) -> None:
    last_online = time.time()
    while True:
        now = time.time()
        if connected():
            last_online = now
        try:
            beat(path, last_online)
        except OSError:
            pass  # a missing heartbeat turns unhealthy by itself; nothing to add here
        await asyncio.sleep(every)


def problem(path: Path = HEARTBEAT, now: float | None = None) -> str | None:
    """Why the Bot is not live, or None when it is."""
    now = time.time() if now is None else now
    try:
        silent = now - path.stat().st_mtime
        last_online = float(path.read_text())
    except (OSError, ValueError) as error:
        return f"no readable heartbeat at {path}: {error}"
    if silent > STALL_SECONDS:
        return f"event loop silent for {silent:.0f}s"
    if now - last_online > OFFLINE_SECONDS:
        return f"Discord gateway down for {now - last_online:.0f}s"
    return None


def main() -> int:
    if reason := problem():
        print(reason)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
