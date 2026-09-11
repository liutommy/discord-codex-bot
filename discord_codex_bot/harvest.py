from __future__ import annotations

import asyncio
import json
import logging
import re
from collections.abc import Awaitable, Callable

from .codex import run_codex
from .config import Config
from .memory import MemoryStore
from .threads import ThreadStore
from .usage import read_rate_limits

LOGGER = logging.getLogger(__name__)
Runner = Callable[[str], Awaitable[str]]
_USER_MESSAGE = re.compile(r"<USER_MESSAGE>\n?(.*?)\n?</USER_MESSAGE>", re.S)
MAX_TRANSCRIPT_CHARS = 60_000

INSTRUCTIONS = """Below is a finished Discord conversation between one member (後輩) and the
assistant. Extract only what is worth remembering about THIS MEMBER next month: how they want to
be called, likes and dislikes, ongoing projects or plans, relationships they mention, and anything
they explicitly asked to be remembered. Skip one-off questions, facts the assistant merely looked
up or explained, jokes, and anything about the assistant's own persona. Write each item as a short
note in the conversation's language with a name (≤ 30 characters), today's date, and one or two
sentences of text. Return JSON matching the schema; return {"notes": []} when nothing qualifies."""


def rollout_path(config: Config, thread_id: str):
    root = config.codex_home / "sessions"
    if not root.is_dir():
        return None
    matches = list(root.rglob(f"rollout-*-{thread_id}.jsonl"))
    return matches[0] if matches else None


def transcript(config: Config, thread_id: str) -> str:
    """User turns and assistant answers of one thread, as plain text; empty when unavailable."""
    path = rollout_path(config, thread_id)
    if path is None:
        return ""
    turns: list[str] = []
    for line in path.read_text("utf-8", errors="ignore").splitlines():
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            continue
        if event.get("type") != "response_item":
            continue
        payload = event.get("payload") or {}
        if payload.get("type") != "message":
            continue
        text = "\n".join(
            part.get("text", "")
            for part in payload.get("content") or []
            if part.get("type") in ("input_text", "output_text")
        ).strip()
        if not text:
            continue
        if payload.get("role") == "user":
            match = _USER_MESSAGE.search(text)
            if match is None:
                continue  # instruction-only turns (recall results, environment context)
            turns.append(f"後輩：{match.group(1).strip()}")
        elif payload.get("role") == "assistant":
            turns.append(f"前輩：{text}")
    body = "\n\n".join(turns)
    return body[-MAX_TRANSCRIPT_CHARS:] if len(body) > MAX_TRANSCRIPT_CHARS else body


def _parse(answer: str) -> list[tuple[str, str]]:
    data = json.loads(answer)
    return [
        (str(item["name"]).strip(), str(item["text"]).strip())
        for item in data.get("notes", [])
        if str(item.get("text", "")).strip()
    ]


async def harvest_thread(
    store: MemoryStore, config: Config, key: str, thread_id: str, runner: Runner
) -> int:
    """Distil one finished thread into the member's personal memory; returns notes added."""
    guild_id, _channel_id, user_id = (int(part) for part in key.split(":"))
    text = transcript(config, thread_id)
    if not text:
        return 0
    notes = _parse(await runner(f"{INSTRUCTIONS}\n\n<TRANSCRIPT>\n{text}\n</TRANSCRIPT>"))
    for name, body in notes:
        store.add("user", guild_id, user_id, name, body)
    return len(notes)


def codex_runner(config: Config) -> Runner:
    async def run(prompt: str) -> str:
        result = await run_codex(
            prompt, config, raw=True, schema=config.consolidate_schema_path
        )
        return result.text

    return run


async def harvest_forever(
    threads: ThreadStore, store: MemoryStore, config: Config, queue_run
) -> None:
    """Every HARVEST_INTERVAL_MINUTES: distil each no-longer-resumable thread once, when the last
    known 5h reading leaves at least CONSOLIDATE_MIN_REMAINING_PERCENT."""
    runner = codex_runner(config)
    while True:
        await asyncio.sleep(config.harvest_interval_minutes * 60)
        candidates = threads.harvest_candidates()
        if not candidates:
            continue
        if not _quota_ok(config):
            continue
        for key, thread_id in candidates:
            await queue_run(
                lambda k=key, t=thread_id: _harvest_one(threads, store, config, k, t, runner)
            )


def _quota_ok(config: Config) -> bool:
    limits = read_rate_limits(config)
    remaining = 100.0 - limits.primary_used_percent if limits else 100.0
    if remaining < config.consolidate_min_remaining_percent:
        LOGGER.info("Harvest deferred: 5h remaining %.0f%%", remaining)
        return False
    return True


async def _harvest_one(threads, store, config, key, thread_id, runner) -> str:
    try:
        added = await harvest_thread(store, config, key, thread_id, runner)
    except Exception as error:
        LOGGER.exception("Harvest failed for thread %s", thread_id)
        return f"{thread_id[:8]} {key}: failed ({error})"
    threads.mark_harvested(thread_id)
    LOGGER.info("Harvested thread %s for %s: %d notes", thread_id[:8], key, added)
    return f"{thread_id[:8]} {key}: {added} notes"


async def run_once(config: Config, force: bool = False) -> str:
    """Operator entry point: `python -m discord_codex_bot.harvest [--force]` inside the container.
    Harvests every pending thread now; --force ignores the 5h-quota gate."""
    from .bot import instructions_version
    from .memory import MemoryLimits

    threads = ThreadStore(
        config.codex_home / "discord_threads.json",
        config.thread_ttl_minutes * 60,
        instructions_version(config),
    )
    store = MemoryStore(
        config.codex_home / "memory",
        MemoryLimits(
            config.memory_index_max_lines,
            config.memory_index_max_bytes,
            config.memory_user_max_bytes,
            config.memory_guild_max_bytes,
            config.memory_read_max_lines,
            config.memory_read_max_bytes,
            config.memory_search_max_matches,
            config.memory_search_context_lines,
        ),
    )
    candidates = threads.harvest_candidates()
    if not candidates:
        return "nothing to harvest"
    if not force and not _quota_ok(config):
        return "skipped: 5h quota below the gate (use --force to override)"
    runner = codex_runner(config)
    lines = [await _harvest_one(threads, store, config, k, t, runner) for k, t in candidates]
    return "\n".join(lines)


if __name__ == "__main__":
    import sys

    from .config import load_config

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    print(asyncio.run(run_once(load_config(), force="--force" in sys.argv[1:])))
