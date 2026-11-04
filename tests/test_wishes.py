import sqlite3
from dataclasses import FrozenInstanceError

import pytest

from discord_codex_bot.linkclean import SwitchStore
from discord_codex_bot.wishes import (
    MAX_NOTICE_ATTEMPTS,
    OPEN_STATUSES,
    WishSettings,
    WishStore,
    deliver_notices,
)


@pytest.fixture
def store(tmp_path):
    return WishStore(tmp_path / "state" / "wishes.sqlite3")


def add(store, **changes):
    args = dict(
        guild_id=1,
        channel_id=2,
        message_id=3,
        user_id=4,
        title="標題",
        original="原始需求",
        need="確認需求",
        feasibility="技術評估",
        alternatives="方案",
        asked="確認",
    )
    return store.add(**(args | changes))


def test_store_roundtrip_and_filters(store):
    wish, created = add(store, asked="a" * 1600)
    assert created and wish.original == "原始需求" and wish.need == "確認需求"
    assert len(wish.asked) == 1500 and wish.status == "open" and wish.note == ""
    assert store.get(wish.id) == wish
    with pytest.raises(FrozenInstanceError):
        wish.title = "x"
    second, _ = add(store, guild_id=5)
    other, _ = add(store, user_id=9)
    assert store.list() == [wish, second, other]
    assert store.list(guild_id=1, user_id=4, statuses=OPEN_STATUSES) == [wish]
    assert store.list(limit=1) == [wish]
    assert store.list(statuses=[]) == []
    assert store.get(999) is None
    assert WishStore(store.path).get(wish.id) == wish


def test_duplicates_use_trimmed_title_or_original_within_user_and_guild(store):
    wish, _ = add(store, title=" 標題 ", original=" 原始需求 ")
    assert add(store, title="標題", original="別的原話") == (wish, False)
    assert add(store, title="另一個標題", original="原始需求") == (wish, False)
    for status in ("accepted", "in_progress"):
        wish = store.set_status(wish.id, status)
        assert add(store) == (wish, False)
    assert add(store, user_id=99)[1] is True
    assert add(store, guild_id=99)[1] is True
    store.set_status(wish.id, "done")
    assert add(store)[1] is True


def test_withdraw_is_owned_open_and_does_not_queue_notices(store):
    wish, _ = add(store)
    assert store.withdraw(wish.id, 9) is None
    assert store.withdraw(999, 4) is None
    withdrawn = store.withdraw(wish.id, 4)
    assert withdrawn.status == "withdrawn"
    assert store.withdraw(wish.id, 4) is None and store.pending_notices() == []
    assert add(store)[1]


@pytest.mark.parametrize("status", ["accepted", "in_progress", "done", "rejected"])
def test_status_changes_queue_snapshot_and_progress_notes(store, status):
    wish, _ = add(store)
    changed = store.set_status(wish.id, status, "說明")
    assert changed.status == status and changed.note == "說明"
    assert changed.updated_at >= wish.created_at
    store.set_status(wish.id, status, "補充")
    first, second = store.pending_notices()
    assert first.wish.id == wish.id and first.status == status and first.note == "說明"
    assert second.note == "補充"
    store.mark_delivered(first.id)
    assert store.pending_notices() == [second]
    delivered = store.notices(wish.id)[0]
    assert delivered.state == "delivered" and delivered.delivered_at
    assert delivered.attempts == 1


def test_status_and_notice_are_atomic(store):
    wish, _ = add(store)
    with store._connect() as connection:
        connection.execute(
            "CREATE TRIGGER fail_notice BEFORE INSERT ON wish_notices "
            "BEGIN SELECT RAISE(ABORT, 'test failure'); END"
        )
    with pytest.raises(sqlite3.IntegrityError):
        store.set_status(wish.id, "accepted", "說明")
    assert store.get(wish.id) == wish and store.pending_notices() == []


def test_notices_stop_after_five_failures(store, caplog):
    wish, _ = add(store)
    store.set_status(wish.id, "accepted")
    [notice] = store.pending_notices()
    for attempt in range(1, MAX_NOTICE_ATTEMPTS + 1):
        store.mark_failed(notice.id, "e" * 1000)
        row = store.notices(wish.id)[0]
        assert row.attempts == attempt and len(row.last_error) == 500
        assert bool(store.pending_notices()) == (attempt < MAX_NOTICE_ATTEMPTS)
    assert row.state == "failed" and "failed after 5 attempts" in caplog.text
    store.mark_delivered(notice.id)
    store.mark_failed(notice.id, "again")
    assert store.notices(wish.id)[0] == row


@pytest.mark.parametrize(
    "field,value",
    [
        ("title", ""),
        ("title", "x" * 61),
        ("original", " "),
        ("original", "x" * 1501),
        ("need", ""),
        ("need", "x" * 1501),
        ("feasibility", ""),
        ("feasibility", "x" * 1501),
        ("alternatives", "x" * 1501),
    ],
)
def test_add_validates_attributes_before_writing(store, field, value):
    with pytest.raises(ValueError, match=field):
        add(store, **{field: value})
    assert store.list() == []


def test_status_validates_and_allows_maximum_lengths(store):
    wish, _ = add(
        store,
        title="x" * 60,
        original="x" * 1500,
        need="x" * 1500,
        feasibility="x" * 1500,
        alternatives="x" * 1500,
    )
    for status, note in (("withdrawn", ""), ("unknown", ""), ("open", "x" * 1501)):
        with pytest.raises(ValueError):
            store.set_status(wish.id, status, note)
    with pytest.raises(ValueError, match="exist"):
        store.set_status(999, "open")
    assert store.get(wish.id) == wish and store.pending_notices() == []
    assert store.set_status(wish.id, "open", "x" * 1500).note == "x" * 1500


def test_channel_settings_persist_and_clear_as_empty_row(tmp_path):
    path = tmp_path / "switches.sqlite3"
    settings = WishSettings(SwitchStore(path))
    assert settings.channel(1) is None
    settings.set_channel(1, 22)
    assert WishSettings(SwitchStore(path)).channel(1) == 22
    assert settings.channel(2) is None
    settings.set_channel(1, None)
    assert settings.channel(1) is None
    assert settings.switches._get(1, "wish_channel") == ""


async def test_a_failed_notice_holds_back_its_wish_but_not_other_wishes(store):
    # A member must never read an older status after a newer one (Codex on PR #89).
    wish, _ = add(store)
    other, _ = add(store, title="另一個", original="另一個需求", user_id=5)
    store.set_status(wish.id, "accepted")
    store.set_status(wish.id, "done")
    store.set_status(other.id, "accepted")
    sent, down = [], {"accepted"}

    async def deliver(notice):
        if notice.wish_id == wish.id and notice.status in down:
            raise RuntimeError("unavailable")
        sent.append((notice.wish_id, notice.status))

    await deliver_notices(store, deliver)
    first, second = store.notices(wish.id)
    assert first.state == "pending" and first.attempts == 1
    assert second.state == "pending" and second.attempts == 0  # held, not tried
    assert sent == [(other.id, "accepted")]

    down.clear()
    await deliver_notices(store, deliver)
    assert sent[1:] == [(wish.id, "accepted"), (wish.id, "done")]  # in the order they were set


async def test_a_notice_given_up_on_releases_the_ones_after_it(store):
    wish, _ = add(store)
    store.set_status(wish.id, "accepted")
    store.set_status(wish.id, "done")
    sent = []

    async def deliver(notice):
        if notice.status == "accepted":
            raise RuntimeError("unavailable")
        sent.append(notice.status)

    for _ in range(MAX_NOTICE_ATTEMPTS):
        await deliver_notices(store, deliver)
    assert sent == []
    assert store.notices(wish.id)[0].state == "failed"
    await deliver_notices(store, deliver)
    assert sent == ["done"]
