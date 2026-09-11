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
