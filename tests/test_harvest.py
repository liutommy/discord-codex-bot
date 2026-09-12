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


# ----- agy transcripts, operator entry point, background loop ----------------------------------

import asyncio  # noqa: E402
import contextlib  # noqa: E402

import pytest  # noqa: E402

from discord_codex_bot import harvest  # noqa: E402
from discord_codex_bot.harvest import harvest_forever, run_once  # noqa: E402


def _agy_log(tmp_path: Path, conversation: str, lines: list[str]) -> None:
    logs = tmp_path / ".gemini/antigravity-cli/brain" / conversation / ".system_generated/logs"
    logs.mkdir(parents=True)
    (logs / "transcript.jsonl").write_text("\n".join(lines) + "\n", "utf-8")


async def _one_note(prompt: str) -> str:
    return json.dumps({"notes": [{"name": "n", "date": "2026-09-11", "text": "叫小美"}]})


def test_agy_transcript_keeps_user_messages_and_planner_answers(
    tmp_path: Path, config: Config, monkeypatch
) -> None:
    config = replace(config, codex_home=tmp_path / "codex", agy_home=tmp_path)
    _agy_log(
        tmp_path,
        "conv-1",
        [
            json.dumps(
                {
                    "type": "USER_INPUT",
                    "content": "rules\n<USER_MESSAGE>\n我叫小美\n</USER_MESSAGE>",
                }
            ),
            json.dumps({"type": "USER_INPUT", "content": '<RESULT kind="search"/>\n\nNow answer.'}),
            "not json",
            json.dumps({"type": "PLANNER_RESPONSE", "content": "  記住了。 "}),
            json.dumps({"type": "PLANNER_RESPONSE", "content": ""}),
            json.dumps({"type": "TOOL_CALL", "content": "view_file"}),
            json.dumps({"type": "USER_INPUT", "content": "<USER_MESSAGE>抹茶呢</USER_MESSAGE>"}),
        ],
    )
    assert transcript(config, "conv-1") == "後輩：我叫小美\n\n前輩：記住了。\n\n後輩：抹茶呢"
    assert transcript(config, "conv-missing") == ""
    monkeypatch.setattr(harvest, "MAX_TRANSCRIPT_CHARS", 6)
    assert transcript(config, "conv-1") == "後輩：抹茶呢"  # tail only


def _pending_thread(tmp_path: Path, config: Config, thread_id: str) -> ThreadStore:
    from discord_codex_bot.bot import instructions_version

    threads = ThreadStore(
        tmp_path / "discord_threads.json", 3600, instructions_version(config)
    )
    key = ThreadStore.key(1, 2, 3)
    threads.remember(key, thread_id)
    threads.remember(key, "live")  # replaces -> thread_id is pending
    return threads


async def test_run_once_harvests_pending_threads_and_reports(
    tmp_path: Path, config: Config, monkeypatch
) -> None:
    config = replace(config, codex_home=tmp_path)
    monkeypatch.setattr(harvest, "codex_runner", lambda cfg: _one_note)
    assert await run_once(config) == "nothing to harvest"
    _pending_thread(tmp_path, config, "t1")
    _rollout(tmp_path, "t1")
    assert await run_once(config) == "t1 1:2:3: 1 notes"
    store = MemoryStore(tmp_path / "memory", LIMITS)
    assert [e.name for e in store.entries("user", 1, 3)] == ["n"]
    assert await run_once(config) == "nothing to harvest"  # marked harvested and persisted


async def test_run_once_gate_and_failure_reporting(
    tmp_path: Path, config: Config, monkeypatch
) -> None:
    config = replace(config, codex_home=tmp_path)
    _pending_thread(tmp_path, config, "t1")
    _rollout(tmp_path, "t1")
    usage = {
        "type": "event_msg",
        "payload": {"type": "token_count", "rate_limits": {"primary": {"used_percent": 80.0}}},
    }
    (tmp_path / "sessions" / "rollout-usage.jsonl").write_text(json.dumps(usage) + "\n", "utf-8")
    assert (await run_once(config)).startswith("skipped: 5h quota")

    async def broken(prompt: str) -> str:
        return "not json"

    monkeypatch.setattr(harvest, "codex_runner", lambda cfg: broken)
    assert (await run_once(config, force=True)).startswith("t1 1:2:3: failed (")
    assert (await run_once(config, force=True)).startswith("t1 1:2:3: failed (")  # still pending
    monkeypatch.setattr(harvest, "codex_runner", lambda cfg: _one_note)
    assert await run_once(config, force=True) == "t1 1:2:3: 1 notes"


async def test_harvest_forever_wakes_on_the_event(
    tmp_path: Path, config: Config, monkeypatch
) -> None:
    config = replace(config, codex_home=tmp_path, harvest_interval_minutes=10_000)
    threads = ThreadStore(tmp_path / "threads.json", 3600, "v1")
    key = ThreadStore.key(1, 2, 3)
    threads.remember(key, "t1")
    threads.remember(key, "live")
    _rollout(tmp_path, "t1")
    store = MemoryStore(tmp_path / "memory", LIMITS)
    monkeypatch.setattr(harvest, "codex_runner", lambda cfg: _one_note)
    done = asyncio.Event()
    reports: list[str] = []

    async def queue_run(operation):
        reports.append(await operation())
        done.set()

    wakeup = asyncio.Event()
    task = asyncio.create_task(harvest_forever(threads, store, config, queue_run, wakeup))
    wakeup.set()
    await asyncio.wait_for(done.wait(), timeout=2)
    task.cancel()
    with contextlib.suppress(asyncio.CancelledError):
        await task
    assert reports == ["t1 1:2:3: 1 notes"]
    assert threads.harvest_candidates() == [] and not wakeup.is_set()


async def test_harvest_forever_skips_when_quota_is_low(
    tmp_path: Path, config: Config, monkeypatch
) -> None:
    config = replace(config, codex_home=tmp_path, harvest_interval_minutes=10_000)
    threads = ThreadStore(tmp_path / "threads.json", 3600, "v1")
    key = ThreadStore.key(1, 2, 3)
    threads.remember(key, "t1")
    threads.remember(key, "live")
    monkeypatch.setattr(harvest, "_quota_ok", lambda cfg: False)
    monkeypatch.setattr(harvest, "codex_runner", lambda cfg: _one_note)

    async def queue_run(operation):
        pytest.fail("harvest must not run below the quota gate")

    wakeup = asyncio.Event()
    task = asyncio.create_task(
        harvest_forever(threads, MemoryStore(tmp_path / "m", LIMITS), config, queue_run, wakeup)
    )
    wakeup.set()
    await asyncio.sleep(0.05)
    task.cancel()
    with contextlib.suppress(asyncio.CancelledError):
        await task
    assert threads.harvest_candidates() == [(key, "t1")] and not wakeup.is_set()


async def test_harvest_thread_ignores_a_malformed_key(tmp_path: Path, config: Config) -> None:
    config = replace(config, codex_home=tmp_path)
    _rollout(tmp_path, "t1")
    store = MemoryStore(tmp_path / "memory", LIMITS)
    called = False

    async def runner(prompt: str) -> str:
        nonlocal called
        called = True
        return json.dumps({"notes": [{"name": "n", "date": "2026-09-11", "text": "t"}]})

    assert await harvest_thread(store, config, "not-a-key", "t1", runner) == 0
    assert await harvest_thread(store, config, "1:2", "t1", runner) == 0
    assert not called and store.guild_ids() == []
    assert await harvest_thread(store, config, ThreadStore.key(1, 2, 3), "t1", runner) == 1
