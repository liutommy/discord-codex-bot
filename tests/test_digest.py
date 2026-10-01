import json
import time
from dataclasses import replace
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

from discord_codex_bot import digest
from discord_codex_bot.config import Config
from discord_codex_bot.digest import (
    GUILD_MARK,
    USER_MARK,
    conversations,
    digest_all,
    digest_forever,
    seconds_until_weekly,
)
from discord_codex_bot.harvest import (
    LEDGER_FILE,
    LEDGER_KEEP_DAYS,
    read_ledger,
    record_retired,
)
from discord_codex_bot.memory import MemoryLimits, MemoryStore
from discord_codex_bot.threads import ThreadStore

LIMITS = MemoryLimits(200, 25_000, 50_000_000, 200_000_000, 2000, 50_000, 50, 3)
NOW = time.time()  # ThreadStore.recent() stamps live threads with the real clock
DAY = 86400


def _thread(config: Config, thread_id: str, messages: list[str]) -> None:
    """A Codex rollout whose member turns are `messages`, each answered by the assistant."""
    day = config.codex_home / "sessions" / "2026" / "09" / "28"
    day.mkdir(parents=True, exist_ok=True)

    def line(role: str, kind: str, text: str) -> str:
        payload = {"type": "message", "role": role, "content": [{"type": kind, "text": text}]}
        return json.dumps({"type": "response_item", "payload": payload}, ensure_ascii=False)

    lines = []
    for message in messages:
        lines.append(
            line("user", "input_text", f"rules\n<USER_MESSAGE>\n{message}\n</USER_MESSAGE>")
        )
        lines.append(line("assistant", "output_text", f"前輩說：{message} 很好"))
    (day / f"rollout-2026-09-28T10-00-00-{thread_id}.jsonl").write_text("\n".join(lines), "utf-8")


def _setup(tmp_path: Path, config: Config) -> tuple[Config, MemoryStore]:
    config = replace(config, codex_home=tmp_path / "codex")
    config.codex_home.mkdir(parents=True)
    return config, MemoryStore(tmp_path / "memory", LIMITS)


def test_ledger_records_once_and_drops_old_entries(tmp_path: Path, config: Config) -> None:
    config, _ = _setup(tmp_path, config)
    assert read_ledger(config) == []
    record_retired(config, "1:2:3", "old", now=NOW - (LEDGER_KEEP_DAYS + 1) * DAY)
    record_retired(config, "1:2:3", "a", now=NOW - DAY)
    record_retired(config, "1:2:3", "a", now=NOW)  # re-harvested: one entry, newest time
    with (config.codex_home / LEDGER_FILE).open("a") as ledger:
        ledger.write("not json\n")
    record_retired(config, "1:2:4", "b", now=NOW)
    assert [(e["thread_id"], e["at"]) for e in read_ledger(config)] == [("a", NOW), ("b", NOW)]


def test_seconds_until_weekly_targets_the_next_weekday_hour() -> None:
    tz = "Asia/Taipei"
    wednesday_noon = datetime(2026, 9, 30, 12, 0, tzinfo=ZoneInfo(tz))
    assert seconds_until_weekly(6, 1, tz, wednesday_noon) == (3 * 24 + 13) * 3600  # Sun 01:00
    assert seconds_until_weekly(2, 13, tz, wednesday_noon) == 3600  # later today
    assert seconds_until_weekly(2, 12, tz, wednesday_noon) == 7 * 24 * 3600  # now -> next week


def test_conversations_merges_ledger_and_live_threads_within_the_window(
    tmp_path: Path, config: Config
) -> None:
    config, _ = _setup(tmp_path, config)
    _thread(config, "t-old", ["上個月的事"])
    _thread(config, "t1", ["遊戲王新卡"])
    _thread(config, "t2", ["今天抽卡"])
    _thread(config, "t-dm", ["私訊"])
    record_retired(config, "1:2:3", "t-old", now=NOW - 10 * DAY)
    record_retired(config, "1:2:3", "t1", now=NOW - 2 * DAY)
    record_retired(config, "None:9:3", "t-dm", now=NOW - DAY)  # a DM has no server
    record_retired(config, "1:2:3", "t-gone", now=NOW - DAY)  # transcript pruned
    threads = ThreadStore(config.codex_home / "threads.json", 3600, "v1")
    threads.remember("1:5:3", "t2")
    found = conversations(config, threads, since=NOW - 7 * DAY)
    assert found == {1: {3: [["遊戲王新卡"], ["今天抽卡"]]}}
    # Assistant text is never read, even when it repeats the member's words.
    assert "前輩說" not in json.dumps(found, ensure_ascii=False)


async def test_member_digest_keeps_only_notes_quoted_from_two_conversations(
    tmp_path: Path, config: Config
) -> None:
    config, store = _setup(tmp_path, config)
    store.add("user", 1, 3, "暱稱", "叫小美")
    _thread(config, "t1", ["遊戲王新卡什麼時候出", "謝謝"])
    _thread(config, "t2", ["遊戲王的禁卡表呢"])
    _thread(config, "t3", ["只聊一次的事"])
    for index, thread in enumerate(("t1", "t2"), 1):
        record_retired(config, "1:2:3", thread, now=NOW - (3 - index) * DAY)  # t1 older
    record_retired(config, "1:2:4", "t3", now=NOW - DAY)  # member 4: one conversation only
    prompts = []

    async def runner(prompt: str, scope: str) -> str:
        prompts.append((scope, prompt))
        if scope == "guild":
            return json.dumps({"notes": []})
        good = [
            {"conversation": "C1", "quote": "遊戲王新卡"},
            {"conversation": "C2", "quote": "遊戲王的禁卡"},
        ]
        return json.dumps(
            {
                "notes": [
                    {"name": "常問遊戲王", "text": "似乎常關注遊戲王", "evidence": good},
                    {
                        "name": "同一段兩次",
                        "text": "x",
                        "evidence": [
                            {"conversation": "C1", "quote": "遊戲王新卡"},
                            {"conversation": "C1", "quote": "謝謝"},
                        ],
                    },
                    {
                        "name": "引文對錯段",
                        "text": "x",
                        "evidence": [
                            {"conversation": "C1", "quote": "遊戲王的禁卡"},
                            {"conversation": "C2", "quote": "遊戲王的禁卡"},
                        ],
                    },
                    {
                        "name": "引用前輩",
                        "text": "x",
                        "evidence": [
                            {"conversation": "C1", "quote": "很好"},
                            {"conversation": "C2", "quote": "遊戲王的禁卡"},
                        ],
                    },
                    {
                        "name": "不存在的段",
                        "text": "x",
                        "evidence": [
                            {"conversation": "C1", "quote": "遊戲王新卡"},
                            {"conversation": "C9", "quote": "遊戲王新卡"},
                        ],
                    },
                    {
                        "name": "太短",
                        "text": "x",
                        "evidence": [
                            {"conversation": "C1", "quote": "遊"},
                            {"conversation": "C2", "quote": "遊"},
                        ],
                    },
                    {"name": "", "text": "x", "evidence": good},
                ]
            }
        )

    summary = await digest_all(config, None, store, runner, now=NOW)
    names = [entry.name for entry in store.entries("user", 1, 3)]
    assert names == ["暱稱", f"{USER_MARK}常問遊戲王"]
    assert store.entries("user", 1, 4) == []
    assert summary == "1/個人/…3: +1"
    user_prompts = [prompt for scope, prompt in prompts if scope == "user"]
    assert len(user_prompts) == 1  # member 4 never reached the model
    assert "叫小美" in user_prompts[0]  # existing notes are shown to avoid repeats
    assert '"C1": ["遊戲王新卡什麼時候出", "謝謝"]' in user_prompts[0]  # oldest first


async def test_guild_digest_needs_two_members_and_writes_no_member_notes(
    tmp_path: Path, config: Config
) -> None:
    config, store = _setup(tmp_path, config)
    _thread(config, "a", ["週五開團打副本嗎"])
    _thread(config, "b", ["週五開團我會到"])
    _thread(config, "c", ["我自己喜歡貓"])
    record_retired(config, "1:2:3", "a", now=NOW - DAY)
    record_retired(config, "1:2:4", "b", now=NOW - DAY)
    record_retired(config, "1:2:5", "c", now=NOW - DAY)
    record_retired(config, "7:2:3", "a2", now=NOW - DAY)
    _thread(config, "a2", ["只有一個人的伺服器"])

    async def runner(prompt: str, scope: str) -> str:
        assert scope == "guild"
        assert "<@" not in prompt and "1:2:3" not in prompt  # labels, not member ids
        return json.dumps(
            {
                "notes": [
                    {
                        "name": "週五開團",
                        "text": "伺服器每週五開團",
                        "evidence": [
                            {"member": "M1", "quote": "週五開團"},
                            {"member": "M2", "quote": "週五開團"},
                        ],
                    },
                    {
                        "name": "只有一人",
                        "text": "x",
                        "evidence": [
                            {"member": "M3", "quote": "喜歡貓"},
                            {"member": "M1", "quote": "喜歡貓"},
                        ],
                    },
                ]
            }
        )

    summary = await digest_all(config, None, store, runner, now=NOW)
    assert [e.name for e in store.entries("guild", 1, None)] == [f"{GUILD_MARK}週五開團"]
    assert store.entries("guild", 7, None) == []
    for user_id in (3, 4, 5):
        assert store.entries("user", 1, user_id) == []
    assert summary == "1/伺服器: +1"


async def test_one_failing_scope_does_not_stop_the_rest(tmp_path: Path, config: Config) -> None:
    config, store = _setup(tmp_path, config)
    for thread, key in (("a", "1:2:3"), ("b", "1:2:3"), ("c", "1:2:4")):
        _thread(config, thread, [f"訊息{thread}"])
        record_retired(config, key, thread, now=NOW - DAY)

    async def runner(prompt: str, scope: str) -> str:
        if scope == "user":
            return "not json"
        return json.dumps(
            {
                "notes": [
                    {
                        "name": "n",
                        "text": "t",
                        "evidence": [
                            {"member": "M1", "quote": "訊息a"},
                            {"member": "M2", "quote": "訊息c"},
                        ],
                    }
                ]
            }
        )

    queued = []

    async def queue_run(job):
        queued.append(job)
        return await job()

    summary = await digest_all(config, None, store, runner, queue_run, now=NOW)
    assert summary == "1/個人/…3: failed (JSONDecodeError)\n1/伺服器: +1"
    assert len(queued) == 3  # each scope is its own queue item


async def test_digest_forever_is_off_at_weekday_minus_one(config: Config) -> None:
    await digest_forever(None, None, replace(config, digest_weekday=-1), None)  # returns at once


async def test_codex_runner_isolates_and_picks_the_scope_schema(config: Config, monkeypatch):
    calls = []

    async def fake_batch(prompt, cfg, **kwargs):
        calls.append(kwargs)
        return "{}"

    monkeypatch.setattr(digest, "run_batch", fake_batch)
    runner = digest.codex_runner(config)
    await runner("p", "user")
    await runner("p", "guild")
    assert calls == [
        {"schema": config.digest_user_schema_path, "isolated": True},
        {"schema": config.digest_guild_schema_path, "isolated": True},
    ]


@pytest.mark.parametrize(
    ("name", "label"),
    [("digest-user-schema.json", "conversation"), ("digest-guild-schema.json", "member")],
)
def test_digest_schemas_are_strict_and_require_two_quotes(name: str, label: str) -> None:
    schema = json.loads(Path("config", name).read_text())
    note = schema["properties"]["notes"]["items"]
    assert note["required"] == ["name", "text", "evidence"]
    assert note["additionalProperties"] is False
    evidence = note["properties"]["evidence"]
    assert evidence["minItems"] == 2
    assert evidence["items"]["required"] == [label, "quote"]
    assert evidence["items"]["additionalProperties"] is False


def test_the_model_and_the_consolidator_know_the_markers(config: Config) -> None:
    from discord_codex_bot.codex import _prompt
    from discord_codex_bot.consolidate import INSTRUCTIONS
    from discord_codex_bot.help import FEATURES

    prompt = _prompt("hi")
    assert USER_MARK in prompt and GUILD_MARK in prompt and "soft hint" in prompt
    assert USER_MARK in INSTRUCTIONS and GUILD_MARK in INSTRUCTIONS
    assert any(USER_MARK in line and GUILD_MARK in line for line in FEATURES)


async def test_harvesting_a_thread_records_it_in_the_ledger(tmp_path: Path, config: Config) -> None:
    from discord_codex_bot.harvest import _harvest_one

    config, store = _setup(tmp_path, config)
    _thread(config, "t1", ["隨便問問"])
    threads = ThreadStore(config.codex_home / "threads.json", 3600, "v1")

    async def nothing(prompt: str) -> str:
        return json.dumps({"notes": []})

    await _harvest_one(threads, store, config, "1:2:3", "t1", nothing)
    assert [(e["key"], e["thread_id"]) for e in read_ledger(config)] == [("1:2:3", "t1")]
