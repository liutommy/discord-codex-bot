from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from typing import TypeVar

T = TypeVar("T")


class QueueFullError(RuntimeError):
    pass


class SerialQueue:
    def __init__(self, max_size: int) -> None:
        self._max_size = max_size
        self._pending = 0
        self._pending_lock = asyncio.Lock()
        self._worker = asyncio.Lock()

    @property
    def size(self) -> int:
        return self._pending

    async def run(self, operation: Callable[[], Awaitable[T]]) -> T:
        async with self._pending_lock:
            if self._pending >= self._max_size:
                raise QueueFullError("Codex queue is full")
            self._pending += 1
        try:
            async with self._worker:
                return await operation()
        finally:
            async with self._pending_lock:
                self._pending -= 1
