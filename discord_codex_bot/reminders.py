"""Reminders: a member asks for a nudge at a time; the Bot posts it in the same channel and
@mentions them. Stored as JSON so a restart does not lose them; a loop fires what is due."""

from __future__ import annotations

import asyncio
import json
import logging
import re
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime, timedelta, timezone
from pathlib import Path

LOGGER = logging.getLogger(__name__)
TAIPEI = timezone(timedelta(hours=8))
MAX_PER_USER = 20
MAX_DAYS_AHEAD = 365

_RELATIVE = re.compile(r"^\s*(\d+)\s*(分鐘|分|小時|小时|時|天|日|min|m|h|d)\s*(後|后)?\s*$", re.I)
_DAY_WORDS = {"今天": 0, "明天": 1, "後天": 2, "后天": 2, "大後天": 3}
_CLOCK = re.compile(
    r"^\s*(?:(?P<day>今天|明天|後天|后天|大後天)\s*)?"
    r"(?:(?P<ampm>早上|上午|中午|下午|晚上|凌晨)\s*)?"
    r"(?P<h>\d{1,2})\s*(?:[:：]\s*(?P<m>\d{2})|點\s*(?P<m2>\d{1,2})?\s*分?|點半)?\s*$"
)
# Tags the model appends to an answer: create / cancel reminders on the member's behalf.
REMIND_TAG = re.compile(
    r'<remind\s+when="([^"]{1,60})"\s+text="([^"]{1,300})"(?:\s+who="<?@?!?([0-9]{1,25})>?")?\s*/?>'
    r"(?:\s*</remind>)?"
)
CANCEL_TAG = re.compile(r'<cancel_reminder\s+id="([0-9]{1,9})"\s*/?>(?:\s*</cancel_reminder>)?')
_ISO = re.compile(r"^\s*(\d{4})-(\d{2})-(\d{2})[T ](\d{1,2}):(\d{2})\s*$")
_DATE = re.compile(
    r"^\s*(?:(?P<y>\d{4})[-/.])?(?P<mo>\d{1,2})[-/.](?P<d>\d{1,2})"
    r"(?:\s+(?P<h>\d{1,2})(?:[:：](?P<m>\d{2}))?)?\s*$"
)


def parse_when(text: str, now: datetime | None = None) -> datetime | None:
    """A Taipei-local time expression → aware UTC datetime, or None when not understood.
    Accepts "30分鐘後" / "2小時後" / "3天後", "明天 9:30" / "後天下午3點" / "21:00" (today, else
    tomorrow when already past), and "9/15 14:30" / "2026-10-01 08:00"."""
    now = (now or datetime.now(UTC)).astimezone(TAIPEI)
    text = text.strip()
    if match := _ISO.match(text):  # what a model most reliably produces
        y, mo, d, h, m = (int(g) for g in match.groups())
        try:
            return now.replace(
                year=y, month=mo, day=d, hour=h, minute=m, second=0, microsecond=0
            ).astimezone(UTC)
        except ValueError:
            return None
    if match := _RELATIVE.match(text):
        amount, unit = int(match.group(1)), match.group(2).lower()
        if unit in ("分鐘", "分", "min", "m"):
            delta = timedelta(minutes=amount)
        elif unit in ("小時", "小时", "時", "h"):
            delta = timedelta(hours=amount)
        else:
            delta = timedelta(days=amount)
        return (now + delta).astimezone(UTC)
    if match := _DATE.match(text):
        year = int(match.group("y") or now.year)
        hour = int(match.group("h") or 9)
        minute = int(match.group("m") or 0)
        try:
            when = now.replace(
                year=year, month=int(match.group("mo")), day=int(match.group("d")),
                hour=hour, minute=minute, second=0, microsecond=0,
            )
        except ValueError:
            return None
        if when < now and not match.group("y"):
            when = when.replace(year=year + 1)
        return when.astimezone(UTC)
    if match := _CLOCK.match(text):
        hour = int(match.group("h"))
        minute = int(match.group("m") or match.group("m2") or 0)
        if "點半" in text:
            minute = 30
        ampm = match.group("ampm") or ""
        if ampm in ("下午", "晚上") and hour < 12:
            hour += 12
        if ampm == "中午" and hour < 11:
            hour += 12
        if ampm == "凌晨" and hour == 12:
            hour = 0
        if hour > 23 or minute > 59:
            return None
        when = now.replace(hour=hour, minute=minute, second=0, microsecond=0)
        when += timedelta(days=_DAY_WORDS.get(match.group("day") or "", 0))
        if match.group("day") is None and when <= now:
            when += timedelta(days=1)  # a bare clock time already past today means tomorrow
        return when.astimezone(UTC)
    return None


def extract_reminder_tags(
    answer: str,
) -> tuple[str, list[tuple[str, str, int | None]], list[int]]:
    """(answer without the tags, [(when, text, target_id)], [ids to cancel])."""
    creates = [
        (when, text, int(who) if who else None) for when, text, who in REMIND_TAG.findall(answer)
    ]
    cancels = [int(i) for i in CANCEL_TAG.findall(answer)]
    clean = CANCEL_TAG.sub("", REMIND_TAG.sub("", answer)).strip()
    return clean, creates, cancels


def render_pending(items: list[dict]) -> str:
    """The member's pending reminders as prompt lines (id, Taipei time, text)."""
    def note(item: dict) -> str:
        target = item.get("target_id")
        return f"（提醒 <@{target}>）" if target not in (None, item["user_id"]) else ""

    return "\n".join(
        f"#{i['id']} {describe(datetime.fromisoformat(i['due']))} {i['text']}{note(i)}"
        for i in items
    )


def describe(when: datetime) -> str:
    local = when.astimezone(TAIPEI)
    return local.strftime("%m/%d %H:%M")


class ReminderStore:
    def __init__(self, path: Path) -> None:
        self._path = path
        self._items: list[dict] = []
        self._next_id = 1
        self._load()

    def _load(self) -> None:
        try:
            data = json.loads(self._path.read_text("utf-8"))
        except (OSError, ValueError):
            return
        self._items = [i for i in data.get("items", []) if isinstance(i, dict)]
        self._next_id = int(data.get("next_id", 1))

    def _save(self) -> None:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        payload = {"items": self._items, "next_id": self._next_id}
        self._path.write_text(json.dumps(payload, ensure_ascii=False), "utf-8")

    def add(
        self,
        guild_id: int | None,
        channel_id: int,
        user_id: int,
        when: datetime,
        text: str,
        target_id: int | None = None,
    ) -> dict | str:
        """The stored reminder, or a user-facing reason it was refused."""
        now = datetime.now(UTC)
        if when <= now:
            return "那個時間已經過了。"
        if when - now > timedelta(days=MAX_DAYS_AHEAD):
            return f"最多只能設 {MAX_DAYS_AHEAD} 天內的提醒。"
        if len(self.for_user(user_id)) >= MAX_PER_USER:
            return f"你已經有 {MAX_PER_USER} 個提醒了，先取消幾個。"
        item = {
            "id": self._next_id, "guild_id": guild_id, "channel_id": channel_id,
            "user_id": user_id, "due": when.astimezone(UTC).isoformat(),
            "text": text.strip()[:500], "target_id": target_id or user_id,
        }
        self._next_id += 1
        self._items.append(item)
        self._save()
        return item

    def for_user(self, user_id: int) -> list[dict]:
        return sorted((i for i in self._items if i["user_id"] == user_id), key=lambda i: i["due"])

    def cancel(self, user_id: int, reminder_id: int) -> bool:
        before = len(self._items)
        self._items = [
            i for i in self._items if not (i["id"] == reminder_id and i["user_id"] == user_id)
        ]
        if len(self._items) != before:
            self._save()
            return True
        return False

    def pop_due(self, now: datetime | None = None) -> list[dict]:
        now = now or datetime.now(UTC)
        due = [i for i in self._items if datetime.fromisoformat(i["due"]) <= now]
        if due:
            self._items = [i for i in self._items if i not in due]
            self._save()
        return due


async def reminder_loop(
    store: ReminderStore, fire: Callable[[dict], Awaitable[None]], interval_seconds: float
) -> None:
    while True:
        for item in store.pop_due():
            try:
                await fire(item)
            except Exception:  # one undeliverable reminder must not stop the others
                LOGGER.exception("Reminder %s could not be delivered", item.get("id"))
        await asyncio.sleep(interval_seconds)
