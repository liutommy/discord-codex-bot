"""Member-visible link cleaning: the tracking params out of links members share.

The internal side — prompt and tracking ingest — is strip_tracking in links.py. This module is
the member side: what counts as a "links-only" message (the shape B mode may delete) and the
per-guild mode. The mode lives in SQLite, not .env: an operator who sees a bad rewrite changes
it from a slash command without a rebuild.

Modes: ``all`` (default) replaces links-only messages and appends clean links under text
messages; ``links`` only replaces links-only messages and never appends, so a message with
text is left exactly as posted; ``off`` does nothing member-visible. Internal cleanup (prompt,
tracking ingest) is not a mode: it always runs.

The same store keeps the second per-guild knob, ``embedfix`` (embedfix.py): whether links
are also swapped to embed-fixer proxies before delivery. It rides on the linkclean mode for
delivery, so ``off`` silences both.
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
CREATE TABLE IF NOT EXISTS guild_settings (
    guild_id INTEGER NOT NULL,
    key TEXT NOT NULL,
    value TEXT NOT NULL,
    PRIMARY KEY (guild_id, key)
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
