import json
from dataclasses import replace
from pathlib import Path

from discord_codex_bot.config import Config
from discord_codex_bot.harvest import harvest_thread, transcript
from discord_codex_bot.memory import MemoryLimits, MemoryStore
from discord_codex_bot.threads import ThreadStore

LIMITS = MemoryLimits(200, 25_000, 50_000_000, 200_000_000, 2000, 50_000, 50, 3)


def _rollout(tmp_path: Path, thread_id: str) -> Path:
    day = tmp_path / "sessions" / "2026" / "09" / "11"
    day.mkdir(parents=True, exist_ok=True)
    path = day / f"rollout-2026-09-11T10-00-00-{thread_id}.jsonl"

    def msg(role, kind, text):
        return json.dumps(
            {
                "type": "response_item",
                "payload": {
                    "type": "message",
                    "role": role,
                    "content": [{"type": kind, "text": text}],
                },
            }
        )

    lines = [
        msg("user", "input_text", "rules...\n<USER_MESSAGE>\n我叫小美，最愛抹茶\n</USER_MESSAGE>"),
        msg("assistant", "output_text", "記住了，小美。"),
        msg("user", "input_text", '<RESULT kind="search">...</RESULT>\n\nNow answer.'),
        msg("assistant", "output_text", "抹茶拿鐵好喝。"),
    ]
    path.write_text("\n".join(lines) + "\n", "utf-8")
    return path


def test_transcript_keeps_member_turns_and_answers_only(tmp_path: Path, config: Config) -> None:
    config = replace(config, codex_home=tmp_path)
    assert transcript(config, "missing") == ""
    _rollout(tmp_path, "t1")
    text = transcript(config, "t1")
    assert text == "後輩：我叫小美，最愛抹茶\n\n前輩：記住了，小美。\n\n前輩：抹茶拿鐵好喝。"


async def test_harvest_adds_notes_to_the_member_scope(tmp_path: Path, config: Config) -> None:
    config = replace(config, codex_home=tmp_path)
    _rollout(tmp_path, "t1")
    store = MemoryStore(tmp_path / "memory", LIMITS)
    prompts = []

    async def runner(prompt: str) -> str:
        prompts.append(prompt)
        note = {"name": "暱稱與喜好", "date": "2026-09-11", "text": "叫小美，最愛抹茶"}
        return json.dumps({"notes": [note]})

    added = await harvest_thread(store, config, ThreadStore.key(1, 2, 3), "t1", runner)
    assert added == 1
    assert "<TRANSCRIPT>" in prompts[0] and "最愛抹茶" in prompts[0]
    assert [e.name for e in store.entries("user", 1, 3)] == ["暱稱與喜好"]
    assert store.entries("guild", 1, None) == []
    assert await harvest_thread(store, config, ThreadStore.key(1, 2, 3), "nope", runner) == 0


def test_thread_store_yields_expired_replaced_and_reset_threads_once(tmp_path: Path) -> None:
    store = ThreadStore(tmp_path / "threads.json", ttl_seconds=60, version="v1")
    key = ThreadStore.key(1, 2, 3)
    store.remember(key, "a")
    assert store.harvest_candidates() == []
    store.remember(key, "b")  # replaced -> a is pending
    assert store.harvest_candidates() == [(key, "a")]
    import time

    later = time.time() + 61
    assert store.harvest_candidates(now=later) == [(key, "a"), (key, "b")]
    store.mark_harvested("a")
    store.mark_harvested("b")
    assert store.harvest_candidates(now=later) == []
    store.forget(key)  # already harvested -> nothing new
    assert store.harvest_candidates() == []
    store.remember(key, "c")
    assert store.forget(key) and store.harvest_candidates() == [(key, "c")]
    reloaded = ThreadStore(tmp_path / "threads.json", ttl_seconds=60, version="v1")
    assert reloaded.harvest_candidates() == [(key, "c")]
