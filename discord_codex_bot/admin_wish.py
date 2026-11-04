"""Operator wishlist commands; status changes queue notices for the Bot's delivery loop."""

from __future__ import annotations

from dataclasses import asdict
from typing import Any

from .admin_server import AdminError, Handler, Rejected, require_guild
from .wishes import DEVELOPER_STATUSES, OPEN_STATUSES, STATUS_LABELS, Wish

WISH_ID = {
    "type": "int",
    "min": 1,
    "max": 999_999_999,
    "required": True,
    "help": "願望編號（wish list 可查詢）",
}
GUILD = {"type": "id", "help": "伺服器 id（要在 ALLOWED_GUILD_IDS 內）"}
EG = "--guild 123456789012345678"


def _row(wish: Wish) -> dict:
    row = asdict(wish)
    row["link"] = (
        f"https://discord.com/channels/{wish.guild_id}/{wish.channel_id}/{wish.message_id}"
        if wish.message_id is not None
        else ""
    )
    return row


def _line(wish: Wish) -> str:
    return (
        f"#{wish.id}「{wish.title}」・{STATUS_LABELS[wish.status]}・成員 {wish.user_id}"
        f"・伺服器 {wish.guild_id}・頻道 {wish.channel_id}"
    )


def _wish(client: Any, wish_id: int) -> Wish:
    wish = client.wishes.get(wish_id)
    if wish is None:
        raise AdminError(f"找不到願望 #{wish_id}。")
    require_guild(client, wish.guild_id)
    return wish


async def wish_list(client: Any, args: dict) -> dict:
    status = args.get("status", "open_all")
    statuses = OPEN_STATUSES if status == "open_all" else (status,)
    wishes = client.wishes.list(guild_id=args.get("guild"), statuses=statuses)
    wishes = [wish for wish in wishes if wish.guild_id in client.config.allowed_guild_ids]
    wishes = wishes[: args.get("limit", 50)]
    text = "\n".join([f"許願池（{len(wishes)} 個）：", *(_line(wish) for wish in wishes)])
    return {"text": text, "wishes": [_row(wish) for wish in wishes]}


async def wish_show(client: Any, args: dict) -> dict:
    wish = _wish(client, args["id"])
    notices = [
        {key: value for key, value in asdict(notice).items() if key != "wish"}
        for notice in client.wishes.notices(wish.id)
    ]
    lines = [
        _line(wish),
        f"原始需求：{wish.original}",
        f"確認需求：{wish.need}",
        f"技術可行性：{wish.feasibility}",
        f"其他方案：{wish.alternatives}",
        f"本次訊息：{wish.asked}",
        f"開發者說明：{wish.note}",
        f"訊息連結：{_row(wish)['link']}",
        f"建立時間：{wish.created_at}；更新時間：{wish.updated_at}",
    ]
    lines.extend(
        f"通知 #{row['id']}：{row['state']}，嘗試 {row['attempts']} 次，錯誤：{row['last_error']}"
        for row in notices
    )
    return {"text": "\n".join(lines), "wish": _row(wish), "notices": notices}


async def wish_status(client: Any, args: dict) -> dict:
    wish = _wish(client, args["id"])
    try:
        wish = client.wishes.set_status(wish.id, args["status"], args.get("note", ""))
    except ValueError as error:
        raise Rejected(str(error)) from None
    return {
        "text": f"願望 #{wish.id} 狀態：{STATUS_LABELS[wish.status]}；通知已排入佇列。",
        "wish": _row(wish),
    }


async def wish_channel(client: Any, args: dict) -> dict:
    if args.get("clear") and "channel" in args:
        raise Rejected("--channel 與 --clear 只能擇一。")
    settings = client.wish_settings
    if args.get("clear") or "channel" in args:
        # The reserved channel argument has already passed check_context.
        settings.set_channel(args["guild"], None if args.get("clear") else args["channel"])
    channel = settings.channel(args["guild"])
    text = f"許願頻道：{channel}" if channel else "這個伺服器還沒有設定許願頻道。"
    return {"text": text, "guild": args["guild"], "channel": channel}


HANDLERS: dict[str, Handler] = {
    "wish.list": wish_list,
    "wish.show": wish_show,
    "wish.status": wish_status,
    "wish.channel": wish_channel,
}
GROUPS = {"wish": "許願池：list、show、status、channel"}
SCHEMAS: dict[str, dict] = {
    "wish.list": {
        "help": "列出願望，預設列出允許伺服器中尚未結案的願望",
        "examples": ["wish list", f"wish list {EG} --status open_all --limit 50"],
        "args": {
            "guild": GUILD,
            "status": {
                "type": "choice",
                "choices": [*STATUS_LABELS, "open_all"],
                "help": "狀態（預設 open_all：待評估、已排入、開發中）",
            },
            "limit": {"type": "int", "min": 1, "max": 500, "help": "最多幾筆（預設 50）"},
        },
    },
    "wish.show": {
        "help": "查看完整願望與通知紀錄",
        "examples": ["wish show --id 7"],
        "args": {"id": WISH_ID},
    },
    "wish.status": {
        "help": "更新狀態並排入成員通知；相同狀態可補充進度說明",
        "examples": ['wish status --id 7 --status accepted --note "已排入開發計畫"'],
        "args": {
            "id": WISH_ID,
            "status": {"type": "choice", "required": True, "choices": DEVELOPER_STATUSES},
            "note": {
                "type": "str",
                "max_len": 1500,
                "min_len": 0,
                "stdin": True,
                "help": "開發者說明；通知由 Bot 的迴圈投遞",
            },
        },
    },
    "wish.channel": {
        "help": "查詢、指定或清除伺服器的許願頻道",
        "examples": [
            f"wish channel {EG} --channel 234567890123456789",
            f"wish channel {EG}",
            f"wish channel {EG} --clear",
        ],
        "args": {
            "guild": {**GUILD, "required": True},
            "channel": {"type": "id", "help": "屬於該伺服器且通過開放檢查的頻道"},
            "clear": {"type": "bool", "help": "關閉許願池"},
        },
    },
}
