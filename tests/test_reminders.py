from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from pathlib import Path

from discord_codex_bot.reminders import (
    TAIPEI,
    ReminderStore,
    describe,
    parse_when,
    reminder_loop,
)

NOW = datetime(2026, 9, 13, 14, 0, tzinfo=TAIPEI)  # 2026-09-13 14:00 Taipei


def local(when):
    return when.astimezone(TAIPEI).strftime("%Y-%m-%d %H:%M")


def test_parse_when_relative_clock_and_date() -> None:
    assert local(parse_when("30分鐘後", NOW)) == "2026-09-13 14:30"
    assert local(parse_when("2小時後", NOW)) == "2026-09-13 16:00"
    assert local(parse_when("3天後", NOW)) == "2026-09-16 14:00"
    assert local(parse_when("15 min", NOW)) == "2026-09-13 14:15"
    assert local(parse_when("21:00", NOW)) == "2026-09-13 21:00"
    assert local(parse_when("9:30", NOW)) == "2026-09-14 09:30"  # already past today → tomorrow
    assert local(parse_when("明天 9:30", NOW)) == "2026-09-14 09:30"
    assert local(parse_when("後天下午3點", NOW)) == "2026-09-15 15:00"
    assert local(parse_when("明天晚上8點半", NOW)) == "2026-09-14 20:30"
    assert local(parse_when("今天 23:59", NOW)) == "2026-09-13 23:59"
    assert local(parse_when("9/15 14:30", NOW)) == "2026-09-15 14:30"
    assert local(parse_when("1/2", NOW)) == "2027-01-02 09:00"  # past date without year → next year
    assert local(parse_when("2026-10-01 08:00", NOW)) == "2026-10-01 08:00"
    assert parse_when("等一下", NOW) is None
    assert parse_when("25:00", NOW) is None
    assert parse_when("13/45", NOW) is None
    assert parse_when("30分鐘後", NOW).tzinfo is UTC
    assert describe(parse_when("明天 9:30", NOW)) == "09/14 09:30"


def test_store_add_list_cancel_due_and_persistence(tmp_path: Path) -> None:
    path = tmp_path / "r.json"
    store = ReminderStore(path)
    soon = datetime.now(UTC) + timedelta(minutes=5)
    item = store.add(1, 2, 3, soon, "  收衣服  ")
    assert item["id"] == 1 and item["text"] == "收衣服"
    past = datetime.now(UTC) - timedelta(seconds=1)
    assert store.add(1, 2, 3, past, "x") == "那個時間已經過了。"
    assert "天內" in store.add(1, 2, 3, datetime.now(UTC) + timedelta(days=400), "x")
    later = store.add(1, 2, 3, soon + timedelta(hours=1), "第二個")
    assert [i["id"] for i in store.for_user(3)] == [1, later["id"]]
    assert store.for_user(9) == []
    assert store.cancel(9, 1) is False  # not theirs
    assert store.cancel(3, 1) is True and [i["id"] for i in store.for_user(3)] == [2]
    reloaded = ReminderStore(path)
    assert [i["id"] for i in reloaded.for_user(3)] == [2]
    assert reloaded.add(1, 2, 3, soon, "n")["id"] == 3
    assert reloaded.pop_due(now=datetime.now(UTC)) == []
    due = reloaded.pop_due(now=soon + timedelta(hours=2))
    assert sorted(i["id"] for i in due) == [2, 3] and reloaded.for_user(3) == []


async def test_reminder_loop_fires_due_items_and_survives_a_failure(tmp_path: Path) -> None:
    store = ReminderStore(tmp_path / "r.json")
    store.add(1, 2, 3, datetime.now(UTC) + timedelta(milliseconds=30), "a")
    store.add(1, 2, 3, datetime.now(UTC) + timedelta(milliseconds=30), "b")
    fired = []

    async def fire(item):
        if item["text"] == "a":
            raise RuntimeError("channel gone")
        fired.append(item["text"])

    task = asyncio.get_running_loop().create_task(reminder_loop(store, fire, 0.02))
    await asyncio.sleep(0.15)
    task.cancel()
    assert fired == ["b"] and store.for_user(3) == []
