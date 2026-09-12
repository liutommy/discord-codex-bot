import asyncio

import pytest

from discord_codex_bot.queue import QueueFullError, SerialQueue


async def test_queue_rejects_when_full_and_frees_the_slot() -> None:
    queue = SerialQueue(1)
    gate, started = asyncio.Event(), asyncio.Event()

    async def slow() -> str:
        started.set()
        await gate.wait()
        return "done"

    task = asyncio.create_task(queue.run(slow))
    await started.wait()
    assert queue.size == 1
    with pytest.raises(QueueFullError):
        await queue.run(slow)
    gate.set()
    assert await task == "done" and queue.size == 0


async def test_queue_runs_operations_one_at_a_time_in_order() -> None:
    queue = SerialQueue(3)
    events: list[str] = []

    def job(name: str):
        async def run() -> str:
            events.append(f"{name}:start")
            await asyncio.sleep(0.01)
            events.append(f"{name}:end")
            return name

        return run

    results = await asyncio.gather(queue.run(job("a")), queue.run(job("b")), queue.run(job("c")))
    assert results == ["a", "b", "c"]
    assert events == ["a:start", "a:end", "b:start", "b:end", "c:start", "c:end"]


async def test_queue_releases_the_slot_when_the_operation_fails() -> None:
    queue = SerialQueue(1)

    async def boom() -> None:
        raise ValueError("x")

    with pytest.raises(ValueError):
        await queue.run(boom)
    assert queue.size == 0
