"""The healthcheck fails when the Bot's loop stalls or Discord stays unreachable, and only then."""

from __future__ import annotations

import asyncio
import contextlib
import logging
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
    health.beat(path, offline)
    now = time.time()
    os.utime(path, (now - silent, now - silent))


def test_a_running_connected_bot_is_live(tmp_path) -> None:
    path = tmp_path / "beat"
    _aged(path, silent=5, offline=0)
    assert health.problem(path) is None


def test_a_short_gateway_drop_is_still_live(tmp_path) -> None:
    path = tmp_path / "beat"
    _aged(path, silent=5, offline=health.OFFLINE_SECONDS - 60)  # discord.py is reconnecting
    assert health.problem(path) is None


def test_a_stalled_loop_or_a_long_outage_is_not(tmp_path) -> None:
    path = tmp_path / "beat"
    _aged(path, silent=health.STALL_SECONDS + 10, offline=0)
    assert "event loop silent" in health.problem(path)
    _aged(path, silent=5, offline=health.OFFLINE_SECONDS + 10)
    assert "gateway down" in health.problem(path)


def test_no_heartbeat_or_a_garbled_one_is_not(tmp_path) -> None:
    path = tmp_path / "beat"
    assert "no readable heartbeat" in health.problem(path)
    path.write_text("not a number\n")
    assert "no readable heartbeat" in health.problem(path)


async def _beats(monkeypatch, path: Path, steps: list[tuple[float, bool]]) -> list[float]:
    """Drive beat_forever on a fake clock: each step is (seconds since the last beat, connected
    at that beat). Returns what each beat wrote."""
    clock = [1000.0]
    up = [True]
    written: list[float] = []
    plan = iter(steps)

    async def sleep(_seconds: float) -> None:
        written.append(float(path.read_text()))  # noqa: ASYNC240 (a fake sleep in a test)
        try:
            gap, up[0] = next(plan)
        except StopIteration:
            raise asyncio.CancelledError from None
        clock[0] += gap

    monkeypatch.setattr(health.asyncio, "sleep", sleep)
    with contextlib.suppress(asyncio.CancelledError):
        await health.beat_forever(lambda: up[0], path, every=30, clock=lambda: clock[0])
    return written


async def test_downtime_is_counted_beat_by_beat_and_reset_on_reconnect(
    tmp_path, monkeypatch
) -> None:
    steps = [(30, False), (30, False), (30, False), (30, True), (30, False)]
    assert await _beats(monkeypatch, tmp_path / "beat", steps) == [0, 30, 60, 90, 0, 30]


async def test_a_frozen_vm_does_not_count_the_freeze_as_downtime(tmp_path, monkeypatch) -> None:
    # 10 minutes down, then the VM is frozen for 35 minutes and wakes with the socket still gone
    steps = [(30, False)] * 20 + [(35 * 60, False), (30, False)]
    written = await _beats(monkeypatch, tmp_path / "beat", steps)
    assert written[20] == 600  # before the freeze
    assert written[21] == 660  # the freeze counts as one long beat (2 x 30 s), not 35 minutes
    assert written[-1] == 690
    path = tmp_path / "beat"
    assert health.problem(path) is None


async def test_a_heartbeat_that_cannot_be_written_is_logged_once(
    tmp_path, monkeypatch, caplog
) -> None:
    path = tmp_path / "gone" / "beat"  # its directory does not exist: every write fails
    clock = [0.0]
    rounds = [0]

    async def sleep(_seconds: float) -> None:
        rounds[0] += 1
        if rounds[0] == 3:
            path.parent.mkdir()  # the disk has room again
        if rounds[0] == 5:
            raise asyncio.CancelledError
        clock[0] += 30

    monkeypatch.setattr(health.asyncio, "sleep", sleep)
    with caplog.at_level(logging.INFO, logger="discord_codex_bot.health"):
        with contextlib.suppress(asyncio.CancelledError):
            await health.beat_forever(lambda: True, path, every=30, clock=lambda: clock[0])
    messages = [record.getMessage() for record in caplog.records]
    assert sum("Cannot write the health heartbeat" in m for m in messages) == 1
    assert sum("being written again" in m for m in messages) == 1
    assert path.read_text() == "0\n"


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
    _aged(path, silent=1, offline=0)
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
