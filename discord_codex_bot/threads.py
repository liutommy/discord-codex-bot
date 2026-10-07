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
    memory. `harvest_candidates()` lists them, `mark_harvested()` retires them. A member replying
    to the Bot's answer to someone else continues that thread under their own key, so one thread
    can be a candidate once per member; each harvest takes only that member's turns.
    """

    def set_version(self, version: str) -> None:
        """Point at a new instruction version. Every thread started under the old one stops being
        resumable at once, which is the only way a persona change reaches live conversations."""
        self._version = version

    def __init__(self, path: Path, ttl_seconds: float, version: str = "") -> None:
        self._path = path
        self._ttl = ttl_seconds
        self._version = version
        self._by_key: dict[str, dict[str, float | str | bool]] = {}
        self._by_message: dict[str, dict[str, str]] = {}
        self._pending: list[dict[str, str | float]] = []  # key, thread_id, at
        self._load()

    @staticmethod
    def key(guild_id: int | None, channel_id: int | None, user_id: int) -> str:
        return f"{guild_id}:{channel_id}:{user_id}"

    def _live(self, entry: dict, now: float) -> bool:
        return entry.get("version", "") == self._version and now - float(entry["at"]) <= self._ttl

    def live_entry(self, key: str, now: float | None = None) -> dict | None:
        """The member's latest thread record for `key` while it is still within the TTL and the
        current instruction version — whether or not the next request would resume it."""
        entry = self._by_key.get(key)
        if entry is None or not self._live(entry, time.time() if now is None else now):
            return None
        return dict(entry)

    def current(
        self, key: str, now: float | None = None, plain: bool = False, model: str = ""
    ) -> str:
        """The member's resumable thread, if it was started under the same workspace (persona
        vs. persona-free) the next request will use; a style set or cleared in between starts
        a new thread instead of continuing under the wrong AGENTS.md."""
        entry = self._by_key.get(key)
        if entry is None or not self._live(entry, time.time() if now is None else now):
            return ""
        if entry.get("plain") is None or bool(entry["plain"]) != plain:
            return ""  # unknown workspace (pre-flag entry) is never resumed
        if entry.get("model", "") != model:
            return ""  # a Codex thread cannot continue on agy and vice versa
        return str(entry["thread_id"])

    def by_message(self, message_id: int | None, plain: bool = False, model: str = "") -> str:
        entry = self._by_message.get(str(message_id)) if message_id is not None else None
        if entry is None or entry.get("version", "") != self._version:
            return ""
        if entry.get("plain") is None or bool(entry["plain"]) != plain:
            return ""  # unknown workspace (pre-flag entry) is never resumed
        if entry.get("model", "") != model:
            return ""
        return entry["thread_id"]

    def routed(
        self, key: str, message_id: int | None = None, plain: bool = False, now: float | None = None
    ) -> dict | None:
        """The routed conversation a request continues — the answer it replies to, else the
        member's latest thread within the TTL — as {"thread_id", "model", "route"}; None when
        there is none, or it is the member's own model's (no route), or the workspace differs.
        Not filtered by model: the router may move it to another backend, and the caller
        resumes the id only on the backend that created it."""
        entry = self._by_message.get(str(message_id)) if message_id is not None else None
        if entry is None or entry.get("version", "") != self._version:
            entry = self._by_key.get(key)
            if entry is None or not self._live(entry, time.time() if now is None else now):
                return None
        if entry.get("plain") is None or bool(entry["plain"]) != plain or "route" not in entry:
            return None
        return {
            "thread_id": str(entry["thread_id"]),
            "model": str(entry.get("model", "")),
            "route": entry["route"],
        }

    def backend_of(self, thread_id: str) -> str:
        """The backend that created `thread_id` ("codex", "grok", "agy", a router), as recorded
        with it; "" when the store does not know the id."""
        entries = [*self._by_key.values(), *self._by_message.values()]
        for entry in entries:
            if entry.get("thread_id") == thread_id and entry.get("model"):
                return str(entry["model"]).split(":", 1)[0]
        return ""

    def remember(
        self,
        key: str,
        thread_id: str,
        message_id: int | None = None,
        plain: bool = False,
        model: str = "",
        route: dict | None = None,
    ) -> None:
        """`model` is the "<backend>:<model>" that created `thread_id` (only that backend can
        resume it); `route` is where the router has this conversation (routing.Route), absent
        for a member's own model."""
        if not thread_id:
            return
        previous = self._by_key.get(key)
        if previous and previous["thread_id"] != thread_id and not previous.get("harvested"):
            self._pending.append(_retired(key, previous))
        # A thread continued after it was harvested (reply to an old answer) has new content;
        # it will be harvested again once it stops being resumable. Consolidation merges dupes.
        self._by_key[key] = {
            "thread_id": thread_id,
            "at": time.time(),
            "version": self._version,
            "plain": plain,
            "model": model,
            **({"route": route} if route else {}),
        }
        if message_id is not None:
            self._by_message[str(message_id)] = {
                "thread_id": thread_id,
                "version": self._version,
                "plain": plain,
                "model": model,
                **({"route": route} if route else {}),
            }
            while len(self._by_message) > MAX_MESSAGE_LINKS:
                del self._by_message[next(iter(self._by_message))]
        self._save()

    def forget(self, key: str) -> bool:
        entry = self._by_key.pop(key, None)
        if entry is None:
            return False
        if not entry.get("harvested"):
            self._pending.append(_retired(key, entry))
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
        seen: set[tuple[str, str]] = set()
        return [c for c in found if not (c in seen or seen.add(c))]

    def last_active(self, key: str, thread_id: str) -> float | None:
        """When `key` last used `thread_id` (its last remembered turn); None when the store no
        longer knows, or the thread was retired before this was recorded (2026-10-07). A thread
        taken up again and retired twice is pending twice: the latest time counts (Codex on
        PR #39)."""
        times = [
            float(pending["at"])
            for pending in self._pending
            if (pending["key"], pending["thread_id"]) == (key, thread_id) and "at" in pending
        ]
        entry = self._by_key.get(key)
        if entry is not None and entry["thread_id"] == thread_id:
            times.append(float(entry["at"]))
        return max(times, default=None)

    def keys_for(self, thread_id: str) -> set[str]:
        """Every conversation key the store still links to `thread_id` (current or pending);
        more than one member means a shared reply thread."""
        keys = {key for key, entry in self._by_key.items() if entry["thread_id"] == thread_id}
        return keys | {p["key"] for p in self._pending if p["thread_id"] == thread_id}

    def recent(self, since: float) -> list[tuple[str, str, float]]:
        """(key, thread_id, last used) of each conversation's latest thread used since `since`;
        the weekly digest adds these to the harvest ledger, which only has retired threads."""
        return [
            (key, str(entry["thread_id"]), float(entry["at"]))
            for key, entry in self._by_key.items()
            if float(entry["at"]) >= since
        ]

    def mark_harvested(self, thread_id: str, key: str | None = None) -> None:
        """Retire `thread_id` for `key` only (another member on the same thread still has their
        own harvest to come), or for every key when none is given."""
        self._pending = [
            p
            for p in self._pending
            if not (p["thread_id"] == thread_id and key in (None, p["key"]))
        ]
        for entry_key, entry in self._by_key.items():
            if entry["thread_id"] == thread_id and key in (None, entry_key):
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


def _retired(key: str, entry: dict) -> dict:
    """A pending harvest: the thread, and when it was last used (the harvest ledger's time)."""
    return {"key": key, "thread_id": str(entry["thread_id"]), "at": float(entry["at"])}
