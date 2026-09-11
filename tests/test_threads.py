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


def test_personal_style_switches_to_the_persona_free_workspace(config: Config) -> None:
    assert "/workspace" in _arguments(config)
    assert "/workspace-plain" in _arguments(config, plain=True)
    assert "--cd" not in _arguments(config, resume="t", plain=True)  # resumed threads keep cwd


def test_style_change_switches_workspace_and_starts_a_new_thread(tmp_path: Path) -> None:
    store = ThreadStore(tmp_path / "threads.json", ttl_seconds=600, version="v1")
    key = ThreadStore.key(1, 2, 3)
    store.remember(key, "plain-thread", message_id=7, plain=True)
    assert store.current(key, plain=True) == "plain-thread"
    assert store.by_message(7, plain=True) == "plain-thread"
    # member clears their personal style -> persona workspace -> the plain thread must not resume
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
