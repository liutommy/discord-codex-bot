"""Confirmed feature requests and their bounded status-notification outbox."""

from __future__ import annotations

import asyncio
import logging
import re
import sqlite3
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from .linkclean import SwitchStore

LOGGER = logging.getLogger(__name__)
WISH_DB = "wishes.sqlite3"
MAX_NOTICE_ATTEMPTS = 5
STATUS_LABELS = {
    "open": "待評估",
    "accepted": "已排入",
    "in_progress": "開發中",
    "done": "已完成",
    "rejected": "不做",
    "withdrawn": "已撤回",
}
OPEN_STATUSES = ("open", "accepted", "in_progress")
DEVELOPER_STATUSES = ("open", "accepted", "in_progress", "done", "rejected")

# Read attributes by name; alternatives is twelve characters (tags._TAG's limit).
WISH_TAG = re.compile(r'<wish\b((?:\s+[a-z_]{1,12}="[^"]*")+)\s*/?>(?:\s*</wish>)?')
CANCEL_WISH_TAG = re.compile(
    r'<cancel_wish\b((?:\s+[a-z_]{1,12}="[^"]*")+)\s*/?>(?:\s*</cancel_wish>)?'
)
ATTR = re.compile(r'([a-z_]{1,12})="([^"]*)"')

SCHEMA = """
CREATE TABLE IF NOT EXISTS wishes (
    id INTEGER PRIMARY KEY,
    guild_id INTEGER NOT NULL,
    channel_id INTEGER NOT NULL,
    message_id INTEGER,
    user_id INTEGER NOT NULL,
    title TEXT NOT NULL,
    original TEXT NOT NULL,
    need TEXT NOT NULL,
    feasibility TEXT NOT NULL,
    alternatives TEXT NOT NULL DEFAULT '',
    asked TEXT NOT NULL DEFAULT '',
    status TEXT NOT NULL DEFAULT 'open'
        CHECK (status IN ('open','accepted','in_progress','done','rejected','withdrawn')),
    note TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS wishes_guild_status ON wishes(guild_id, status);
CREATE TABLE IF NOT EXISTS wish_notices (
    id INTEGER PRIMARY KEY,
    wish_id INTEGER NOT NULL REFERENCES wishes(id) ON DELETE CASCADE,
    status TEXT NOT NULL,
    note TEXT NOT NULL DEFAULT '',
    state TEXT NOT NULL DEFAULT 'pending' CHECK (state IN ('pending','delivered','failed')),
    attempts INTEGER NOT NULL DEFAULT 0,
    last_error TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL,
    delivered_at TEXT
);
CREATE INDEX IF NOT EXISTS wish_notices_state_id ON wish_notices(state, id);
"""


@dataclass(frozen=True)
class Wish:
    id: int
    guild_id: int
    channel_id: int
    message_id: int | None
    user_id: int
    title: str
    original: str
    need: str
    feasibility: str
    alternatives: str
    asked: str
    status: str
    note: str
    created_at: str
    updated_at: str


@dataclass(frozen=True)
class WishNotice:
    id: int
    wish_id: int
    status: str
    note: str
    state: str
    attempts: int
    last_error: str
    created_at: str
    delivered_at: str | None
    wish: Wish


def _now() -> str:
    return datetime.now(UTC).isoformat()


def _validate(name: str, value: str, minimum: int, maximum: int) -> str:
    value = value.strip()
    if not minimum <= len(value) <= maximum:
        raise ValueError(f"{name} must contain {minimum}–{maximum} characters")
    return value


class WishStore:
    """Each public operation uses one transaction, including status and notice creation."""

    def __init__(self, path: Path) -> None:
        self.path = path
        path.parent.mkdir(parents=True, exist_ok=True)
        with self._connect() as connection:
            connection.executescript(SCHEMA)

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.path, timeout=10)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        connection.execute("PRAGMA journal_mode = WAL")
        connection.execute("PRAGMA busy_timeout = 10000")
        return connection

    def add(
        self,
        guild_id: int,
        channel_id: int,
        message_id: int | None,
        user_id: int,
        title: str,
        original: str,
        need: str,
        feasibility: str,
        alternatives: str = "",
        asked: str = "",
    ) -> tuple[Wish, bool]:
        title = _validate("title", title, 1, 60)
        original = _validate("original", original, 1, 1500)
        need = _validate("need", need, 1, 1500)
        feasibility = _validate("feasibility", feasibility, 1, 1500)
        alternatives = _validate("alternatives", alternatives, 0, 1500)
        now = _now()
        with self._connect() as connection:
            # Serialize the duplicate check with the insert, including across connections.
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                "SELECT * FROM wishes WHERE guild_id=? AND user_id=? "
                "AND status IN (?,?,?) AND (title=? OR original=?) ORDER BY id LIMIT 1",
                (guild_id, user_id, *OPEN_STATUSES, title, original),
            ).fetchone()
            if row is not None:
                return Wish(**row), False
            cursor = connection.execute(
                "INSERT INTO wishes (guild_id, channel_id, message_id, user_id, title, original, "
                "need, feasibility, alternatives, asked, created_at, updated_at) "
                "VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                (
                    guild_id,
                    channel_id,
                    message_id,
                    user_id,
                    title,
                    original,
                    need,
                    feasibility,
                    alternatives,
                    asked[:1500],
                    now,
                    now,
                ),
            )
            row = connection.execute(
                "SELECT * FROM wishes WHERE id=?", (cursor.lastrowid,)
            ).fetchone()
            return Wish(**row), True

    def get(self, wish_id: int) -> Wish | None:
        with self._connect() as connection:
            row = connection.execute("SELECT * FROM wishes WHERE id=?", (wish_id,)).fetchone()
            return Wish(**row) if row is not None else None

    def list(
        self,
        guild_id: int | None = None,
        statuses: Sequence[str] | None = None,
        user_id: int | None = None,
        limit: int | None = None,
    ) -> list[Wish]:
        clauses, values = [], []
        for key, value in (("guild_id", guild_id), ("user_id", user_id)):
            if value is not None:
                clauses.append(f"{key}=?")
                values.append(value)
        if statuses is not None:
            clauses.append("status IN (" + ",".join("?" for _ in statuses) + ")")
            values.extend(statuses)
        query = "SELECT * FROM wishes"
        if clauses:
            query += " WHERE " + " AND ".join(clauses)
        query += " ORDER BY id"
        if limit is not None:
            query += " LIMIT ?"
            values.append(limit)
        with self._connect() as connection:
            return [Wish(**row) for row in connection.execute(query, values)]

    def withdraw(self, wish_id: int, user_id: int) -> Wish | None:
        with self._connect() as connection:
            cursor = connection.execute(
                "UPDATE wishes SET status='withdrawn', updated_at=? "
                "WHERE id=? AND user_id=? AND status IN (?,?,?)",
                (_now(), wish_id, user_id, *OPEN_STATUSES),
            )
            if not cursor.rowcount:
                return None
            # A queued status update about a wish the member took back is not sent.
            connection.execute(
                "UPDATE wish_notices SET state='failed', last_error='wish withdrawn' "
                "WHERE wish_id=? AND state='pending'",
                (wish_id,),
            )
            return Wish(
                **connection.execute("SELECT * FROM wishes WHERE id=?", (wish_id,)).fetchone()
            )

    def set_status(self, wish_id: int, status: str, note: str = "") -> Wish:
        if status not in DEVELOPER_STATUSES:
            raise ValueError("status is not a developer status")
        note = _validate("note", note, 0, 1500)
        now = _now()
        with self._connect() as connection:
            # A wish its member withdrew stays withdrawn: reopening it would notify them about
            # something they took back.
            cursor = connection.execute(
                "UPDATE wishes SET status=?, note=?, updated_at=? "
                "WHERE id=? AND status!='withdrawn'",
                (status, note, now, wish_id),
            )
            if not cursor.rowcount:
                exists = connection.execute(
                    "SELECT 1 FROM wishes WHERE id=?", (wish_id,)
                ).fetchone()
                raise ValueError(
                    "wish was withdrawn by its member" if exists else "wish does not exist"
                )
            connection.execute(
                "INSERT INTO wish_notices (wish_id, status, note, created_at) VALUES (?,?,?,?)",
                (wish_id, status, note, now),
            )
            return Wish(
                **connection.execute("SELECT * FROM wishes WHERE id=?", (wish_id,)).fetchone()
            )

    @staticmethod
    def _notice(connection: sqlite3.Connection, row: sqlite3.Row) -> WishNotice:
        wish = Wish(
            **connection.execute("SELECT * FROM wishes WHERE id=?", (row["wish_id"],)).fetchone()
        )
        return WishNotice(**row, wish=wish)

    def notices(self, wish_id: int) -> list[WishNotice]:
        with self._connect() as connection:
            return [
                self._notice(connection, row)
                for row in connection.execute(
                    "SELECT * FROM wish_notices WHERE wish_id=? ORDER BY id", (wish_id,)
                )
            ]

    def pending_notices(self) -> list[WishNotice]:
        with self._connect() as connection:
            return [
                self._notice(connection, row)
                for row in connection.execute(
                    "SELECT * FROM wish_notices WHERE state='pending' ORDER BY id"
                )
            ]

    def mark_delivered(self, notice_id: int) -> None:
        with self._connect() as connection:
            connection.execute(
                "UPDATE wish_notices SET state='delivered', attempts=attempts+1, "
                "last_error='', delivered_at=? WHERE id=? AND state='pending'",
                (_now(), notice_id),
            )

    def still_pending(self, notice_id: int) -> bool:
        """The notice is still to be sent and its wish was not withdrawn, as of now."""
        with self._connect() as connection:
            row = connection.execute(
                "SELECT 1 FROM wish_notices n JOIN wishes w ON w.id = n.wish_id "
                "WHERE n.id=? AND n.state='pending' AND w.status!='withdrawn'",
                (notice_id,),
            ).fetchone()
            return row is not None

    def mark_failed(self, notice_id: int, error: str) -> None:
        with self._connect() as connection:
            cursor = connection.execute(
                "UPDATE wish_notices SET attempts=attempts+1, last_error=?, "
                "state=CASE WHEN attempts+1>=? THEN 'failed' ELSE 'pending' END "
                "WHERE id=? AND state='pending' RETURNING state",
                (error[:500], MAX_NOTICE_ATTEMPTS, notice_id),
            )
            row = cursor.fetchone()
            if row is not None and row["state"] == "failed":
                LOGGER.warning(
                    "Wish notice %s failed after %s attempts", notice_id, MAX_NOTICE_ATTEMPTS
                )


class WishSettings:
    """The designated channel, in the existing guild settings database; absent means off."""

    def __init__(self, switches: SwitchStore) -> None:
        self.switches = switches

    def channel(self, guild_id: int) -> int | None:
        value = self.switches._get(guild_id, "wish_channel")
        return int(value) if value else None

    def set_channel(self, guild_id: int, channel_id: int | None) -> None:
        self.switches._put(guild_id, "wish_channel", str(channel_id) if channel_id else "")


async def deliver_notices(
    store: WishStore, deliver: Callable[[WishNotice], Awaitable[None]]
) -> None:
    """One pass, oldest first. A failed notice holds back the later ones of the same wish until
    it is delivered or given up on, so a member never reads an older status after a newer one;
    other wishes' notices still go."""
    held: set[int] = set()
    for notice in store.pending_notices():
        if notice.wish_id in held:
            continue
        # Read again just before sending: an earlier send in this pass can take seconds, and the
        # member may have withdrawn the wish meanwhile (withdraw() fails its pending notices).
        if not store.still_pending(notice.id):
            continue
        try:
            await deliver(notice)
        except Exception as error:
            store.mark_failed(notice.id, str(error))
            held.add(notice.wish_id)
        else:
            store.mark_delivered(notice.id)


async def notice_loop(
    store: WishStore, deliver: Callable[[WishNotice], Awaitable[None]], interval: float = 30
) -> None:
    while True:
        try:
            await deliver_notices(store, deliver)
        except Exception:
            LOGGER.exception("Wish notice pass failed")
        await asyncio.sleep(interval)
