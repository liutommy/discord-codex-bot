"""Operator commands for members' own features, run as the member: remind, memory, model,
style, reset, stop, export and status. The schema format is in admin_server.py.

Each one does what the member's slash command does, through the same store methods and client
helpers, scoped to `--user`: the operator can act for any member, but only on that member's
own reminders, memories and settings. Nothing here posts to a channel; a reminder set here
fires later like any other."""

from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import asdict
from datetime import datetime
from pathlib import Path
from typing import Any

from .admin import write_private
from .admin_server import AdminError, Handler, Rejected
from .backends import AGY, CODEX, GROK, OPENROUTER, ORCAROUTER, split_stored
from .backup import export_memory_zip
from .config import REASONING_EFFORTS
from .memory import SCOPES, STYLE_MAX_CHARS
from .reminders import MAX_DAYS_AHEAD, MAX_PER_USER, describe, parse_when, targets
from .threads import ThreadStore

LOGGER = logging.getLogger(__name__)
PROVIDERS = (GROK, CODEX, AGY, OPENROUTER, ORCAROUTER)
SLASH_TEXT_MAX = 6000  # what a slash command's text option holds
REMINDER_TEXT_MAX = 500  # what ReminderStore keeps of a reminder's text
NAME_MAX = 100
EXPORTS = "exports"
NO_CHANNEL_THREAD = "續接：（續接是每個頻道各自的；加 --channel 看那個頻道）"

GUILD = {"type": "id", "required": True, "help": "伺服器 id（要在 ALLOWED_GUILD_IDS 內）"}
CHANNEL = {
    "type": "id",
    "required": True,
    "help": "頻道或討論串 id（要屬於 --guild，且是 Bot 開放的頻道）",
}
USER = {
    "type": "id",
    "required": True,
    "help": "以這位成員的身分（要是 --guild 的現任成員）",
}
# For the commands that take a channel: the member must also see it.
USER_IN_CHANNEL = {
    **USER,
    "help": "以這位成員的身分（要是 --guild 的現任成員；有 --channel 時要看得到它）",
}
# Whose stored items these are: a member who has left keeps them firing, so they must still
# be reachable (see "owner" in admin_server).
OWNER = {**USER, "owner": True, "help": "設提醒的成員（可以已經離開伺服器）"}
SCOPE = {"type": "choice", "choices": list(SCOPES), "help": "user＝個人記憶；guild＝伺服器記憶"}
# Placeholder ids for the examples: guild, channel, member, another member.
EG = "--guild 123456789012345678"
EGC = f"{EG} --channel 234567890123456789"
EGU = "--user 345678901234567890"


def _with(spec: dict, **changes: Any) -> dict:
    return {**spec, **changes}


# --------------------------------------------------------------------------- remind


def _reminder_line(item: dict) -> str:
    who = targets(item)
    line = (
        f"#{item['id']} {describe(datetime.fromisoformat(item['due']))}"
        f" 伺服器 {item.get('guild_id')} 頻道 {item['channel_id']}"
    )
    if who != [item["user_id"]]:
        line += " → " + "、".join(str(target) for target in who)
    return f"{line} — {item['text']}"


async def remind_add(client: Any, args: dict) -> dict:
    due = parse_when(args["when"])
    if due is None:
        raise Rejected("看不懂 --when；可以寫：30分鐘後、明天 9:30、後天下午3點、9/15 14:30")
    item = client.reminders.add(
        args["guild"], args["channel"], args["user"], due, args["text"], args.get("who")
    )
    if isinstance(item, str):  # the store's own refusal: past, too far, full, a repeat
        raise AdminError(item)
    return {"text": f"⏰ 已設定 {_reminder_line(item)}（台北時間）", "reminder": item}


async def remind_list(client: Any, args: dict) -> dict:
    items = [
        i for i in client.reminders.for_user(args["user"]) if i.get("guild_id") == args["guild"]
    ]
    text = "\n".join(map(_reminder_line, items)) if items else "這位成員沒有提醒。"
    return {"text": text, "reminders": items}


async def remind_cancel(client: Any, args: dict) -> dict:
    # In the guild the operator named, checked like a member: a mistyped id must not reach a
    # reminder the same person set somewhere else.
    mine = [i for i in client.reminders.for_user(args["user"]) if i["id"] == args["id"]]
    if not mine or mine[0].get("guild_id") != args["guild"]:
        raise AdminError(f"找不到這位成員在這個伺服器的提醒 #{args['id']}。")
    if not client.reminders.cancel(args["user"], args["id"]):
        raise AdminError(f"找不到這位成員的提醒 #{args['id']}。")
    return {"text": f"已取消提醒 #{args['id']}。", "id": args["id"]}


# --------------------------------------------------------------------------- memory


def _owner(args: dict, scope: str) -> int | None:
    """The member a scope belongs to: required for a personal memory, unused for the server's."""
    if scope == "user" and "user" not in args:
        raise Rejected("個人記憶（--scope user）要給 --user")
    return args.get("user") if scope == "user" else None


async def memory_add(client: Any, args: dict) -> dict:
    scope = args["scope"]
    line = client.memory.add(scope, args["guild"], _owner(args, scope), args["name"], args["text"])
    return {"text": f"已記住：{line}", "line": line}


async def memory_forget(client: Any, args: dict) -> dict:
    """By file name, else by exact name, as /forget does; a name two notes share deletes
    nothing, so the operator picks one by its file."""
    scope, guild = args["scope"], args["guild"]
    owner = _owner(args, scope)
    name = args["name"].strip()
    entries = client.memory.all_entries(scope, guild, owner)
    matches = [e for e in entries if e.file == name] or [e for e in entries if e.name == name]
    if len(matches) > 1:
        raise AdminError(
            "有多條同名記憶，沒有刪除任何資料；請改用 memory list 括號裡的檔名（.md）指定。"
        )
    if not matches or not client.memory.forget(scope, guild, owner, matches[0].file, by_file=True):
        raise AdminError(f"找不到這則{SCOPES[scope]}記憶，沒有刪除任何資料；名稱見 memory list。")
    entry = matches[0]
    LOGGER.info("Memory forget guild=%s user=%s scope=%s by admin", guild, owner, scope)
    return {
        "text": f"已刪除{SCOPES[scope]}記憶「{entry.name}」（{entry.file}）。",
        "name": entry.name,
        "file": entry.file,
    }


async def memory_list(client: Any, args: dict) -> dict:
    guild, user = args["guild"], args.get("user")
    scope = args.get("scope", "")
    if user is None:
        if scope == "user":
            raise Rejected("個人記憶（--scope user）要給 --user")
        scope = "guild"  # without a member only the server's memory is there to list
    entries = {
        kind: [asdict(entry) for entry in client.memory.all_entries(kind, guild, user)]
        for kind in ((scope,) if scope else SCOPES)
    }
    return {"text": client._memory_text(guild, user, scope), "entries": entries}


# --------------------------------------------------------------------------- model


def _backend(stored: str) -> str:
    return split_stored(stored)[0].split(":")[0]


async def model_get(client: Any, args: dict) -> dict:
    stored = client.memory.get_model(args["guild"], args["user"])
    current = stored or client.config.default_model
    origin = "" if stored else "（預設）"
    text = f"目前{origin}：{client._describe(*split_stored(current))}"
    return {"text": text, "stored": stored or None}


async def model_set(client: Any, args: dict) -> dict:
    """/model with options filled in: --value picks a model (checked against the lists the
    menu offers), --provider alone takes its default model, --effort sets the level."""
    guild, user = args["guild"], args["user"]
    provider, value, effort = args.get("provider"), args.get("value"), args.get("effort")
    if provider is None and value is None and effort is None:
        raise Rejected("要給 --provider、--value 或 --effort 至少一個")
    saved, why = await client._change_model(guild, user, provider, value, effort)
    if not saved and value is None:
        raise Rejected(f"{why}，請用 --value 指定模型")
    if not saved:
        # The CLI has no menu to pick from: name what the provider offers instead.
        named = value.strip().split(":", 1)[0]
        current = _backend(client.memory.get_model(guild, user))
        listed = named if named in PROVIDERS else provider or current
        offered = [option.value for option in await client.model_options(listed, "")]
        raise Rejected(why + (f"；可選：{', '.join(offered[:10])}" if offered else ""))
    return {"text": f"已設定：{client._describe(*split_stored(saved))}", "stored": saved}


async def model_clear(client: Any, args: dict) -> dict:
    guild, user = args["guild"], args["user"]
    cleared = client.memory.clear_model(guild, user)
    LOGGER.info("Model cleared guild=%s user=%s had=%s by admin", guild, user, cleared)
    text = "已清除，回到預設模型。" if cleared else "這位成員沒有設定模型。"
    return {"text": text, "cleared": cleared}


# --------------------------------------------------------------------------- style


def _style_result(client: Any, args: dict, changed: bool) -> dict:
    guild, user = args["guild"], args["user"]
    head = "已更新。" if changed else "目前設定："
    return {
        "text": f"{head}\n{client._style_status(guild, user)}",
        "style": client.memory.get_style(guild, user),
        "persona_off": client.memory.get_persona_off(guild, user),
    }


async def style_get(client: Any, args: dict) -> dict:
    return _style_result(client, args, False)


async def style_set(client: Any, args: dict) -> dict:
    client.memory.set_style(args["guild"], args["user"], args["text"])
    return _style_result(client, args, True)


async def style_clear(client: Any, args: dict) -> dict:
    client.memory.clear_style(args["guild"], args["user"])
    return _style_result(client, args, True)


async def style_persona(client: Any, args: dict) -> dict:
    client.memory.set_persona_off(args["guild"], args["user"], args["mode"] == "off")
    return _style_result(client, args, True)


# --------------------------------------------------------------------------- reset, stop


async def reset(client: Any, args: dict) -> dict:
    forgot = client.threads.forget(ThreadStore.key(args["guild"], args["channel"], args["user"]))
    text = "已清除這位成員在此頻道的對話脈絡。" if forgot else "這位成員在此頻道沒有進行中的對話。"
    return {"text": text, "reset": forgot}


async def stop(client: Any, args: dict) -> dict:
    """Cancel the member's in-flight requests: in one channel, like /stop there, or in every
    channel of the guild when no --channel is given."""
    guild, user = args["guild"], args["user"]
    if "channel" in args:
        keys = [ThreadStore.key(guild, args["channel"], user)]
    else:
        keys = [k for k in client.active if k.startswith(f"{guild}:") and k.endswith(f":{user}")]
    stopped = 0
    for key in keys:
        task = client.active.get(key)
        if task is not None and not task.done():
            task.cancel()
            stopped += 1
    text = f"已取消 {stopped} 個進行中的請求。" if stopped else "這位成員沒有進行中的請求。"
    return {"text": text, "stopped": stopped}


# --------------------------------------------------------------------------- export, status


def export_dir(config: Any) -> Path:
    """BACKUP_DIR/exports when the backup volume is mounted, else CODEX_HOME/exports: either
    way a host directory the operator can reach."""
    mounted = config.backup_dir is not None and config.backup_dir.is_dir()
    return (config.backup_dir if mounted else config.codex_home) / EXPORTS


def _save_export(directory: Path, label: str, data: bytes) -> Path:
    directory.mkdir(mode=0o700, parents=True, exist_ok=True)
    stamp = time.strftime("%Y%m%d-%H%M%S")
    target, counter = directory / f"{label}-{stamp}.zip", 2
    while target.exists():
        target = directory / f"{label}-{stamp}-{counter}.zip"
        counter += 1
    write_private(target, data)  # a member's memories: for the operator's eyes only
    return target


async def export(client: Any, args: dict) -> dict:
    guild, user = args["guild"], args["user"]
    label = f"memory-{guild}-{user}"
    root = client.memory.scope_dir("user", guild, user)
    data = await asyncio.to_thread(export_memory_zip, root, label)
    if data is None:
        raise AdminError("這位成員在這個伺服器還沒有個人記憶。")
    target = await asyncio.to_thread(_save_export, export_dir(client.config), label, data)
    return {
        "text": f"已匯出（{max(1, len(data) // 1024)} KB）：{target}",
        "path": str(target),
        "bytes": len(data),
    }


async def status(client: Any, args: dict) -> dict:
    channel = args.get("channel")
    text = await client._status_text(args["guild"], channel, args["user"])
    if channel is None:
        # Continuity is kept per channel; with none given the line would say there is none.
        lines = text.split("\n")
        text = "\n".join(NO_CHANNEL_THREAD if line.startswith("續接：") else line for line in lines)
    return {"text": text}


# --------------------------------------------------------------------------- tables


HANDLERS: dict[str, Handler] = {
    "remind.add": remind_add,
    "remind.list": remind_list,
    "remind.cancel": remind_cancel,
    "memory.add": memory_add,
    "memory.forget": memory_forget,
    "memory.list": memory_list,
    "model.get": model_get,
    "model.set": model_set,
    "model.clear": model_clear,
    "style.get": style_get,
    "style.set": style_set,
    "style.clear": style_clear,
    "style.persona": style_persona,
    "reset": reset,
    "stop": stop,
    "export": export,
    "status": status,
}

GROUPS: dict[str, str] = {
    "remind": "成員的提醒（同 /remind）：add 設定、list 列出、cancel 取消",
    "memory": "Bot 的記憶（同 /remember、/forget、/memory）：add、forget、list",
    "model": "成員用的模型（同 /model）：get 查看、set 設定、clear 回到預設",
    "style": "成員的回覆風格與人設開關（同 /style）：get、set、clear、persona",
}

SCHEMAS: dict[str, dict] = {
    "remind.add": {
        "help": "以成員的身分設定提醒（同 /remind）",
        "description": (
            "以 --user 的身分設定一個提醒，和他自己用 /remind 一樣：時間要在 "
            f"{MAX_DAYS_AHEAD} 天內，每人最多 {MAX_PER_USER} 個，\n"
            "和他既有的提醒頻道、內容與要 @ 的人都相同，且到期時間相差不到 1 分鐘時不會重複設定，\n"
            "而是以「已經有一樣的提醒 #N」失敗（結束碼 1）。\n"
            "到時 Bot 在 --channel 發提醒並 @ --user；給了 --who 則改 @ 那些成員"
            "（要含 --user 請一併列出）。\n"
            "那則提醒是提醒功能本身；這個指令現在不會在頻道發任何訊息。"
        ),
        "examples": [
            f'remind add {EGC} {EGU} --when "明天 9:30" --text "交週報"',
            f"remind add {EGC} {EGU} --when 30分鐘後 --text 開會 "
            "--who 345678901234567890 456789012345678901",
        ],
        "args": {
            "guild": GUILD,
            "channel": _with(
                CHANNEL, help="到時在這個頻道提醒（要屬於 --guild，且是 Bot 開放的頻道）"
            ),
            "user": _with(USER, help="設提醒的成員（要是 --guild 的現任成員，並看得到 --channel）"),
            "when": {
                "type": "str",
                "required": True,
                "max_len": 100,
                "help": (
                    "台北時間：30分鐘後、2小時後、3天後、明天 9:30、後天下午3點、"
                    "21:00（今天，已過則明天）、9/15 14:30、2026-10-01 08:00"
                ),
            },
            "text": {
                "type": "str",
                "required": True,
                "max_len": REMINDER_TEXT_MAX,
                "stdin": True,
                "help": "提醒內容",
            },
            "who": {
                "type": "list[id]",
                "max_items": 10,
                "help": "改 @ 這些成員（每位都要是 --guild 的成員；不給＝@ --user 自己）",
            },
        },
    },
    "remind.list": {
        "help": "列出成員設的提醒（同 /remind 不帶選項）",
        "description": (
            "列出 --user 在 --guild 設的提醒：編號、到期時間、伺服器、頻道、要 @ 的人與內容。\n"
            "成員已離開伺服器也可以列，他設的提醒仍會照常發出。"
        ),
        "examples": [f"remind list {EG} {EGU}"],
        "args": {"guild": GUILD, "user": OWNER},
    },
    "remind.cancel": {
        "help": "取消成員設的一個提醒（同 /remind cancel）",
        "description": (
            "取消 --user 在 --guild 設的提醒 --id（編號用 remind list 查）。\n"
            "只取消他自己設、在這個伺服器的提醒；成員已離開伺服器也可以取消。"
        ),
        "examples": [f"remind cancel {EG} {EGU} --id 12"],
        "args": {
            "guild": GUILD,
            "user": _with(OWNER, help="設提醒的成員；只能取消他自己設的（可以已經離開伺服器）"),
            "id": {"type": "id", "required": True, "help": "提醒編號（remind list 看得到）"},
        },
    },
    "memory.add": {
        "help": "記住一件事（同 /remember）",
        "description": (
            "存一則記憶，和 /remember 存在同一個地方，Bot 之後回答時會參考：\n"
            "--scope user 是 --user 的個人記憶（只在他問時用到）；"
            "--scope guild 是整個伺服器共用的記憶，不需要 --user。"
        ),
        "examples": [
            f"memory add --scope user {EG} {EGU} --name 生日 --text 3月14日",
            f"memory add --scope guild {EG} --name 群規 --text -",
        ],
        "args": {
            "scope": _with(SCOPE, required=True),
            "guild": GUILD,
            "user": _with(USER, required=False, help="個人記憶的成員（--scope user 時必填）"),
            "name": {"type": "str", "required": True, "max_len": NAME_MAX, "help": "短標題"},
            "text": {
                "type": "str",
                "required": True,
                "max_len": SLASH_TEXT_MAX,
                "stdin": True,
                "help": "要記住的內容",
            },
        },
    },
    "memory.forget": {
        "help": "刪除一則記憶（同 /forget）",
        "description": (
            "用完整名稱或 memory list 括號裡的檔名（.md）指定要刪的記憶。\n"
            "有好幾條同名時什麼都不刪，請改用檔名。"
        ),
        "examples": [f"memory forget --scope user {EG} {EGU} --name 生日"],
        "args": {
            "scope": _with(SCOPE, required=True),
            "guild": GUILD,
            "user": _with(USER, required=False, help="個人記憶的成員（--scope user 時必填）"),
            "name": {
                "type": "str",
                "required": True,
                "max_len": SLASH_TEXT_MAX,
                "help": "記憶名稱，或 memory list 括號裡的檔名",
            },
        },
    },
    "memory.list": {
        "help": "列出記憶（同 /memory）",
        "description": (
            "不給 --user 只列伺服器記憶；給 --user 則列他的個人記憶與伺服器記憶，"
            "--scope 可只列其中一種。\n--json 另有每則記憶的名稱、檔名與內容。"
        ),
        "examples": [f"memory list {EG} {EGU}", f"memory list {EG}"],
        "args": {
            "scope": _with(SCOPE, help="只列個人或伺服器記憶（不給＝兩者）"),
            "guild": GUILD,
            "user": _with(
                USER, required=False, help="也列這位成員的個人記憶（要是 --guild 的現任成員）"
            ),
        },
    },
    "model.get": {
        "help": "查看成員用的模型（同 /model 查看）",
        "description": (
            "顯示 --user 在 --guild 用的模型與強度；沒設定過時顯示預設模型（DEFAULT_MODEL）。"
        ),
        "examples": [f"model get {EG} {EGU}"],
        "args": {"guild": GUILD, "user": USER},
    },
    "model.set": {
        "help": "設定成員用的模型（同 /model）",
        "description": (
            "和 /model 的選單走同一個檢查，只能選它提供的模型：\n"
            "  --value     模型，例如 agy:gemini-3.8-flash、openrouter:<id>，"
            "或配合 --provider 只寫 id\n"
            "  --provider  只給它＝改用這個來源的預設模型\n"
            "  --effort    預設推理強度，low／medium／high／xhigh／max 之一"
            "（和 /model 一樣，不對照模型檢查）\n"
            "至少要給一個。--value 的模型不合時會被拒絕，並列出可選的模型；"
            "只給 --provider 而它沒有預設模型時，\n會被拒絕並請你用 --value 指定。"
        ),
        "examples": [
            f"model set {EG} {EGU} --value agy:gemini-3.8-flash",
            f"model set {EG} {EGU} --effort high",
        ],
        "args": {
            "guild": GUILD,
            "user": USER,
            "provider": {"type": "choice", "choices": list(PROVIDERS), "help": "模型來源"},
            "value": {"type": "str", "max_len": 200, "help": "模型"},
            "effort": {
                "type": "choice",
                "choices": list(REASONING_EFFORTS),
                "help": "預設推理強度（和 /model 一樣，不對照模型檢查）",
            },
        },
    },
    "model.clear": {
        "help": "清除成員的模型設定，回到預設（同 /model 清除）",
        "examples": [f"model clear {EG} {EGU}"],
        "args": {"guild": GUILD, "user": USER},
    },
    "style.get": {
        "help": "查看成員的個人回覆風格與人設開關（同 /style 查看）",
        "examples": [f"style get {EG} {EGU}"],
        "args": {"guild": GUILD, "user": USER},
    },
    "style.set": {
        "help": "設定成員的個人回覆風格（同 /style text）",
        "description": (
            "設定 --user 在 --guild 的個人回覆風格；它覆蓋預設輸出風格，只影響他的回答。"
        ),
        "examples": [f'style set {EG} {EGU} --text "回答盡量簡短，用條列"'],
        "args": {
            "guild": GUILD,
            "user": USER,
            "text": {
                "type": "str",
                "required": True,
                "max_len": STYLE_MAX_CHARS,
                "stdin": True,
                "help": "回覆風格",
            },
        },
    },
    "style.clear": {
        "help": "清除成員的個人回覆風格，回到預設輸出風格（同 /style 清除）",
        "examples": [f"style clear {EG} {EGU}"],
        "args": {"guild": GUILD, "user": USER},
    },
    "style.persona": {
        "help": "保留或關閉成員的伺服器人設（同 /style persona）",
        "description": "off＝他的回答不套用伺服器人設；keep＝照常套用。只影響 --user 自己。",
        "examples": [f"style persona {EG} {EGU} --mode off"],
        "args": {
            "guild": GUILD,
            "user": USER,
            "mode": {
                "type": "choice",
                "required": True,
                "choices": ["keep", "off"],
                "help": "keep＝保留；off＝關閉",
            },
        },
    },
    "reset": {
        "help": "忘掉成員在一個頻道的對話脈絡（同 /reset）",
        "description": "--user 在 --channel 的下一題從頭開始；其他頻道的對話與他的記憶都不受影響。",
        "examples": [f"reset {EGC} {EGU}"],
        "args": {"guild": GUILD, "channel": CHANNEL, "user": USER_IN_CHANNEL},
    },
    "stop": {
        "help": "取消成員進行中的請求（同 /stop）",
        "description": (
            "給 --channel 只取消那個頻道的（同 /stop）；不給則取消他在 --guild 所有頻道的。"
        ),
        "examples": [f"stop {EG} {EGU}"],
        "args": {
            "guild": GUILD,
            "channel": _with(CHANNEL, required=False),
            "user": USER_IN_CHANNEL,
        },
    },
    "export": {
        "help": "把成員的個人記憶打包成 zip（同 /export），印出檔案路徑",
        "description": (
            "有設定 BACKUP_DIR 且該目錄存在時，zip 寫到 BACKUP_DIR/exports"
            "（.env.example 設為 /backups，\n"
            "compose 把 host 的 ./backups 掛在那裡）；否則寫到 volume 裡的 CODEX_HOME/exports。\n"
            "權限 0600，不會傳給成員。印出的是 container 內的路徑：在 BACKUP_DIR 下的從 host 的\n"
            "bind mount 取，在 CODEX_HOME 下的用 docker cp 拿出來。"
        ),
        "examples": [f"export {EG} {EGU}"],
        "timeout": 120,
        "args": {"guild": GUILD, "user": USER},
    },
    "status": {
        "help": "成員的 /status：設定、續接、記憶與系統狀態",
        "description": (
            "看 --user 會在 /status 看到的內容。續接是每個頻道各自的，要看就加 --channel。"
        ),
        "examples": [f"status {EGC} {EGU}"],
        "timeout": 120,
        "args": {
            "guild": GUILD,
            "channel": _with(CHANNEL, required=False),
            "user": USER_IN_CHANNEL,
        },
    },
}
