from pathlib import Path

from discord_codex_bot.codex import _arguments
from discord_codex_bot.config import Config
from discord_codex_bot.threads import ThreadStore


def test_thread_store_resumes_within_ttl_and_persists(tmp_path: Path) -> None:
    path = tmp_path / "threads.json"
    store = ThreadStore(path, ttl_seconds=60)
    key = ThreadStore.key(1, 2, 3)
    assert store.current(key) == ""

    store.remember(key, "thread-a", message_id=99)
    assert store.current(key) == "thread-a"
    assert store.by_message(99) == "thread-a"
    assert store.by_message(None) == ""

    reloaded = ThreadStore(path, ttl_seconds=60)
    assert reloaded.current(key) == "thread-a"
    assert reloaded.by_message(99) == "thread-a"

    import time

    assert store.current(key, now=time.time() + 61) == ""
    assert store.forget(key)
    assert not store.forget(key)
    assert store.current(key) == ""


def test_discord_conversation_lookup_survives_switch_and_reload(tmp_path: Path) -> None:
    path = tmp_path / "threads.json"
    store = ThreadStore(path, ttl_seconds=60)
    key = ThreadStore.key(1, 2, 3)
    store.remember(key, "agy-a", 10, model="agy:flash")
    # Before the next answer is recorded, changing the selected model must still find it.
    assert store.current(key, model="codex") == ""
    assert store.conversation_messages(key) == [10]
    store.remember(key, "codex-b", 20, model="codex", route={"earlier": ["agy-a"]})
    store.remember(ThreadStore.key(1, 9, 3), "codex-b", 30)
    store.remember(ThreadStore.key(1, 2, 4), "codex-b", 40)
    reloaded = ThreadStore(path, ttl_seconds=60)
    assert reloaded.conversation_messages(key) == [10, 20]
    # Every answer to this member in this channel counts, whatever thread or style; other
    # members (40) and channels (30) never do.
    store.remember(key, "unrelated", 50, plain=True)
    assert store.conversation_messages(key) == [10, 20, 50]
    assert store.conversation_messages(key, 20) == [10, 20, 50]
    # /reset: the answers before it are no longer this member's context.
    store.forget(key)
    assert store.conversation_messages(key) == []
    assert ThreadStore(path, ttl_seconds=60).conversation_messages(key) == []


def test_discord_lookup_keeps_spare_answers_and_honours_reset(tmp_path: Path) -> None:
    path = tmp_path / "threads.json"
    store = ThreadStore(path, ttl_seconds=60)
    key = ThreadStore.key(1, 2, 3)
    store.remember(key, "codex-a", 10, model="codex")
    # A spare answered on the member's own model: no thread to resume, still a turn to read.
    store.remember(key, "", 11)
    assert store.conversation_messages(key) == [10, 11]
    # A reply to the spare's answer finds the conversation's legacy entries through the live one.
    store._by_message["9"] = {"thread_id": "codex-a", "version": store._version}
    assert store.conversation_messages(key, 11) == [9, 10, 11]
    del store._by_message["9"]
    assert (
        store.by_message(11, model="codex") == "" and store.current(key, model="codex") == "codex-a"
    )
    # After /reset neither the latest conversation nor a reply to an old answer reads them.
    store.forget(key)
    assert store.conversation_messages(key) == []
    assert store.conversation_messages(key, 10) == []
    store.remember(key, "codex-b", 12, model="codex")
    assert ThreadStore(path, ttl_seconds=60).conversation_messages(key, 10) == [12]


def test_reset_also_hides_answers_recorded_before_keys(tmp_path: Path) -> None:
    """An answer written before answers carried their key (legacy) cannot come back through a
    reply after /reset either: the reset is a cutoff on answer ids."""
    path = tmp_path / "threads.json"
    store = ThreadStore(path, ttl_seconds=60)
    key = ThreadStore.key(1, 2, 3)
    store.remember(key, "old", 10, model="codex")
    del store._by_message["10"]["key"]  # as written before this change
    store.remember(key, "old", 11, model="codex")
    assert store.conversation_messages(key, 10) == [10, 11]
    store.forget(key)
    assert store.conversation_messages(key, 10) == []
    assert ThreadStore(path, ttl_seconds=60).conversation_messages(key, 10) == []


def test_instruction_version_change_stops_resuming_old_threads(tmp_path: Path) -> None:
    path = tmp_path / "threads.json"
    old = ThreadStore(path, ttl_seconds=600, version="v1")
    key = ThreadStore.key(1, 2, 3)
    old.remember(key, "thread-a", message_id=99)
    same = ThreadStore(path, ttl_seconds=600, version="v1")
    assert same.current(key) == "thread-a" and same.by_message(99) == "thread-a"
    new = ThreadStore(path, ttl_seconds=600, version="v2")
    assert new.current(key) == "" and new.by_message(99) == ""
    new.remember(key, "thread-b", message_id=100)
    assert new.current(key) == "thread-b" and new.by_message(100) == "thread-b"


def test_legacy_message_links_without_version_are_ignored(tmp_path: Path) -> None:
    path = tmp_path / "threads.json"
    path.write_text('{"by_key": {}, "by_message": {"5": "thread-old"}}', "utf-8")
    store = ThreadStore(path, ttl_seconds=600, version="v1")
    assert store.by_message(5) == ""


def test_resume_arguments_target_the_stored_thread(config: Config) -> None:
    args = _arguments(config, resume="thread-a")
    assert args[:3] == ("exec", "resume", "thread-a")
    assert "--color" not in args and "--cd" not in args
    assert args[-2:] == ("--", "-")
    fresh = _arguments(config)
    assert fresh[:2] == ("exec", "--model")
    assert "--cd" in fresh


def test_persona_opt_out_switches_to_the_persona_free_workspace(config: Config) -> None:
    assert "/workspace" in _arguments(config)
    assert "/workspace-plain" in _arguments(config, plain=True)
    assert "--cd" not in _arguments(config, resume="t", plain=True)  # resumed threads keep cwd


def test_persona_change_switches_workspace_and_starts_a_new_thread(tmp_path: Path) -> None:
    store = ThreadStore(tmp_path / "threads.json", ttl_seconds=600, version="v1")
    key = ThreadStore.key(1, 2, 3)
    store.remember(key, "plain-thread", message_id=7, plain=True)
    assert store.current(key, plain=True) == "plain-thread"
    assert store.by_message(7, plain=True) == "plain-thread"
    # member turns the persona back on -> persona workspace -> the plain thread must not resume
    assert store.current(key, plain=False) == ""
    assert store.by_message(7, plain=False) == ""
    store.remember(key, "persona-thread", message_id=8, plain=False)
    assert store.current(key, plain=False) == "persona-thread"
    assert store.harvest_candidates() == [(key, "plain-thread")]


def test_continued_thread_is_harvested_again_and_switch_is_detected(tmp_path: Path) -> None:
    store = ThreadStore(tmp_path / "threads.json", ttl_seconds=60, version="v1")
    key = ThreadStore.key(1, 2, 3)
    store.remember(key, "a")
    store.mark_harvested("a")
    assert not store.switched(key, "a")
    store.remember(key, "a")  # continued after harvest -> eligible again when it expires
    import time

    assert store.harvest_candidates(now=time.time() + 61) == [(key, "a")]
    assert store.switched(key, "b")
    store.remember(key, "b")
    assert store.harvest_candidates() == [(key, "a")]


def test_entries_without_workspace_flag_are_never_resumed(tmp_path: Path) -> None:
    path = tmp_path / "threads.json"
    path.write_text(
        '{"by_key": {"1:2:3": {"thread_id": "old", "at": 9999999999, "version": "v1"}},'
        ' "by_message": {"5": {"thread_id": "old", "version": "v1"}}, "pending": []}',
        "utf-8",
    )
    store = ThreadStore(path, ttl_seconds=600, version="v1")
    assert store.current("1:2:3", plain=False) == "" and store.current("1:2:3", plain=True) == ""
    assert store.by_message(5, plain=False) == "" and store.by_message(5, plain=True) == ""


def test_message_links_are_capped_and_empty_ids_ignored(tmp_path: Path, monkeypatch) -> None:
    from discord_codex_bot import threads

    monkeypatch.setattr(threads, "MAX_MESSAGE_LINKS", 2)
    store = ThreadStore(tmp_path / "t.json", ttl_seconds=600, version="v1")
    key = ThreadStore.key(1, 2, 3)
    for message_id in (10, 11, 12):
        store.remember(key, f"thread-{message_id}", message_id=message_id)
    assert store.by_message(10) == ""  # oldest link evicted
    assert store.by_message(11) == "thread-11" and store.by_message(12) == "thread-12"
    store.remember(key, "", message_id=13)
    assert store.current(key) == "thread-12" and store.by_message(13) == ""
    assert store.harvest_candidates() == [(key, "thread-10"), (key, "thread-11")]


def test_store_survives_corrupt_or_unwritable_file(tmp_path: Path) -> None:
    path = tmp_path / "t.json"
    path.write_text("{not json", "utf-8")
    store = ThreadStore(path, ttl_seconds=600, version="v1")
    assert store.current("1:2:3") == "" and store.harvest_candidates() == []
    (tmp_path / "file").write_text("", "utf-8")
    unwritable = ThreadStore(tmp_path / "file" / "t.json", ttl_seconds=600, version="v1")
    unwritable.remember("1:2:3", "a", message_id=1)  # OSError swallowed
    assert unwritable.current("1:2:3") == "a"


def test_store_replaces_its_file_whole(tmp_path: Path, monkeypatch) -> None:
    """A save that fails midway leaves the previous file and no scratch file behind."""
    from discord_codex_bot import memory

    path = tmp_path / "t.json"
    store = ThreadStore(path, ttl_seconds=600, version="v1")
    store.remember("1:2:3", "a", message_id=1)
    before = path.read_text("utf-8")

    def interrupted(src, dst):
        raise OSError("disk full")

    monkeypatch.setattr(memory.os, "replace", interrupted)
    store.forget("1:2:3")
    assert path.read_text("utf-8") == before
    assert sorted(p.name for p in tmp_path.iterdir()) == ["t.json"]


def test_reset_of_an_expired_thread_is_harvested_once(tmp_path: Path) -> None:
    import time

    store = ThreadStore(tmp_path / "t.json", ttl_seconds=60, version="v1")
    key = ThreadStore.key(1, 2, 3)
    store.remember(key, "a")
    later = time.time() + 61
    assert store.harvest_candidates(now=later) == [(key, "a")]
    assert store.forget(key)  # pending and by_key both name "a": listed once
    assert store.harvest_candidates(now=later) == [(key, "a")]


def test_live_entry_returns_the_record_only_while_live(tmp_path: Path) -> None:
    store = ThreadStore(tmp_path / "t.json", ttl_seconds=100, version="v1")
    key = ThreadStore.key(1, 2, 3)
    assert store.live_entry(key) is None
    store.remember(key, "t1", None, plain=False, model="codex:x")
    entry = store.live_entry(key)
    assert entry["thread_id"] == "t1" and entry["model"] == "codex:x" and "at" in entry
    assert store.live_entry(key, now=entry["at"] + 101) is None  # past the TTL


def test_a_shared_reply_thread_is_harvested_once_per_member(tmp_path: Path) -> None:
    # Member B replying to the Bot's answer to A continues A's thread (kept on purpose). Each of
    # them harvests it once, for their own turns; one harvest must not retire the other's
    # (Codex on PR #13).
    import time

    store = ThreadStore(tmp_path / "t.json", ttl_seconds=60, version="v1")
    a, b = ThreadStore.key(1, 2, 3), ThreadStore.key(1, 2, 4)
    store.remember(a, "T")
    store.remember(b, "T")
    later = time.time() + 61
    assert store.harvest_candidates(now=later) == [(a, "T"), (b, "T")]
    assert store.keys_for("T") == {a, b}
    store.mark_harvested("T", a)
    assert store.harvest_candidates(now=later) == [(b, "T")]
    store.mark_harvested("T", b)
    assert store.harvest_candidates(now=later) == []
