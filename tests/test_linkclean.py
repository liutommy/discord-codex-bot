from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from discord_codex_bot.linkclean import MAX_URLS, SwitchStore, is_link_only, plan
from discord_codex_bot.links import find_urls


def test_switch_defaults_to_all_and_persists_explicit_choices(tmp_path: Path) -> None:
    path = tmp_path / "linkclean.sqlite3"
    store = SwitchStore(path)
    assert store.mode(1) == "all"  # no row yet: default on
    store.set(1, "off")
    assert store.mode(1) == "off"
    assert store.mode(2) == "all"  # other guilds are untouched
    store.set(1, "links")
    assert SwitchStore(path).mode(1) == "links"  # survives a restart
    store.set(2, "off")
    store.set(2, "off")
    assert SwitchStore(path).mode(2) == "off"
    with pytest.raises(ValueError):
        store.set(2, "sometimes")


def test_switch_migrates_the_boolean_table_once(tmp_path: Path) -> None:
    path = tmp_path / "linkclean.sqlite3"
    with sqlite3.connect(path) as connection:
        connection.execute("CREATE TABLE linkclean (guild_id INTEGER PRIMARY KEY, enabled INTEGER)")
        connection.executemany("INSERT INTO linkclean VALUES (?, ?)", [(1, 0), (2, 1)])
    store = SwitchStore(path)
    assert (store.mode(1), store.mode(2), store.mode(3)) == ("off", "all", "all")
    with sqlite3.connect(path) as connection:
        tables = {r[0] for r in connection.execute("SELECT name FROM sqlite_master")}
    assert "linkclean" not in tables and "linkclean_mode" in tables
    assert SwitchStore(path).mode(1) == "off"  # a second start finds nothing to migrate


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
