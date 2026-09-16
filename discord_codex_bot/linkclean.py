"""Member-visible link cleaning: the tracking params out of links members share.

The internal side — prompt and tracking ingest — is strip_tracking in links.py. This module is
the member side: what counts as a "links-only" message (the shape B mode may delete) and the
per-guild switch that turns the whole thing off. The switch lives in SQLite, not .env: an
operator who sees a bad rewrite kills it from a slash command without a rebuild.
"""

from __future__ import annotations

import re
import sqlite3
from pathlib import Path

from .links import strip_tracking

# A Discord message is 2000 characters; ten URLs is far past anything a member will paste. This
# is a loop bound, not a behaviour limit.
MAX_URLS = 10
_WORD = re.compile(r"\w")

SCHEMA = """
CREATE TABLE IF NOT EXISTS linkclean (
    guild_id INTEGER PRIMARY KEY,
    enabled INTEGER NOT NULL CHECK (enabled IN (0, 1))
);
"""


class SwitchStore:
    """The per-guild switch. A row is an explicit choice; no row means enabled, so a fresh
    deployment cleans by default and the kill switch is opt-in, not opt-out."""

    def __init__(self, path: Path) -> None:
        self.path = path
        path.parent.mkdir(parents=True, exist_ok=True)
        with self._connect() as connection:
            connection.executescript(SCHEMA)

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.path, timeout=10)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA journal_mode = WAL")
        connection.execute("PRAGMA busy_timeout = 10000")
        return connection

    def enabled(self, guild_id: int) -> bool:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT enabled FROM linkclean WHERE guild_id=?", (guild_id,)
            ).fetchone()
        return True if row is None else bool(row["enabled"])

    def set(self, guild_id: int, enabled: bool) -> None:
        with self._connect() as connection:
            connection.execute(
                "INSERT INTO linkclean (guild_id, enabled) VALUES (?, ?) "
                "ON CONFLICT (guild_id) DO UPDATE SET enabled=excluded.enabled",
                (guild_id, int(enabled)),
            )


def is_link_only(content: str, urls: list[str]) -> bool:
    """True when nothing but links, whitespace, punctuation and emoji remains — the message
    whose whole content is the link, and the only shape B mode may delete. `urls` must be the
    raw URLs as written (find_urls with clean=False), so they match the member's text."""
    rest = content
    for url in urls:
        rest = rest.replace(url, " ", 1)
    return _WORD.search(rest) is None


def plan(content: str, raw_urls: list[str]) -> tuple[list[str], list[str], bool] | None:
    """What to do about a member message, or None when there is nothing to do.

    Returns (all clean URLs, only the changed ones, whether the message is links-only). The
    split matters: a replaced message must repost every link (its original is destroyed), but
    an appended note only needs the ones that actually changed."""
    clean = [strip_tracking(url) for url in raw_urls]
    if clean == raw_urls:
        return None
    changed = [c for r, c in zip(raw_urls, clean, strict=True) if c != r]
    return clean, changed, is_link_only(content, raw_urls)
