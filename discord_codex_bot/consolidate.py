from __future__ import annotations

import asyncio
import json
import logging
from collections.abc import Awaitable, Callable
from dataclasses import asdict
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from .codex import run_codex
from .config import Config
from .memory import SCOPES, MemoryStore, Note
from .usage import probe_rate_limits

LOGGER = logging.getLogger(__name__)
Runner = Callable[[str], Awaitable[str]]

INSTRUCTIONS = """You are consolidating a memory store for a Discord assistant. Below are
notes, oldest first, each with a name, the date it was written, and its text. Rewrite them into a
clean set:
- Merge duplicates and fragments about the same topic into one note.
- When notes contradict, the newer date wins; drop the superseded statement.
- Keep every fact that is still true. Do not invent facts. Do not drop a note only because it is
  old.
- Keep names short (≤ 30 characters) and texts concise but complete; keep the notes' language.
- Order the result oldest first; give each merged note the date of its newest source.
Return only JSON matching the schema."""


def _batches(notes: list[Note], max_bytes: int) -> list[list[Note]]:
    batches: list[list[Note]] = [[]]
    size = 0
    for note in notes:
        weight = len((note.name + note.date + note.text).encode("utf-8")) + 40
        if batches[-1] and size + weight > max_bytes:
            batches.append([])
            size = 0
        batches[-1].append(note)
        size += weight
    return [batch for batch in batches if batch]


def _parse(answer: str) -> list[Note]:
    data = json.loads(answer)
    return [
        Note(str(item["name"]).strip(), str(item["date"]).strip(), str(item["text"]).strip())
        for item in data["notes"]
        if str(item.get("text", "")).strip()
    ]


async def consolidate_scope(
    store: MemoryStore,
    scope: str,
    guild_id: int,
    user_id: int | None,
    runner: Runner,
    max_input_bytes: int,
) -> tuple[int, int]:
    """Rewrite one scope through the model; returns (notes before, notes after)."""
    notes = store.notes(scope, guild_id, user_id)
    if not notes:
        return 0, 0
    result: list[Note] = []
    for batch in _batches(notes, max_input_bytes):
        payload = json.dumps(
            {"notes": [asdict(note) for note in batch]}, ensure_ascii=False, indent=1
        )
        result.extend(_parse(await runner(f"{INSTRUCTIONS}\n\n{payload}")))
    if not result:
        raise RuntimeError("consolidation returned no notes; keeping the current store")
    store.rewrite(scope, guild_id, user_id, result)
    return len(notes), len(result)


async def consolidate_all(store: MemoryStore, runner: Runner, max_input_bytes: int) -> str:
    lines = []
    for guild_id in store.guild_ids():
        targets = [("guild", None)] + [("user", user_id) for user_id in store.user_ids(guild_id)]
        for scope, user_id in targets:
            label = f"{guild_id}/{SCOPES[scope]}" + (f"/{user_id}" if user_id else "")
            try:
                before, after = await consolidate_scope(
                    store, scope, guild_id, user_id, runner, max_input_bytes
                )
            except Exception as error:  # one bad scope must not stop the rest
                LOGGER.exception("Consolidation failed for %s", label)
                lines.append(f"{label}: failed ({error})")
                continue
            if before:
                lines.append(f"{label}: {before} → {after}")
    return "\n".join(lines) or "nothing to consolidate"


def seconds_until(hour: int, timezone: str, now: datetime | None = None) -> float:
    tz = ZoneInfo(timezone)
    current = now.astimezone(tz) if now else datetime.now(tz)
    target = current.replace(hour=hour, minute=0, second=0, microsecond=0)
    if target <= current:
        target += timedelta(days=1)
    return (target - current).total_seconds()


def codex_runner(config: Config) -> Runner:
    async def run(prompt: str) -> str:
        result = await run_codex(
            prompt, config, raw=True, schema=config.consolidate_schema_path
        )
        return result.text

    return run


async def consolidate_forever(store: MemoryStore, config: Config, queue_run) -> None:
    """Daily at CONSOLIDATE_HOUR local time: if enough 5h quota remains, rewrite every scope."""
    runner = codex_runner(config)
    while True:
        await asyncio.sleep(seconds_until(config.consolidate_hour, config.consolidate_timezone))
        try:
            limits = await queue_run(lambda: probe_rate_limits(config))
            if limits is None:
                LOGGER.warning("Consolidation skipped: rate limits unknown")
                continue
            remaining = 100.0 - limits.primary_used_percent
            if remaining < config.consolidate_min_remaining_percent:
                LOGGER.info(
                    "Consolidation skipped: 5h remaining %.0f%% < %d%%",
                    remaining,
                    config.consolidate_min_remaining_percent,
                )
                continue
            summary = await queue_run(
                lambda: consolidate_all(store, runner, config.consolidate_max_input_bytes)
            )
            LOGGER.info("Consolidation done (5h remaining %.0f%%):\n%s", remaining, summary)
        except Exception:
            LOGGER.exception("Consolidation run failed")
