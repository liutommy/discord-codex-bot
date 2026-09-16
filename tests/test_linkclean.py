from __future__ import annotations

from pathlib import Path

from discord_codex_bot.linkclean import MAX_URLS, SwitchStore, is_link_only, plan
from discord_codex_bot.links import find_urls


def test_switch_defaults_to_enabled_and_persists_explicit_choices(tmp_path: Path) -> None:
    path = tmp_path / "linkclean.sqlite3"
    store = SwitchStore(path)
    assert store.enabled(1) is True  # no row yet: default on
    store.set(1, False)
    assert store.enabled(1) is False
    assert store.enabled(2) is True  # other guilds are untouched
    store.set(1, True)
    assert SwitchStore(path).enabled(1) is True  # survives a restart
    store.set(2, False)
    store.set(2, False)
    assert SwitchStore(path).enabled(2) is False


def test_is_link_only_covers_emoji_punctuation_and_wrapping() -> None:
    assert is_link_only("https://a.example/x", ["https://a.example/x"])
    assert is_link_only("🔥 快來 https://a.example/x ！！", []) is False  # words remain
    assert is_link_only("🔥 https://a.example/x ！！", ["https://a.example/x"])
    assert is_link_only(
        "https://a.example/x。",
        ["https://a.example/x"],  # rstripped: the trailing 。 stays as punctuation
    )
    assert is_link_only("<https://a.example/x>", ["https://a.example/x"])
    # A markdown link has words around the URL: it is text, not links-only.
    assert is_link_only("[點這裡](https://a.example/x)", ["https://a.example/x"]) is False
    # CJK counts as words too.
    assert is_link_only("連結 https://a.example/x", ["https://a.example/x"]) is False


def test_plan_splits_repost_from_append_and_is_none_when_clean() -> None:
    raw = find_urls(
        "https://a.example/x?utm_source=mail 和 https://b.example/y", MAX_URLS, clean=False
    )
    outcome = plan("https://a.example/x?utm_source=mail 和 https://b.example/y", raw)
    assert outcome == (
        ["https://a.example/x", "https://b.example/y"],
        ["https://a.example/x"],  # only what actually changed
        False,  # text around the links: append, never delete
    )
    dirty_only = "https://a.example/x?utm_source=mail"
    raw_only = find_urls(dirty_only, MAX_URLS, clean=False)
    assert plan(dirty_only, raw_only) == (["https://a.example/x"], ["https://a.example/x"], True)
    clean_only = "https://a.example/x"
    assert plan(clean_only, find_urls(clean_only, MAX_URLS, clean=False)) is None
    assert plan("no links here", []) is None
