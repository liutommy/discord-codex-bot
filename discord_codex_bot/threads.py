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

    Every entry records the `version` of the instruction files it was started with (AGENTS.md,
    output style). Codex loads those at thread start and keeps them for the thread's life, so a
    thread from an older version is never resumed — a persona or style change takes effect on the
    next message instead of lingering until the TTL expires.

    A thread that stops being resumable (TTL passed, version changed, replaced by `new`/reset)
    becomes a harvest candidate: its transcript is distilled once into the member's long-term
    memory. `harvest_candidates()` lists them, `mark_harvested()` retires them.
    """

    def __init__(self, path: Path, ttl_seconds: float, version: str = "") -> None:
        self._path = path
        self._ttl = ttl_seconds
        self._version = version
        self._by_key: dict[str, dict[str, float | str | bool]] = {}
        self._by_message: dict[str, dict[str, str]] = {}
        self._pending: list[dict[str, str]] = []
        self._load()

    @staticmethod
    def key(guild_id: int | None, channel_id: int | None, user_id: int) -> str:
        return f"{guild_id}:{channel_id}:{user_id}"

    def _live(self, entry: dict, now: float) -> bool:
        return entry.get("version", "") == self._version and now - float(entry["at"]) <= self._ttl

    def current(self, key: str, now: float | None = None, plain: bool = False) -> str:
        """The member's resumable thread, if it was started under the same workspace (persona
        vs. persona-free) the next request will use; a style set or cleared in between starts
        a new thread instead of continuing under the wrong AGENTS.md."""
        entry = self._by_key.get(key)
        if entry is None or not self._live(entry, time.time() if now is None else now):
            return ""
        if bool(entry.get("plain", False)) != plain:
            return ""
        return str(entry["thread_id"])

    def by_message(self, message_id: int | None, plain: bool = False) -> str:
        entry = self._by_message.get(str(message_id)) if message_id is not None else None
        if entry is None or entry.get("version", "") != self._version:
            return ""
        if bool(entry.get("plain", False)) != plain:
            return ""
        return entry["thread_id"]

    def remember(
        self, key: str, thread_id: str, message_id: int | None = None, plain: bool = False
    ) -> None:
        if not thread_id:
            return
        previous = self._by_key.get(key)
        if previous and previous["thread_id"] != thread_id and not previous.get("harvested"):
            self._pending.append({"key": key, "thread_id": str(previous["thread_id"])})
        # A thread continued after it was harvested (reply to an old answer) has new content;
        # it will be harvested again once it stops being resumable. Consolidation merges dupes.
        self._by_key[key] = {
            "thread_id": thread_id,
            "at": time.time(),
            "version": self._version,
            "plain": plain,
        }
        if message_id is not None:
            self._by_message[str(message_id)] = {
                "thread_id": thread_id,
                "version": self._version,
                "plain": plain,
            }
            while len(self._by_message) > MAX_MESSAGE_LINKS:
                del self._by_message[next(iter(self._by_message))]
        self._save()

    def forget(self, key: str) -> bool:
        entry = self._by_key.pop(key, None)
        if entry is None:
            return False
        if not entry.get("harvested"):
            self._pending.append({"key": key, "thread_id": str(entry["thread_id"])})
        self._save()
        return True

    # ----- harvest ---------------------------------------------------------------------------

    def switched(self, key: str, thread_id: str) -> bool:
        """True when `thread_id` is not the thread currently recorded for `key`, i.e. the next
        `remember()` will retire the old one; callers use it to wake the harvester early."""
        previous = self._by_key.get(key)
        return previous is not None and previous["thread_id"] != thread_id

    def harvest_candidates(self, now: float | None = None) -> list[tuple[str, str]]:
        """(key, thread_id) for every thread that can no longer be resumed and was not harvested."""
        current = time.time() if now is None else now
        found = [(p["key"], p["thread_id"]) for p in self._pending]
        for key, entry in self._by_key.items():
            if not entry.get("harvested") and not self._live(entry, current):
                found.append((key, str(entry["thread_id"])))
        seen: set[str] = set()
        return [c for c in found if not (c[1] in seen or seen.add(c[1]))]

    def mark_harvested(self, thread_id: str) -> None:
        self._pending = [p for p in self._pending if p["thread_id"] != thread_id]
        for entry in self._by_key.values():
            if entry["thread_id"] == thread_id:
                entry["harvested"] = True
        self._save()

    # ----- persistence -----------------------------------------------------------------------

    def _load(self) -> None:
        try:
            data = json.loads(self._path.read_text("utf-8"))
        except (OSError, ValueError):
            return
        self._by_key = dict(data.get("by_key", {}))
        # Entries written before versioning were plain thread ids; they carry no version and
        # therefore never match, which is the intended outcome.
        self._by_message = {
            k: v if isinstance(v, dict) else {"thread_id": str(v), "version": ""}
            for k, v in data.get("by_message", {}).items()
        }
        self._pending = list(data.get("pending", []))

    def _save(self) -> None:
        # Expired entries stay until harvested so their transcript can still be distilled.
        payload = {"by_key": self._by_key, "by_message": self._by_message, "pending": self._pending}
        try:
            self._path.write_text(json.dumps(payload), "utf-8")
        except OSError:
            pass
