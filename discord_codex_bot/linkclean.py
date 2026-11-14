"""Member-visible link cleaning: the tracking params out of links members share.

The internal side — prompt and tracking ingest — is strip_tracking in links.py. This module is
the member side: what counts as a "links-only" message (the shape B mode may delete) and the
per-guild mode. The mode lives in SQLite, not .env: an operator who sees a bad rewrite changes
it from a slash command without a rebuild.

Modes: ``all`` (default) replaces links-only messages and appends clean links under text
messages; ``links`` only replaces links-only messages and never appends, so a message with
text is left exactly as posted; ``off`` does nothing member-visible. Internal cleanup (prompt,
tracking ingest) is not a mode: it always runs.

The same store keeps two more per-guild knobs, both riding on the linkclean mode for
delivery, so ``off`` silences them too. ``embedfix`` (embedfix.py): whether links are also
swapped to embed-fixer proxies. ``xpages``: what happens to an X post with more media than
one vxtwitter card shows -- ``on`` (default) delivers every page link of the links the Bot
swaps to vxtwitter, ``all`` also adds the missing page links under the vxtwitter / fixvx
links members post themselves, ``off`` keeps one link per post. One knob with three levels
rather than two switches: adding pages to members' links without paging the Bot's own swaps
is no useful setting, and one menu keeps the choice in one place.
"""

from __future__ import annotations

import re
import sqlite3
import time
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path

from .links import strip_tracking, url_spans

# A Discord message is 2000 characters; ten URLs is far past anything a member will paste. This
# is a loop bound, not a behaviour limit.
MAX_URLS = 10
# Repost ids are kept this long; older reposts are still recognised by shape (bot.py
# _replies_to_repost), so the table only has to be exact for recent ones and never grows.
REPOST_RETENTION_DAYS = 30
DISCORD_EPOCH_MS = 1_420_070_400_000
# Discord previews at most five links per message. Page links past that go into overflow
# messages, and one member message gets at most MESSAGES_PER_POST from the Bot in all: the
# repost (or the first appended message) plus overflow. Pages past the cap are dropped (logged).
PREVIEWS_PER_MESSAGE = 5
MESSAGES_PER_POST = 3


def snowflake_before(days: float, now: float | None = None) -> int:
    """The smallest Discord id created `days` ago: ids carry ms since the Discord epoch."""
    at_ms = int(((now if now is not None else time.time()) - days * 86400) * 1000)
    return max(at_ms - DISCORD_EPOCH_MS, 0) << 22


_WORD = re.compile(r"\w")

MODES = {"all": "全部清洗", "links": "只清洗純連結", "off": "全關"}
DEFAULT_MODE = "all"
XPAGES = {
    "on": "補齊 Bot 換的連結",
    "all": "也補成員貼的 vxtwitter／fixvx 連結",
    "off": "關閉（每則貼文一個連結）",
}
DEFAULT_XPAGES = "on"

SCHEMA = """
CREATE TABLE IF NOT EXISTS guild_settings (
    guild_id INTEGER NOT NULL,
    key TEXT NOT NULL,
    value TEXT NOT NULL,
    PRIMARY KEY (guild_id, key)
);
CREATE TABLE IF NOT EXISTS reposts (
    message_id INTEGER PRIMARY KEY
);
"""
# Earlier shapes, carried over once each: a boolean `linkclean(guild_id, enabled)` table,
# then a `linkclean_mode(guild_id, mode)` table. Both collapse into the key/value rows.
MIGRATIONS = {
    "linkclean": """
        INSERT OR IGNORE INTO guild_settings (guild_id, key, value)
            SELECT guild_id, 'linkclean', CASE enabled WHEN 0 THEN 'off' ELSE 'all' END
            FROM linkclean;
        DROP TABLE linkclean;
    """,
    "linkclean_mode": """
        INSERT OR IGNORE INTO guild_settings (guild_id, key, value)
            SELECT guild_id, 'linkclean', mode FROM linkclean_mode;
        DROP TABLE linkclean_mode;
    """,
}


class SwitchStore:
    """The per-guild knobs. A row is an explicit choice; no row means the default (`all`,
    embedfix on), so a fresh deployment does everything and turning it down is opt-in."""

    def __init__(self, path: Path) -> None:
        self.path = path
        path.parent.mkdir(parents=True, exist_ok=True)
        with self._connect() as connection:
            connection.executescript(SCHEMA)
            for table, script in MIGRATIONS.items():
                legacy = connection.execute(
                    "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (table,)
                ).fetchone()
                if legacy:
                    connection.executescript(script)

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.path, timeout=10)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA journal_mode = WAL")
        connection.execute("PRAGMA busy_timeout = 10000")
        return connection

    def _get(self, guild_id: int, key: str) -> str | None:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT value FROM guild_settings WHERE guild_id=? AND key=?", (guild_id, key)
            ).fetchone()
        return None if row is None else str(row["value"])

    def _put(self, guild_id: int, key: str, value: str) -> None:
        with self._connect() as connection:
            connection.execute(
                "INSERT INTO guild_settings (guild_id, key, value) VALUES (?, ?, ?) "
                "ON CONFLICT (guild_id, key) DO UPDATE SET value=excluded.value",
                (guild_id, key, value),
            )

    def mode(self, guild_id: int) -> str:
        return self._get(guild_id, "linkclean") or DEFAULT_MODE

    def set(self, guild_id: int, mode: str) -> None:
        if mode not in MODES:
            raise ValueError(f"unknown linkclean mode {mode!r}")
        self._put(guild_id, "linkclean", mode)

    def embedfix(self, guild_id: int) -> bool:
        return self._get(guild_id, "embedfix") != "off"

    def set_embedfix(self, guild_id: int, enabled: bool) -> None:
        self._put(guild_id, "embedfix", "on" if enabled else "off")

    def xpages(self, guild_id: int) -> str:
        value = self._get(guild_id, "xpages")
        return value if value in XPAGES else DEFAULT_XPAGES

    def set_xpages(self, guild_id: int, value: str) -> None:
        if value not in XPAGES:
            raise ValueError(f"unknown xpages value {value!r}")
        self._put(guild_id, "xpages", value)

    def remember_repost(self, message_id: int) -> None:
        """A message the Bot posted while cleaning links. Discord's "reply" pings the author,
        which puts the Bot in `mentions` exactly like an @; these ids let on_message tell a
        reply-ping to a link repost from a real question (only an explicit @ counts there)."""
        with self._connect() as connection:
            connection.execute(
                "INSERT OR IGNORE INTO reposts (message_id) VALUES (?)", (message_id,)
            )
            connection.execute(
                "DELETE FROM reposts WHERE message_id < ?",
                (snowflake_before(REPOST_RETENTION_DAYS),),
            )

    def is_repost(self, message_id: int | None) -> bool:
        if message_id is None:
            return False
        with self._connect() as connection:
            return (
                connection.execute(
                    "SELECT 1 FROM reposts WHERE message_id=?", (message_id,)
                ).fetchone()
                is not None
            )


def link_spans(content: str) -> list[tuple[int, int, str, bool]]:
    """Each link of `content` as written (links.url_spans) plus whether it lies inside the
    member's ||spoiler bars||. Discord pairs the bars in order, so one pair may cover several
    links; bars inside a link's own text are not bars, and a last unpaired one hides nothing."""
    spans = list(url_spans(content))
    marks: list[int] = []
    cursor = 0
    for start, end, _ in [*spans, (len(content), len(content), "")]:
        marks += [cursor + m.start() for m in re.finditer(r"\|\|", content[cursor:start])]
        cursor = end
    pairs = list(zip(marks[::2], marks[1::2], strict=False))
    return [
        (start, end, url, any(left < start and end <= right for left, right in pairs))
        for start, end, url in spans
    ]


def spoilered(content: str, url: str) -> bool:
    """True when the member put `url` (anywhere it occurs) inside ||spoiler bars||; every link
    the Bot delivers for it must be blurred too."""
    return any(inside for _, _, link, inside in link_spans(content) if link == url)


def deliver(url: str, spoiler: bool) -> str:
    return f"||{url}||" if spoiler else url


@dataclass(frozen=True)
class Link:
    """One link of a member message and how the Bot delivers it."""

    raw: str  # as the member wrote it
    clean: str  # as delivered: tracking stripped, maybe embed-fixed (page 1 when paged)
    spoiler: bool = False  # the site rates the work age-restricted: the Bot spoilers it
    more: tuple[str, ...] = ()  # the further page links, delivered after `clean`
    # `clean` is the member's own link, which already shows page 1 (embedfix.fill): `more`
    # holds the pages it lacks, added even where no cleaned copy of the link is posted.
    own: bool = False


def run_of_links(urls: Sequence[str], spoiler: bool, inside_bars: bool = False) -> str:
    """`urls` as one run of text, each link spoilered on its own when `spoiler`. Inside the
    member's own ||bars|| (`inside_bars`, which wins: the bars are already there) the
    separators close and reopen them, so every page link stays blurred and the member's
    outer bars still close the run."""
    if inside_bars:
        return "|| ||".join(urls)
    return " ".join(deliver(url, spoiler) for url in urls)


def replacement(content: str, links: Sequence[Link]) -> tuple[str, list[str]]:
    """The text that replaces a links-only message (before the author line), and the page
    links that did not fit its previews, already spoilered as needed. The member's own links
    keep their places; further pages follow each one while the message has previews left.
    Inline pages exist only while the links alone leave previews free, so link i is then
    preview i: a paged link past the previews sends its first page to the overflow too."""
    runs: dict[tuple[str, bool], str] = {}
    room = PREVIEWS_PER_MESSAGE - len(links)
    overflow: list[str] = []
    for index, link in enumerate(links):
        bars = spoilered(content, link.raw)
        inline = link.more[: max(room, 0)]
        room -= len(inline)
        for inside in (False, True):  # per occurrence: inside the member's bars or not
            runs[link.raw, inside] = run_of_links((link.clean, *inline), link.spoiler, inside)
        late = (link.clean,) if link.more and index >= PREVIEWS_PER_MESSAGE else ()
        rest = (*late, *link.more[len(inline) :])
        overflow += [deliver(url, link.spoiler or bars) for url in rest]
    return rewrite(content, runs), overflow


def rewrite(content: str, runs: dict[tuple[str, bool], str]) -> str:
    """`content` with every link replaced by `runs[link, inside the member's bars]` when
    present, by the link's own span: no text search, so no link is rewritten inside a longer
    one."""
    parts: list[str] = []
    cursor = 0
    for start, end, url, inside in link_spans(content):
        if (url, inside) in runs:
            parts += [content[cursor:start], runs[url, inside]]
            cursor = end
    return "".join(parts) + content[cursor:]


def appended(content: str, links: Sequence[Link], copies: bool) -> tuple[list[str], list[str]]:
    """The messages posted under a member message that is not replaced, one per link, and the
    page links that did not fit them. `copies` (mode ``all``) posts every changed link with
    its pages. A member's own link (`Link.own`) gets the pages it lacks with or without
    `copies`: they are no copy of anything the member wrote."""
    messages: list[str] = []
    overflow: list[str] = []
    for link in links:
        copy = copies and link.clean != link.raw
        group = ((link.clean,) if copy else ()) + (link.more if copy or link.own else ())
        if not group:
            continue
        wrapped = [deliver(url, link.spoiler or spoilered(content, link.raw)) for url in group]
        messages.append(" ".join(wrapped[:PREVIEWS_PER_MESSAGE]))
        overflow += wrapped[PREVIEWS_PER_MESSAGE:]
    return messages, overflow


def overflow_messages(links: Sequence[str], first: int = 1) -> tuple[list[str], int]:
    """`links` packed PREVIEWS_PER_MESSAGE to a message, within what MESSAGES_PER_POST leaves
    after the `first` messages already posted; plus how many links did not fit."""
    room = max(MESSAGES_PER_POST - first, 0) * PREVIEWS_PER_MESSAGE
    kept = list(links[:room])
    step = PREVIEWS_PER_MESSAGE
    messages = ["\n".join(kept[i : i + step]) for i in range(0, len(kept), step)]
    return messages, len(links) - len(kept)


def is_link_only(content: str, urls: list[str]) -> bool:
    """True when nothing but links, whitespace, punctuation and emoji remains — the message
    whose whole content is the link, and the only shape B mode may delete. `urls` must be the
    raw URLs as written (find_urls with clean=False), so they match the member's text."""
    rest = content
    for url in urls:
        rest = rest.replace(url, " ", 1)
    return _WORD.search(rest) is None


def plan(
    content: str, raw_urls: list[str], clean: list[str] | None = None
) -> tuple[list[str], list[str], bool] | None:
    """What to do about a member message, or None when there is nothing to do.

    `clean` is the delivered form of each raw URL (default: strip_tracking; the Bot also
    passes the embed-fixed form). Returns (all clean URLs, only the changed ones, whether
    the message is links-only). The split matters: a replaced message must repost every
    link (its original is destroyed), but an appended note only needs the ones that
    actually changed."""
    if clean is None:
        clean = [strip_tracking(url) for url in raw_urls]
    if clean == raw_urls:
        return None
    changed = [c for r, c in zip(raw_urls, clean, strict=True) if c != r]
    return clean, changed, is_link_only(content, raw_urls)
