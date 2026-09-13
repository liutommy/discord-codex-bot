import json
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from discord_codex_bot.consolidate import _batches, consolidate_all, seconds_until
from discord_codex_bot.memory import MemoryLimits, MemoryStore, Note
from discord_codex_bot.usage import parse_app_server_rate_limits

LIMITS = MemoryLimits(200, 25_000, 50_000_000, 200_000_000, 2000, 50_000, 50, 3)


async def test_consolidate_rewrites_every_scope_and_keeps_backup(tmp_path: Path) -> None:
    store = MemoryStore(tmp_path, LIMITS)
    store.add("guild", 1, None, "飲料", "週五聚會喝綠茶")
    store.add("guild", 1, None, "飲料改", "週五聚會改喝咖啡")
    store.add("user", 1, 7, "暱稱", "叫我阿明")
    seen: list[str] = []

    async def runner(prompt: str) -> str:
        seen.append(prompt)
        incoming = json.loads(prompt.split("\n\n", 1)[1])["notes"]
        merged = incoming[-1]
        note = {"name": "合併", "date": merged["date"], "text": merged["text"]}
        return json.dumps({"notes": [note]})

    summary = await consolidate_all(store, runner, max_input_bytes=100_000)
    assert "1/伺服器: 2 → 1" in summary and "1/個人/7: 1 → 1" in summary
    assert [e.name for e in store.entries("guild", 1, None)] == ["合併"]
    assert store.notes("guild", 1, None)[0].text == "週五聚會改喝咖啡"
    assert (tmp_path / "1" / "guild" / ".backup" / "topics" / "飲料.md").exists()
    assert len(seen) == 2


async def test_consolidate_failure_keeps_scope_and_continues(tmp_path: Path) -> None:
    store = MemoryStore(tmp_path, LIMITS)
    store.add("guild", 1, None, "a", "keep me")
    store.add("guild", 2, None, "b", "me too")
    calls = 0

    async def runner(prompt: str) -> str:
        nonlocal calls
        calls += 1
        if calls == 1:
            return "not json"
        return json.dumps({"notes": [{"name": "b", "date": "2026-09-11", "text": "me too"}]})

    summary = await consolidate_all(store, runner, max_input_bytes=100_000)
    assert "1/伺服器: failed" in summary and "2/伺服器: 1 → 1" in summary
    assert store.notes("guild", 1, None)[0].text == "keep me"


def test_batches_split_by_bytes() -> None:
    notes = [Note(f"n{i}", "2026-09-11", "x" * 100) for i in range(5)]
    batches = _batches(notes, max_bytes=320)
    assert [len(b) for b in batches] == [2, 2, 1]
    assert _batches([], 300) == []


def test_seconds_until_two_am_taipei() -> None:
    tz = ZoneInfo("Asia/Taipei")
    at_one = datetime(2026, 9, 11, 1, 0, tzinfo=tz)
    assert seconds_until(2, "Asia/Taipei", now=at_one) == 3600
    at_three = datetime(2026, 9, 11, 3, 0, tzinfo=tz)
    assert seconds_until(2, "Asia/Taipei", now=at_three) == 23 * 3600


def test_parse_rate_limits_uses_app_server_window_durations() -> None:
    response = {
        "result": {
            "rateLimits": {
                "primary": {"usedPercent": 50.0, "windowDurationMins": 10_080},
                "secondary": {"usedPercent": 17.0, "windowDurationMins": 300},
            }
        }
    }
    limits = parse_app_server_rate_limits(response)
    assert limits.primary_used_percent == 17.0 and limits.secondary_used_percent == 50.0
    assert limits.source == "app-server account/rateLimits/read"


# ----- _parse and consolidate_scope ----------------------------------------------------------

import pytest  # noqa: E402

from discord_codex_bot.consolidate import _parse, consolidate_scope  # noqa: E402


def test_parse_drops_items_without_text_and_strips() -> None:
    answer = json.dumps(
        {
            "notes": [
                {"name": " a ", "date": " 2026-09-11 ", "text": " keep "},
                {"name": "b", "date": "2026-09-11", "text": "   "},
                {"name": "c", "date": "2026-09-11"},
            ]
        }
    )
    assert _parse(answer) == [Note("a", "2026-09-11", "keep")]
    assert _parse('{"notes": []}') == []
    with pytest.raises(KeyError):
        _parse("{}")
    with pytest.raises(json.JSONDecodeError):
        _parse("not json")


async def test_consolidate_scope_refuses_an_empty_rewrite(tmp_path: Path) -> None:
    store = MemoryStore(tmp_path, LIMITS)
    store.add("user", 1, 7, "暱稱", "叫我阿明")
    calls = 0

    async def empty(prompt: str) -> str:
        nonlocal calls
        calls += 1
        return '{"notes": []}'

    with pytest.raises(RuntimeError, match="returned no notes"):
        await consolidate_scope(store, "user", 1, 7, empty, 100_000)
    assert calls == 1
    assert [n.text for n in store.notes("user", 1, 7)] == ["叫我阿明"]
    assert not (tmp_path / "1" / "users" / "7" / ".backup").exists()
    assert await consolidate_scope(store, "guild", 1, None, empty, 100_000) == (0, 0)
    assert calls == 1  # an empty scope never calls the model


async def test_consolidate_scope_batches_large_input(tmp_path: Path) -> None:
    store = MemoryStore(tmp_path, LIMITS)
    for i in range(4):
        store.add("guild", 1, None, f"n{i}", "x" * 100)
    prompts: list[str] = []

    async def echo(prompt: str) -> str:
        prompts.append(prompt)
        incoming = json.loads(prompt.split("\n\n", 1)[1])["notes"]
        return json.dumps({"notes": incoming})

    assert await consolidate_scope(store, "guild", 1, None, echo, 320) == (4, 4)
    assert len(prompts) == 2
    assert [n.name for n in store.notes("guild", 1, None)] == ["n0", "n1", "n2", "n3"]
