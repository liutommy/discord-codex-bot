from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from discord_codex_bot.linkclean import (
    DISCORD_EPOCH_MS,
    MAX_URLS,
    REPOST_RETENTION_DAYS,
    Link,
    SwitchStore,
    appended,
    deliver,
    is_link_only,
    overflow_messages,
    plan,
    replacement,
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


def test_xpages_defaults_on_and_persists_explicit_choices(tmp_path: Path) -> None:
    path = tmp_path / "s.sqlite3"
    store = SwitchStore(path)
    assert store.xpages(1) == "on"
    store.set_xpages(1, "all")
    assert SwitchStore(path).xpages(1) == "all" and store.xpages(2) == "on"
    store.set_xpages(1, "off")
    assert store.xpages(1) == "off" and store.mode(1) == "all" and store.embedfix(1)
    with pytest.raises(ValueError):
        store.set_xpages(1, "sometimes")
    with sqlite3.connect(path) as connection:  # a value no release wrote: the default
        connection.execute("UPDATE guild_settings SET value='junk' WHERE key='xpages'")
    assert store.xpages(1) == "on"


def _paged(raw: str, count: int, spoiler: bool = False) -> Link:
    pages = [f"https://vxtwitter.com/u/status/1/p{n}" for n in range(1, count + 1)]
    return Link(raw, pages[0], spoiler, tuple(pages[1:]))


P = [f"https://vxtwitter.com/u/status/1/p{n}" for n in range(0, 21)]  # P[n] is page n


def test_replacement_fills_five_previews_and_overflows_the_rest() -> None:
    raw = "https://x.com/u/status/1"
    assert replacement(raw, [_paged(raw, 2)]) == (f"{P[1]} {P[2]}", [])
    # Two member links: the second takes one of the five previews.
    other = Link("https://a.example/x?utm_source=m", "https://a.example/x")
    text, overflow = replacement(f"{raw} {other.raw}", [_paged(raw, 7), other])
    assert text == f"{P[1]} {P[2]} {P[3]} {P[4]} https://a.example/x"
    assert overflow == P[5:8]


def test_every_page_link_is_spoilered_inside_or_outside_the_members_bars() -> None:
    raw = "https://x.com/u/status/1"
    assert replacement(raw, [_paged(raw, 7, spoiler=True)]) == (
        " ".join(f"||{url}||" for url in P[1:6]),
        [f"||{P[6]}||", f"||{P[7]}||"],
    )
    # The member's bars stay in the text; the separators close and reopen them.
    assert replacement(f"||{raw}||", [_paged(raw, 7)]) == (
        "||" + "|| ||".join(P[1:6]) + "||",
        [f"||{P[6]}||", f"||{P[7]}||"],
    )
    messages, overflow = appended(f"看 ||{raw}||", [_paged(raw, 2)], copies=True)
    assert messages == [f"||{P[1]}|| ||{P[2]}||"] and overflow == []


def test_appended_sends_pages_with_their_link_and_a_members_link_its_missing_pages() -> None:
    raw = "https://x.com/u/status/1"
    member = Link(
        "https://fixvx.com/u/status/1", "https://fixvx.com/u/status/1", more=(P[2],), own=True
    )
    plain = Link("https://b.example/y", "https://b.example/y")
    content = f"看 {raw} {member.raw} {plain.raw}"
    messages, overflow = appended(content, [_paged(raw, 7), member, plain], copies=True)
    assert messages == [" ".join(P[1:6]), P[2]] and overflow == P[6:8]
    # Without copies (mode `links`) a changed link and its pages are not posted, but a
    # member's own link still gets the pages it lacks.
    assert appended(content, [_paged(raw, 7), member, plain], copies=False) == ([P[2]], [])


def test_a_members_own_link_with_tracking_gets_its_pages_without_a_copy() -> None:
    member = Link(
        "https://fixvx.com/u/status/1?utm_source=mail",
        "https://fixvx.com/u/status/1",
        more=(P[2],),
        own=True,
    )
    content = f"看 {member.raw}"
    assert appended(content, [member], copies=False) == ([P[2]], [])
    assert appended(content, [member], copies=True) == ([f"{member.clean} {P[2]}"], [])


def test_a_post_past_the_previews_sends_its_first_page_to_the_overflow() -> None:
    raws = [f"https://x.com/u/status/{n}" for n in range(1, 7)]
    pages = [
        Link(raw, f"https://vxtwitter.com/u/status/{n}/p1", more=(f"{raw}/p2",))
        for n, raw in enumerate(raws, 1)
    ]
    text, overflow = replacement(" ".join(raws), pages)
    assert text == " ".join(link.clean for link in pages)
    sixth = pages[5]
    assert overflow == [link.more[0] for link in pages[:5]] + [sixth.clean, sixth.more[0]]
    # Repost plus two overflow messages carry all twelve pages.
    assert overflow_messages(overflow) == (["\n".join(overflow[:5]), "\n".join(overflow[5:])], 0)
    # A plain link past the previews is not paged: it stays in the text only.
    plain = [Link(f"https://a.example/{n}?s=m", f"https://a.example/{n}") for n in range(6)]
    assert replacement(" ".join(link.raw for link in plain), plain)[1] == []


def test_replacement_rewrites_each_link_by_its_span_not_inside_a_longer_link() -> None:
    raw = "https://x.com/u/status/1"
    photo = Link(f"{raw}/photo/3", "https://vxtwitter.com/u/status/1/photo/3")
    text, overflow = replacement(f"{raw} {photo.raw}", [_paged(raw, 2), photo])
    assert text == f"{P[1]} {P[2]} {photo.clean}" and overflow == []
    # Reversed order, and a repeated link: every copy rewritten, the longer link intact.
    text, _ = replacement(f"{photo.raw} {raw} {raw}", [photo, _paged(raw, 2)])
    assert text == f"{photo.clean} {P[1]} {P[2]} {P[1]} {P[2]}"


def test_overflow_is_capped_at_three_messages_in_all() -> None:
    assert overflow_messages(P[6:21]) == (["\n".join(P[6:11]), "\n".join(P[11:16])], 5)
    assert overflow_messages(P[6:8]) == (["\n".join(P[6:8])], 0)
    assert overflow_messages(P[6:8], first=2) == (["\n".join(P[6:8])], 0)
    assert overflow_messages(P[6:8], first=3) == ([], 2)
    assert overflow_messages([]) == ([], 0)
