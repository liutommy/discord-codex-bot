from __future__ import annotations

import asyncio
from dataclasses import replace
from pathlib import Path

import pytest

from discord_codex_bot import backup, consolidate
from discord_codex_bot.clock import sleep_for, sleep_until


class FakeClock:
    """A wall clock that sleeping advances; `pause_at` adds a VM pause during that sleep call."""

    def __init__(self, now: float = 1_000_000.0, pause_at: int | None = None, pause: float = 0):
        self.now, self.pause_at, self.pause = now, pause_at, pause
        self.sleeps: list[float] = []

    def __call__(self) -> float:
        return self.now

    async def sleep(self, seconds: float) -> None:
        self.sleeps.append(seconds)
        self.now += seconds
        if len(self.sleeps) == self.pause_at:
            self.now += self.pause  # the monotonic sleep ended, the wall clock moved on further


async def test_sleep_until_steps_against_the_wall_clock() -> None:
    clock = FakeClock()
    await sleep_until(clock.now + 150, clock=clock, sleep=clock.sleep)
    assert clock.sleeps == [60, 60, 30]  # never one long sleep
    assert clock.now == 1_000_150


async def test_sleep_until_returns_right_after_a_pause_skips_the_target() -> None:
    # 2026-10-01: a 7-hour pause. A single asyncio.sleep would end 7 hours late.
    clock = FakeClock(pause_at=1, pause=7 * 3600)
    target = clock.now + 2 * 3600
    await sleep_until(target, clock=clock, sleep=clock.sleep)
    assert len(clock.sleeps) == 1  # the first check after the pause returns
    assert clock.now - target == 60 - 2 * 3600 + 7 * 3600  # at most one step past the resume


async def test_sleep_until_a_past_target_does_not_sleep() -> None:
    clock = FakeClock()
    await sleep_until(clock.now - 5, clock=clock, sleep=clock.sleep)
    await sleep_for(0, clock=clock, sleep=clock.sleep)
    assert clock.sleeps == []


@pytest.mark.parametrize("module", [backup, consolidate])
async def test_daily_jobs_wait_on_the_wall_clock_and_run_once_per_wake(
    module, monkeypatch, tmp_path: Path, config
) -> None:
    waits: list[float] = []
    runs: list[str] = []

    async def fake_sleep_for(seconds: float) -> None:
        waits.append(seconds)
        if len(waits) == 2:
            raise asyncio.CancelledError  # stop the loop at its second wait

    monkeypatch.setattr(module, "sleep_for", fake_sleep_for)
    if module is backup:
        monkeypatch.setattr(backup, "run_backup", lambda cfg: runs.append("backup") or "ok")
        job = backup.backup_forever(replace(config, backup_dir=tmp_path))
    else:
        cfg = replace(config, consolidate_min_remaining_percent=0)

        async def queue_run(work):
            runs.append("consolidate")
            return "ok"

        job = consolidate.consolidate_forever(None, cfg, queue_run)
    with pytest.raises(asyncio.CancelledError):
        await job
    assert len(waits) == 2 and 0 < waits[0] <= 86_400
    assert len(runs) == 1  # one wake, one run: nothing missed is replayed


async def test_weekly_digest_waits_on_the_wall_clock_and_runs_once_per_wake(
    monkeypatch, config
) -> None:
    from discord_codex_bot import digest

    waits: list[float] = []
    runs: list[str] = []

    async def fake_sleep_for(seconds: float) -> None:
        waits.append(seconds)
        if len(waits) == 2:
            raise asyncio.CancelledError

    async def fake_digest_all(*args, **kwargs) -> str:
        runs.append("digest")
        return "nothing new"

    monkeypatch.setattr(digest, "sleep_for", fake_sleep_for)
    monkeypatch.setattr(digest, "digest_all", fake_digest_all)
    with pytest.raises(asyncio.CancelledError):
        await digest.digest_forever(None, None, replace(config, digest_weekday=6), None, None)
    assert len(waits) == 2 and 0 < waits[0] <= 7 * 86_400
    assert runs == ["digest"]
