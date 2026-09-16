from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from discord_codex_bot.linkclean import (
    DISCORD_EPOCH_MS,
    MAX_URLS,
    REPOST_RETENTION_DAYS,
    SwitchStore,
    deliver,
    is_link_only,
    plan,
    snowflake_before,
    spoilered,
)
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


def test_switch_migrates_the_earlier_tables_once(tmp_path: Path) -> None:
    path = tmp_path / "linkclean.sqlite3"
    with sqlite3.connect(path) as connection:
        connection.execute("CREATE TABLE linkclean (guild_id INTEGER PRIMARY KEY, enabled INTEGER)")
        connection.executemany("INSERT INTO linkclean VALUES (?, ?)", [(1, 0), (2, 1)])
        connection.execute("CREATE TABLE linkclean_mode (guild_id INTEGER PRIMARY KEY, mode TEXT)")
        connection.executemany("INSERT INTO linkclean_mode VALUES (?, ?)", [(3, "links")])
    store = SwitchStore(path)
    assert (store.mode(1), store.mode(2), store.mode(3), store.mode(4)) == (
        "off",
        "all",
        "links",
        "all",
    )
    with sqlite3.connect(path) as connection:
        tables = {r[0] for r in connection.execute("SELECT name FROM sqlite_master")}
    assert tables & {"linkclean", "linkclean_mode"} == set() and "guild_settings" in tables
    assert SwitchStore(path).mode(1) == "off"  # a second start finds nothing to migrate


def test_reposts_are_remembered_across_restarts_and_old_ids_are_pruned(tmp_path: Path) -> None:
    path = tmp_path / "s.sqlite3"
    store = SwitchStore(path)
    fresh = snowflake_before(0) + 1  # an id minted just now
    stale = snowflake_before(REPOST_RETENTION_DAYS + 1)  # from before the retention window
    assert store.is_repost(None) is False and store.is_repost(fresh) is False
    store.remember_repost(stale)
    assert store.is_repost(stale) is False  # pruned by the very write that stored it
    store.remember_repost(fresh)
    store.remember_repost(fresh)
    assert SwitchStore(path).is_repost(fresh) is True and store.is_repost(stale) is False
    # The Discord epoch and anything earlier collapse to 0, never negative.
    assert snowflake_before(0, now=DISCORD_EPOCH_MS / 1000) == 0
    assert snowflake_before(365 * 20) == 0


def test_embedfix_switch_defaults_on_and_is_independent_of_the_mode(tmp_path: Path) -> None:
    store = SwitchStore(tmp_path / "s.sqlite3")
    assert store.embedfix(1) is True
    store.set_embedfix(1, False)
    assert store.embedfix(1) is False and store.mode(1) == "all"
    store.set(1, "off")
    assert store.embedfix(1) is False
    store.set_embedfix(1, True)
    assert store.embedfix(1) is True and store.mode(1) == "off"


def test_spoilered_links_are_links_only_and_stay_spoilered() -> None:
    content = "||https://a.example/x?utm_source=m||"
    raw = find_urls(content, MAX_URLS, clean=False)
    assert raw == ["https://a.example/x?utm_source=m"]  # the bars are not part of the URL
    assert is_link_only(content, raw)
    assert spoilered(content, raw[0]) and spoilered(
        "|| https://a.example/x?utm_source=m ||", raw[0]
    )
    assert not spoilered("https://a.example/x?utm_source=m ||後面||", raw[0])
    assert deliver("https://a.example/x", True) == "||https://a.example/x||"
    assert deliver("https://a.example/x", False) == "https://a.example/x"


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
    # A caller-supplied delivered form (embed-fixed) counts as a change too.
    assert plan(clean_only, [clean_only], ["https://fixed.example/x"]) == (
        ["https://fixed.example/x"],
        ["https://fixed.example/x"],
        True,
    )
