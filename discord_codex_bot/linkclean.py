"""Member-visible link cleaning: the tracking params out of links members share.

The internal side — prompt and tracking ingest — is strip_tracking in links.py. This module is
the member side: what counts as a "links-only" message (the shape B mode may delete) and the
per-guild mode. The mode lives in SQLite, not .env: an operator who sees a bad rewrite changes
it from a slash command without a rebuild.

Modes: ``all`` (default) replaces links-only messages and appends clean links under text
messages; ``links`` only replaces links-only messages and never appends, so a message with
text is left exactly as posted; ``off`` does nothing member-visible. Internal cleanup (prompt,
tracking ingest) is not a mode: it always runs.
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

MODES = {"all": "全部清洗", "links": "只清洗純連結", "off": "全關"}
DEFAULT_MODE = "all"

SCHEMA = """
CREATE TABLE IF NOT EXISTS linkclean_mode (
    guild_id INTEGER PRIMARY KEY,
    mode TEXT NOT NULL CHECK (mode IN ('off', 'links', 'all'))
);
"""
# The first shape was a boolean `linkclean(guild_id, enabled)` table; carry its rows over once.
MIGRATE = """
INSERT OR IGNORE INTO linkclean_mode (guild_id, mode)
    SELECT guild_id, CASE enabled WHEN 0 THEN 'off' ELSE 'all' END FROM linkclean;
DROP TABLE linkclean;
"""


class SwitchStore:
    """The per-guild mode. A row is an explicit choice; no row means `all`, so a fresh
    deployment cleans by default and turning it down is opt-in, not opt-out."""

    def __init__(self, path: Path) -> None:
        self.path = path
        path.parent.mkdir(parents=True, exist_ok=True)
        with self._connect() as connection:
            connection.executescript(SCHEMA)
            legacy = connection.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' AND name='linkclean'"
            ).fetchone()
            if legacy:
                connection.executescript(MIGRATE)

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.path, timeout=10)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA journal_mode = WAL")
        connection.execute("PRAGMA busy_timeout = 10000")
        return connection

    def mode(self, guild_id: int) -> str:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT mode FROM linkclean_mode WHERE guild_id=?", (guild_id,)
            ).fetchone()
        return DEFAULT_MODE if row is None else str(row["mode"])

    def set(self, guild_id: int, mode: str) -> None:
        if mode not in MODES:
            raise ValueError(f"unknown linkclean mode {mode!r}")
        with self._connect() as connection:
            connection.execute(
                "INSERT INTO linkclean_mode (guild_id, mode) VALUES (?, ?) "
                "ON CONFLICT (guild_id) DO UPDATE SET mode=excluded.mode",
                (guild_id, mode),
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
