from __future__ import annotations

import json
import time
from pathlib import Path

MAX_MESSAGE_LINKS = 1000


class ThreadStore:
    """Maps Discord conversations to Codex thread ids so follow-ups resume the same thread.

    Two indexes: (guild, channel, user) -> most recent thread within the TTL, and bot message id ->
    thread, so replying to an old bot answer continues that exact conversation. Persisted as JSON
    next to the Codex sessions so a container restart keeps the mapping.
    """

    def __init__(self, path: Path, ttl_seconds: float) -> None:
        self._path = path
        self._ttl = ttl_seconds
        self._by_key: dict[str, dict[str, float | str]] = {}
        self._by_message: dict[str, str] = {}
        self._load()

    @staticmethod
    def key(guild_id: int | None, channel_id: int | None, user_id: int) -> str:
        return f"{guild_id}:{channel_id}:{user_id}"

    def current(self, key: str, now: float | None = None) -> str:
        entry = self._by_key.get(key)
        if entry is None:
            return ""
        if (time.time() if now is None else now) - float(entry["at"]) > self._ttl:
            return ""
        return str(entry["thread_id"])

    def by_message(self, message_id: int | None) -> str:
        return self._by_message.get(str(message_id), "") if message_id is not None else ""

    def remember(self, key: str, thread_id: str, message_id: int | None = None) -> None:
        if not thread_id:
            return
        self._by_key[key] = {"thread_id": thread_id, "at": time.time()}
        if message_id is not None:
            self._by_message[str(message_id)] = thread_id
            for stale in list(self._by_message)[: -MAX_MESSAGE_LINKS or None]:
                if len(self._by_message) <= MAX_MESSAGE_LINKS:
                    break
                del self._by_message[stale]
        self._save()

    def forget(self, key: str) -> bool:
        removed = self._by_key.pop(key, None) is not None
        if removed:
            self._save()
        return removed

    def _load(self) -> None:
        try:
            data = json.loads(self._path.read_text("utf-8"))
        except (OSError, ValueError):
            return
        self._by_key = dict(data.get("by_key", {}))
        self._by_message = dict(data.get("by_message", {}))

    def _save(self) -> None:
        cutoff = time.time() - self._ttl
        self._by_key = {k: v for k, v in self._by_key.items() if float(v["at"]) >= cutoff}
        payload = {"by_key": self._by_key, "by_message": self._by_message}
        try:
            self._path.write_text(json.dumps(payload), "utf-8")
        except OSError:
            pass
