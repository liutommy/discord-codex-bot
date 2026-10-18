"""The healthcheck fails when the Bot's loop stalls or Discord stays unreachable, and only then."""

from __future__ import annotations

import asyncio
import os
import subprocess
import sys
import time
from pathlib import Path
from types import SimpleNamespace

from discord_codex_bot import health
from discord_codex_bot.bot import DiscordCodexClient

ROOT = Path(__file__).resolve().parents[1]


def _aged(path: Path, silent: float, offline: float) -> None:
    now = time.time()
    health.beat(path, now - offline)
    os.utime(path, (now - silent, now - silent))


def test_a_running_connected_bot_is_live(tmp_path) -> None:
    path = tmp_path / "beat"
    _aged(path, silent=5, offline=5)
    assert health.problem(path) is None


def test_a_short_gateway_drop_is_still_live(tmp_path) -> None:
    path = tmp_path / "beat"
    _aged(path, silent=5, offline=health.OFFLINE_SECONDS - 60)  # discord.py is reconnecting
    assert health.problem(path) is None


def test_a_stalled_loop_or_a_long_outage_is_not(tmp_path) -> None:
    path = tmp_path / "beat"
    _aged(path, silent=health.STALL_SECONDS + 10, offline=5)
    assert "event loop silent" in health.problem(path)
    _aged(path, silent=5, offline=health.OFFLINE_SECONDS + 10)
    assert "gateway down" in health.problem(path)


def test_no_heartbeat_or_a_garbled_one_is_not(tmp_path) -> None:
    path = tmp_path / "beat"
    assert "no readable heartbeat" in health.problem(path)
    path.write_text("not a time\n")
    assert "no readable heartbeat" in health.problem(path)


async def test_the_beat_keeps_the_last_time_discord_was_up(tmp_path) -> None:
    path = tmp_path / "beat"
    up = [True]
    task = asyncio.create_task(health.beat_forever(lambda: up[0], path, every=0.01))
    await asyncio.sleep(0.05)
    first = float(path.read_text())
    up[0] = False
    await asyncio.sleep(1.2)
    assert float(path.read_text()) == first  # offline: the clock stops
    assert time.time() - path.stat().st_mtime < 0.5  # but the loop still beats
    up[0] = True
    await asyncio.sleep(0.05)
    assert float(path.read_text()) > first
    task.cancel()


def test_the_command_compose_runs_exits_by_the_verdict(tmp_path) -> None:
    path = tmp_path / "beat"
    run = lambda: subprocess.run(  # noqa: E731
        [sys.executable, "-m", "discord_codex_bot.health"],
        env={**os.environ, "HEALTH_FILE": str(path)},
        cwd=ROOT,
        capture_output=True,
        text=True,
    )
    assert run().returncode == 1  # nothing written yet
    _aged(path, silent=1, offline=1)
    assert run().returncode == 0
    _aged(path, silent=1, offline=health.OFFLINE_SECONDS + 10)
    result = run()
    assert result.returncode == 1 and "gateway down" in result.stdout


def test_connected_means_ready_with_an_open_socket() -> None:
    up = DiscordCodexClient._gateway_up
    ready = lambda: True  # noqa: E731
    assert up(SimpleNamespace(is_ready=ready, ws=SimpleNamespace(open=True)))
    assert not up(SimpleNamespace(is_ready=ready, ws=SimpleNamespace(open=False)))  # reconnecting
    assert not up(SimpleNamespace(is_ready=ready, ws=None))
    assert not up(SimpleNamespace(is_ready=lambda: False, ws=SimpleNamespace(open=True)))


def test_compose_asks_the_bot_not_tini() -> None:
    compose = (ROOT / "compose.yaml").read_text()
    assert '"python", "-m", "discord_codex_bot.health"' in compose
    assert "os.kill(1, 0)" not in compose.split("healthcheck:")[1].split("\n\n")[0]
