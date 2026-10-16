"""Operator commands for tracking: track.* act as the member given by --user, through the same
paths as /track and the <track> tags, so the per-member limit, the provider checks and the owner
scoping of the store all still apply; tracking.outbox lists the notifications that have not gone
out yet. The schema format is in admin_server.py."""

from __future__ import annotations

import logging
from typing import Any

from .admin_server import AdminError, Handler, Rejected
from .tracking import MAX_TRACK_MENTIONS, OutboxMessage, TrackerStore, Watch, cadence, parse_times

LOGGER = logging.getLogger(__name__)
DISABLED = "社群追蹤尚未啟用；管理者需設定 TRACKING_ENABLED=true 與 provider 憑證。"
# Watch ids go to SQLite, which cannot bind an int past 64 bits; the <track_*> tags read at most
# nine digits, and so does this.
WATCH_ID = {"type": "int", "min": 1, "max": 999_999_999, "help": "追蹤編號（track list 看得到）"}
MINUTES = {"type": "int", "min": 1, "max": 99_999}  # five digits, as <track_every> reads them
TIMES_HELP = "每天固定判斷的時間（台灣時間），例如 12:01,20:01"
OUTBOX_LIMIT = 50


def _store(client: Any) -> TrackerStore:
    if client.tracker is None:
        raise AdminError(DISABLED)
    return client.tracker


def _times(text: str, flag: str) -> tuple[str, ...]:
    """parse_times, refused when nothing in it is a time: /track refuses it too rather than
    quietly falling back to the interval."""
    times = parse_times(text)
    if not times:
        raise Rejected(f"{flag} 看不懂，請用像 12:01,20:01 這樣的寫法（台灣時間）。")
    return times


def _missing(args: dict) -> AdminError:
    return AdminError(f"使用者 {args['user']} 沒有追蹤 #{args['id']}。")


def _watch_row(store: TrackerStore, watch: Watch) -> dict:
    source = store.get_source(watch.source_id)
    return {
        "id": watch.id,
        "provider": source.provider if source else "?",
        "locator": source.locator if source else "?",
        "guild": watch.guild_id,
        "channel": watch.channel_id,
        "user": watch.user_id,
        "who": list(watch.mention_ids),
        "interval_minutes": watch.interval_minutes,
        "times": list(watch.times),
        "cadence": cadence(watch),
        "interest": watch.interest,
    }


def _watch_line(row: dict) -> str:
    also = f"，另外 @ {'、'.join(str(uid) for uid in row['who'])}" if row["who"] else ""
    return (
        f"#{row['id']} {row['provider']} · {row['locator']}（{row['cadence']}）\n"
        f"　成員 {row['user']} · 伺服器 {row['guild']} · 頻道 {row['channel']}{also}"
    )


async def track_add(client: Any, args: dict) -> dict:
    times = _times(args["at"], "--at") if "at" in args else ()
    text, watch = await client._add_watch(
        args["source"].strip(),
        args.get("interest", ""),
        tuple(args.get("who", ())),
        args["guild"],
        args["channel"],
        args["user"],
        args.get("every", 0),
        times,
    )
    if watch is None:
        raise AdminError(text)
    return {"text": text, "id": watch.id}


async def track_list(client: Any, args: dict) -> dict:
    store = _store(client)
    watches = store.watches(user_id=args.get("user"), active_only=True)
    if "guild" in args:
        watches = [watch for watch in watches if watch.guild_id == args["guild"]]
    rows = [_watch_row(store, watch) for watch in watches]
    if not rows:
        return {"text": "沒有符合的社群追蹤。", "watches": []}
    lines = [f"社群追蹤 {len(rows)} 個：", *(_watch_line(row) for row in rows)]
    return {"text": "\n".join(lines), "watches": rows}


async def track_cancel(client: Any, args: dict) -> dict:
    deleted = _store(client).delete_watch(args["id"], args["user"])
    # Logged like a <cancel_track>: dropping a member's watch leaves a trace by id.
    LOGGER.info(
        "Tracking cancel (admin) user=%s watch=%s -> %s",
        args["user"],
        args["id"],
        "deleted" if deleted else "not found",
    )
    if not deleted:
        raise _missing(args)
    return {"text": f"已取消追蹤 #{args['id']}。", "id": args["id"]}


async def track_every(client: Any, args: dict) -> dict:
    if not _store(client).set_watch_interval(args["id"], args["minutes"], args["user"]):
        raise _missing(args)
    return {
        "text": f"追蹤 #{args['id']} 改成每 {args['minutes']} 分鐘判斷一次。",
        "id": args["id"],
        "interval_minutes": args["minutes"],
    }


async def track_at(client: Any, args: dict) -> dict:
    times = _times(args["times"], "--times")
    if not _store(client).set_watch_times(args["id"], times, args["user"]):
        raise _missing(args)
    when = "、".join(times)
    return {
        "text": f"追蹤 #{args['id']} 改成每天 {when}（台灣時間）判斷，從下一個時間開始。",
        "id": args["id"],
        "times": list(times),
    }


async def track_log(client: Any, args: dict) -> dict:
    store = _store(client)
    watch_id = args.get("id")
    # For an id that is not theirs the member just sees an empty log; the operator is told.
    if watch_id is not None and all(
        watch.id != watch_id for watch in store.watches(user_id=args["user"])
    ):
        raise _missing(args)
    rows = store.recent_decisions(args["user"], args.get("limit", 10), watch_id)
    # Imported here: the CLI loads this module for its schemas and never needs bot.py.
    from .bot import render_decision_log

    return {"text": render_decision_log(rows), "decisions": rows}


def _outbox_row(message: OutboxMessage) -> dict:
    return {
        "id": message.id,
        "attempts": message.attempts,
        "watch": message.watch.id,
        "guild": message.watch.guild_id,
        "channel": message.watch.channel_id,
        "user": message.watch.user_id,
        "category": message.decision.category,
        "title": message.item.title,
        "url": message.item.url,
    }


async def tracking_outbox(client: Any, args: dict) -> dict:
    messages = _store(client).pending_outbox()
    # A failed delivery stays pending with its attempts counted; every tracking pass retries it.
    failed = [message for message in messages if message.attempts]
    listed = failed if args.get("failed") else messages
    limit = args.get("limit", OUTBOX_LIMIT)
    rows = [_outbox_row(message) for message in listed[:limit]]
    lines = [
        f"待送的追蹤通知 {len(messages)} 則，其中 {len(failed)} 則送出失敗過（每輪追蹤都會重試）。"
    ]
    for row in rows:
        state = f"送出失敗 {row['attempts']} 次" if row["attempts"] else "待送"
        title = row["title"].strip()[:120] or "（無標題）"
        lines.append(
            f"#{row['id']} {state} · 追蹤 #{row['watch']} · 成員 {row['user']} · "
            f"頻道 {row['channel']}\n　{title}\n　{row['url']}"
        )
    if len(listed) > limit:
        lines.append(f"（只列出前 {limit} 則，共 {len(listed)} 則）")
    return {
        "text": "\n".join(lines),
        "pending": len(messages),
        "failed": len(failed),
        "messages": rows,
    }


HANDLERS: dict[str, Handler] = {
    "track.add": track_add,
    "track.list": track_list,
    "track.cancel": track_cancel,
    "track.every": track_every,
    "track.at": track_at,
    "track.log": track_log,
    "tracking.outbox": tracking_outbox,
}

SCHEMAS: dict[str, dict] = {
    "track.add": {
        "help": "以成員身分新增社群追蹤（同 /track）",
        "description": (
            "以 --user 的身分新增一個社群追蹤，和 /track、<track> 走同一條路：\n"
            "每人上限、provider 設定與網址檢查都一樣。\n"
            "之後有符合的新內容時，Bot 會在 --channel 發通知並 @ --user（和 --who 的人）；\n"
            "這就是追蹤功能本身。"
        ),
        # Resolving an X account is two xsearch calls of up to XSEARCH_TIMEOUT_SECONDS each.
        "timeout": 600,
        "args": {
            "guild": {"type": "id", "required": True},
            "channel": {"type": "id", "required": True, "help": "通知發在這個頻道"},
            "user": {"type": "id", "required": True, "help": "以這位成員的身分（追蹤的主人）"},
            "source": {
                "type": "str",
                "required": True,
                "max_len": 500,
                "help": "YouTube／Twitch 頻道、X 帳號、露天賣場（可帶 ?q=關鍵字）或網頁網址",
            },
            "interest": {
                "type": "str",
                "max_len": 4000,
                "stdin": True,
                "help": "想知道的內容；不給則用預設的重大事件政策",
            },
            "who": {
                "type": "list[id]",
                "max_items": MAX_TRACK_MENTIONS,
                "help": "通知時另外 @ 的成員",
            },
            "every": {**MINUTES, "help": "每幾分鐘判斷一次（不給則用預設間隔）"},
            "at": {"type": "str", "max_len": 200, "help": TIMES_HELP, "metavar": "HH:MM,…"},
        },
    },
    "track.list": {
        "help": "列出社群追蹤（可依成員、伺服器篩選）",
        "args": {
            "guild": {"type": "id", "help": "只列這個伺服器的"},
            "user": {"type": "id", "help": "只列這位成員的"},
        },
    },
    "track.cancel": {
        "help": "取消成員的一個追蹤（只取消屬於 --user 的）",
        "args": {
            "user": {"type": "id", "required": True, "help": "追蹤的主人"},
            "id": {**WATCH_ID, "required": True},
        },
    },
    "track.every": {
        "help": "把成員的追蹤改成每 N 分鐘判斷一次（取消固定時間）",
        "args": {
            "user": {"type": "id", "required": True, "help": "追蹤的主人"},
            "id": {**WATCH_ID, "required": True},
            "minutes": {**MINUTES, "required": True, "help": "每幾分鐘判斷一次"},
        },
    },
    "track.at": {
        "help": "把成員的追蹤改成每天固定時間判斷",
        "args": {
            "user": {"type": "id", "required": True, "help": "追蹤的主人"},
            "id": {**WATCH_ID, "required": True},
            "times": {
                "type": "str",
                "required": True,
                "max_len": 200,
                "help": TIMES_HELP,
                "metavar": "HH:MM,…",
            },
        },
    },
    "track.log": {
        "help": "看成員追蹤的判斷紀錄（為什麼提醒或沒提醒）",
        "args": {
            "user": {"type": "id", "required": True, "help": "追蹤的主人"},
            "id": {**WATCH_ID, "help": "只看這個追蹤；不給則看全部"},
            "limit": {"type": "int", "min": 1, "max": 25, "help": "最多幾筆（預設 10）"},
        },
    },
    "tracking.outbox": {
        "help": "列出還沒送出的追蹤通知（含送出失敗、等待重試的）",
        "args": {
            "failed": {"type": "bool", "help": "只列送出失敗過的"},
            "limit": {
                "type": "int",
                "min": 1,
                "max": 500,
                "help": f"最多列幾則（預設 {OUTBOX_LIMIT}）",
            },
        },
    },
}
