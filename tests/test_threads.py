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
