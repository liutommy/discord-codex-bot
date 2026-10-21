"""Operator commands for servers and the Bot itself: linkclean, embedfix, persona, summary,
ask, help, bot.status and alerts.list. The schema format is in admin_server.py.

Nothing here posts to a channel: ask, summary and help hand their text back to the CLI. ask and
summary run as the member named by --user, through the same answer path as /codex and /summary:
the member's model, style and memory apply, and the request is the member's in-flight one (their
/stop cancels it). Action tags in ask's answer take effect as if the member had asked in
--channel, so a reminder or watch it sets fires later in the channel like any other. summary,
like /summary, answers with no channel: its memory tags, reminder cancels and changes to existing
watches take effect, but a new reminder or watch is not set.
"""

from __future__ import annotations

import asyncio
import math
import time
from typing import Any

import discord

from . import grok, instructions, quota, routing
from .admin_server import AdminError, Handler, Rejected, resolve_guild_channel
from .announce import show_time
from .attachments import remove_dir
from .backends import BACKEND_LABELS, GROK
from .codex import codex_login_status
from .config import REASONING_EFFORTS
from .linkclean import MODES
from .summary import (
    DEFAULT_MESSAGES,
    MAX_MESSAGES,
    recent_messages,
    render_transcript,
    summary_prompt,
    window_label,
)
from .threads import ThreadStore
from .usage import probe_rate_limits

# Logged as `by=` where the slash commands log the member's id.
OPERATOR = "admin"
# Discord's cap on a slash command's text option; the Bot's own limits (MAX_PROMPT_CHARS) are
# checked on top, by the same code the slash command uses.
OPTION_MAX_CHARS = 6000
KINDS = {"persona": instructions.PERSONA, "style": instructions.OUTPUT_STYLE}

GUILD = {"type": "id", "required": True, "help": "伺服器 id（要在 ALLOWED_GUILD_IDS 內）"}
CHANNEL = {
    "type": "id",
    "required": True,
    "help": "頻道或討論串 id（要屬於 --guild，且是 Bot 開放的頻道）",
}
MEMBER = {
    "type": "id",
    "required": True,
    "help": (
        "以這位成員的身分：用他的模型、風格與記憶（要是 --guild 的現任成員，並看得到 --channel）"
    ),
}
KIND = {"type": "choice", "choices": list(KINDS), "help": "persona＝人設；style＝預設輸出風格"}
# Placeholder ids for the examples: guild, channel, member.
EG = "--guild 123456789012345678"
EGCU = f"{EG} --channel 234567890123456789 --user 345678901234567890"

# Background tasks the Bot starts (setup_hook, on_ready): (attribute, label, runs forever).
# A loop meant to run forever that has ended is the thing to look at; a one-off may finish.
LOOPS = (
    ("_sweeper", "附件清理", True),
    ("_backup_loop", "備份", True),
    ("_reminder_loop", "提醒", True),
    ("_consolidator", "記憶整併", True),
    ("_digester", "每週摘要", True),
    ("_harvester", "對話收成", True),
    ("_tracking_loop", "社群追蹤", True),
    ("_agy_usage_loop", "agy 額度讀取", True),
    ("_emoji_describer", "表情描述", True),
    ("_login_watch", "登入檢查", True),
    ("_announce_task", "公告佇列", True),
    ("_emoji_backfill_task", "表情回填", False),
    ("_grok_models_task", "Grok 模型清單", False),
)


def _bot_module():
    # Imported on use: bot.py imports the admin server, and only the Bot runs handlers.
    from . import bot

    return bot


def _ago(seconds: float) -> str:
    seconds = max(0.0, seconds)
    if seconds < 3600:
        return f"{int(seconds // 60)} 分鐘前"
    if seconds < 86400:
        return f"{int(seconds // 3600)} 小時前"
    return f"{int(seconds // 86400)} 天前"


# --------------------------------------------------------------------------- linkclean, embedfix


async def linkclean_get(client: Any, args: dict) -> dict:
    mode = client.linkclean.mode(args["guild"])
    return {"text": f"連結洗參數：{MODES.get(mode, mode)}", "mode": mode}


async def linkclean_set(client: Any, args: dict) -> dict:
    client.linkclean.set(args["guild"], args["mode"])
    return {"text": f"連結洗參數已設為「{MODES[args['mode']]}」。", "mode": args["mode"]}


def _embedfix_state(client: Any, guild_id: int) -> dict:
    on = client.linkclean.embedfix(guild_id)
    # It rides on linkclean's delivery, as /embedfix says.
    note = "" if client.linkclean.mode(guild_id) != "off" else "（連結洗參數為全關時不會投遞）"
    return {"text": f"預覽修正：{'開' if on else '關'}{note}", "on": on}


async def embedfix_get(client: Any, args: dict) -> dict:
    return _embedfix_state(client, args["guild"])


async def embedfix_set(client: Any, args: dict) -> dict:
    client.linkclean.set_embedfix(args["guild"], args["state"] == "on")
    return _embedfix_state(client, args["guild"])


# --------------------------------------------------------------------------- persona


def _instruction_files(config: Any) -> dict:
    return {
        name: {
            "uploaded": instructions.is_uploaded(config, kind),
            "chars": len(instructions.current_text(config, kind)),
        }
        for name, kind in KINDS.items()
    }


async def persona_get(client: Any, args: dict) -> dict:
    config = client.config
    if "kind" not in args:
        return {"text": client._instructions_status(), **_instruction_files(config)}
    kind = KINDS[args["kind"]]
    text = instructions.current_text(config, kind)
    return {
        "text": text or "（空的）",
        "kind": args["kind"],
        "uploaded": instructions.is_uploaded(config, kind),
    }


async def persona_set(client: Any, args: dict) -> dict:
    kind = KINDS[args["kind"]]
    try:
        # The upload modal's own check, as if the text came in as a .md file.
        text = instructions.parse_upload("upload.md", args["text"].encode("utf-8"))
    except ValueError as error:
        raise Rejected(f"{instructions.LABELS[kind]}：{error}") from None
    # Backs up what it replaces, rebuilds the workspaces and retires every live thread.
    report = await client._save_instructions({kind: text}, OPERATOR)
    return {"text": report, **_instruction_files(client.config)}


async def persona_clear(client: Any, args: dict) -> dict:
    # /persona's reset: threads retired only when something was actually reset.
    removed, text = await client._reset_instructions(KINDS[args["kind"]], OPERATOR)
    return {"text": text, "removed": removed, **_instruction_files(client.config)}


# --------------------------------------------------------------------------- ask, summary, help


async def _quiet(text: str, view: Any) -> None:
    """The placeholder a slash command paints while it works; the CLI has none."""


async def _as_member(client: Any, key: str, user_id: int, start) -> Any:
    """One answer run as the member's in-flight request (client._run_tracked), so their /stop
    or ❌ cancels it like their own; AdminError when they did."""
    result = await client._run_tracked(key, user_id, _quiet, start)
    if result is None:
        task = asyncio.current_task()
        if task is not None and task.cancelling():
            # _run_tracked swallows the cancellation. The server's timeout or shutdown must
            # still end this as cancelled, or it would read as the member's /stop.
            raise asyncio.CancelledError
        raise AdminError("已取消：成員用 /stop 或 ❌ 取消了這個請求")
    return result


def _answered_by(client: Any, result: Any, routed: Any) -> str:
    if result.via is None:
        return ""
    label = client._via_label(result.via)
    return f"自動選擇：{label}" if routed is not None else label


async def summary(client: Any, args: dict) -> dict:
    guild_id, channel_id, user_id = args["guild"], args["channel"], args["user"]
    hours = args.get("hours")
    if hours is not None and "count" in args:
        raise Rejected("--count 與 --hours 只能擇一")
    channel = await resolve_guild_channel(client, guild_id, channel_id)
    try:
        fetched, cut = await recent_messages(channel, args.get("count", DEFAULT_MESSAGES), hours)
    except discord.HTTPException:
        raise AdminError("讀不到這個頻道的訊息紀錄。") from None
    transcript = render_transcript(fetched)
    if not transcript:
        return {"text": "這段時間裡沒有可摘要的訊息。", "summary": "", "messages": 0}
    prompt = summary_prompt(
        getattr(channel, "name", "此頻道"), transcript, len(fetched), args.get("focus", "")
    )
    key = ThreadStore.key(guild_id, channel_id, user_id)
    # A fresh, unremembered turn, as /summary: it must not become the member's conversation.
    result = await _as_member(
        client,
        key,
        user_id,
        lambda on_video_slow, on_delta: client._answer(
            prompt, [], guild_id, user_id, resume="", on_delta=on_delta
        ),
    )
    remove_dir(result.generated_dir)  # nothing is delivered from here
    if result.via is None:  # _answer's own failure text (queue full, backend failed)
        raise AdminError(result.text)
    return {
        "text": f"頻道摘要{window_label(len(fetched), hours, cut)}\n{result.text}",
        "summary": result.text,
        "messages": len(fetched),
        "model": _answered_by(client, result, None),
    }


async def ask(client: Any, args: dict) -> dict:
    guild_id, channel_id, user_id = args["guild"], args["channel"], args["user"]
    prompt = args["prompt"].strip()
    if reason := client._validate(prompt, []):
        raise Rejected(reason)
    effort = args.get("effort", "")
    new = bool(args.get("new"))
    # The steps of /codex, without the Discord message around them.
    effort_value = effort or client._member_effort(guild_id, user_id)
    key = ThreadStore.key(guild_id, channel_id, user_id)
    plain = client.memory.get_persona_off(guild_id, user_id)
    model = client._model(guild_id, user_id)
    resume = "" if new else client.threads.current(key, plain=plain, model=model)
    routed = await client._route(
        key, None, guild_id, user_id, prompt, [], plain, fresh=new, effort=effort
    )
    if routed is not None:
        resume = routed.resume
    result = await _as_member(
        client,
        key,
        user_id,
        lambda on_video_slow, on_delta: client._answer(
            prompt,
            [],
            guild_id,
            user_id,
            effort_value,
            resume,
            on_video_slow=on_video_slow,
            on_delta=on_delta,
            channel_id=channel_id,
            fresh=new,
            **client._routed_kw(routed),
        ),
    )
    remove_dir(result.generated_dir)  # files are not delivered to the CLI; listed below
    if routed is not None:
        client._log_route(routed, result)
    # No message id: nothing was posted, so the conversation continues by its key alone.
    client._remember(
        key, result.thread_id, None, plain, *client._routed_memo(routed, result, model)
    )
    if result.via is None:  # _answer's own failure text (queue full, backend failed)
        raise AdminError(result.text)
    answer = result.text
    if routed is not None and routed.no_x and result.via.backend != GROK:
        answer += f"\n\n{_bot_module().NO_X_NOTE}"
    answered_by = _answered_by(client, result, routed)
    notes = [f"回答模型：{answered_by}"]
    if result.resumed:
        notes.append("接續先前的對話")
    if result.images:
        notes.append(f"另有 {len(result.images)} 個檔案沒有附上（CLI 不傳檔案）")
    return {
        "text": f"{answer}\n\n（{'；'.join(notes)}）",
        "answer": answer,
        "model": answered_by,
        "auto": routed is not None,
        "resumed": result.resumed,
        "files": [path.name for path in result.images],
    }


async def help_text(client: Any, args: dict) -> dict:
    return {"text": client.help_sheet() if args.get("sheet") else client.help_guide()}


# --------------------------------------------------------------------------- bot.status, alerts


def _loop_state(task: asyncio.Task | None, forever: bool) -> tuple[str, bool]:
    """(how a background task is doing, whether that needs a look)."""
    if task is None:
        return "沒有啟動", False  # its feature is off, or the Bot is not ready yet
    if not task.done():
        return "執行中", False
    if task.cancelled():
        return "已取消", forever
    if (error := task.exception()) is not None:
        return f"已停止（{type(error).__name__}）", True
    return ("已結束", True) if forever else ("已完成", False)


def _agy_lines(client: Any, now: float) -> tuple[list[str], dict]:
    agy = client.agy_quota
    left = agy.summary()
    spent = agy._spent_until  # set by agy's own refusals; no reading shows them
    buckets = " · ".join(f"{bucket} {value:.0f}%" for bucket, value in sorted(left.items()))
    groups = sorted(
        quota.RESERVED | {"gemini"} | {bucket.rsplit("-", 1)[0] for bucket in left} | set(spent)
    )
    pools = {}
    for group in groups:
        if spent.get(group, 0.0) > now:
            pools[group] = f"用完，到 {show_time(spent[group])}"
        elif agy.usable(group):
            pools[group] = "可用"
        else:
            pools[group] = "低於保留量"
    lines = [
        f"agy 額度（剩餘）：{buckets or '還沒讀到'}",
        "agy 額度池：" + " · ".join(f"{group} {state}" for group, state in pools.items()),
    ]
    return lines, {"buckets": left, "pools": pools}


async def _grok_line(client: Any, now: float) -> tuple[str, dict]:
    if not grok.enabled(client.config):
        return "Grok：未設定", {"enabled": False}
    reading = await grok.usage(client.config)
    percent = (reading or {}).get("weekly_percent")
    known = isinstance(percent, int | float)
    chat_open = client._grok_chat_open(reading)
    refusals = [at for at in client._grok_refusals if now - at < 600]
    line = "Grok：" + (f"額度 7d {percent:.0f}%" if known else "額度讀不到")
    line += " · 聊天可用" if chat_open else " · 聊天已達上限（剩下的留給 X 查詢）"
    breaker = now < client._grok_off_until
    if breaker:
        line += f" · 安全檢查斷路中，到 {show_time(client._grok_off_until)}"
    if refusals:
        line += f" · 10 分鐘內被拒 {len(refusals)} 次"
    return line, {
        "enabled": True,
        "weekly_percent": percent if known else None,
        "chat_open": chat_open,
        "off_until": client._grok_off_until if breaker else None,
        "recent_refusals": len(refusals),
    }


def _routing_lines(client: Any) -> list[str]:
    table = client.routing
    if table is None:
        return ["自動挑選：關"]
    lines = [
        f"自動挑選：開（Codex 額度 5h ≥ {table.codex_five_hour:g}% 或 7d ≥ "
        f"{table.codex_seven_day:g}% 時降一級強度）"
    ]
    for kind, cells in table.types.items():
        bands = " · ".join(
            f"{band} {cell}" for band, cell in zip(routing.BANDS, cells, strict=False)
        )
        lines.append(f"　{kind}：{bands}")
    lines.append("　圖片：" + " → ".join(table.image))
    lines.append(f"　Grok 用完：{table.grok_spent} · X 問題沒有 Grok：{table.live_x_without_grok}")
    return lines


async def bot_status(client: Any, args: dict) -> dict:
    config = client.config
    now = time.time()
    data: dict[str, Any] = {}
    latency = client.latency
    head = f"Bot：{'已連線' if client.is_ready() else '尚未就緒'}"
    if math.isfinite(latency):
        head += f" · 延遲 {latency * 1000:.0f} ms"
    head += f" · 啟動於 {show_time(client._started_at)}"
    active = sum(1 for task in client.active.values() if not task.done())
    lines = [
        head,
        f"進行中：成員請求 {active} · Codex 佇列 {client.queue.size}"
        f" · 維運指令 {len(client.admin.running)}（含這一個）",
    ]
    data["ready"] = client.is_ready()
    data["active"] = active

    login = await codex_login_status(config)
    limits = await probe_rate_limits(config)
    codex = f"Codex：{login}"
    if limits is not None:
        codex += (
            f" · 額度 5h {limits.primary_used_percent:.0f}%"
            f" / 7d {limits.secondary_used_percent:.0f}%"
        )
    else:
        codex += " · 額度讀不到"
    lines.append(codex)
    data["codex"] = {
        "login": login,
        "five_hour": limits.primary_used_percent if limits else None,
        "seven_day": limits.secondary_used_percent if limits else None,
    }
    grok_line, data["grok"] = await _grok_line(client, now)
    lines.append(grok_line)
    agy_lines, data["agy"] = _agy_lines(client, now)
    lines += agy_lines

    fallback = client._last_fallback
    if fallback is None:
        lines.append("最近一次備援：這次啟動後沒有")
        data["fallback"] = None
    else:
        when, why, failed, spare = fallback
        label = _bot_module()._FALLBACK_LABEL.get(type(why)) or getattr(why, "label", "服務異常")
        lines.append(
            f"最近一次備援：{show_time(when)}（{_ago(now - when)}）"
            f"{BACKEND_LABELS.get(failed, failed)} {label}，"
            f"改用 {BACKEND_LABELS.get(spare.backend, spare.backend)}（{spare.model}）回答"
        )
        data["fallback"] = {
            "at": when,
            "why": type(why).__name__,
            "failed": failed,
            "spare": f"{spare.backend}:{spare.model}",
        }
    lines += _routing_lines(client)
    data["routing"] = client.routing is not None

    loops: dict[str, str] = {}
    lines.append("背景工作：")
    tasks = [(attr, label, forever, getattr(client, attr, None)) for attr, label, forever in LOOPS]
    tasks.append(("admin", "維運收件匣", True, client.admin._task))
    for attr, label, forever, task in tasks:
        state, bad = _loop_state(task, forever)
        loops[attr] = state
        lines.append(f"　{'⚠️ ' if bad else ''}{label}：{state}")
    data["loops"] = loops
    return {"text": "\n".join(lines), **data}


async def alerts_list(client: Any, args: dict) -> dict:
    alerts = client.alerts
    now = time.monotonic()
    cooldown = client.config.alert_cooldown_minutes * 60
    streaks = {backend: count for backend, count in sorted(alerts._streak.items()) if count}
    outstanding = sorted(backend for backend, open_ in alerts._alerted.items() if open_)
    sent = sorted(alerts._last_sent.items(), key=lambda item: item[1], reverse=True)
    owner = str(alerts.owner_id) if alerts.owner_id else "還沒找到（Bot 就緒時會找應用程式擁有者）"
    lines = [
        f"告警私訊給：{owner}",
        "連續失敗：" + (" · ".join(f"{b} {n} 次" for b, n in streaks.items()) or "無"),
        "等恢復通知：" + ("、".join(outstanding) or "無"),
        f"這次啟動後送出的告警（同類 {cooldown / 60:g} 分鐘內不重送）："
        if sent
        else "這次啟動後還沒送出告警。",
    ]
    recent = []
    for kind, at in sent:
        waiting = cooldown - (now - at)
        lines.append(
            f"　{kind}：{_ago(now - at)}"
            + (f"（冷卻中，還有 {math.ceil(waiting / 60)} 分鐘）" if waiting > 0 else "")
        )
        recent.append({"kind": kind, "seconds_ago": round(now - at)})
    return {
        "text": "\n".join(lines),
        "owner": alerts.owner_id or None,
        "streaks": streaks,
        "outstanding": outstanding,
        "sent": recent,
    }


HANDLERS: dict[str, Handler] = {
    "linkclean.get": linkclean_get,
    "linkclean.set": linkclean_set,
    "embedfix.get": embedfix_get,
    "embedfix.set": embedfix_set,
    "persona.get": persona_get,
    "persona.set": persona_set,
    "persona.clear": persona_clear,
    "summary": summary,
    "ask": ask,
    "help": help_text,
    "bot.status": bot_status,
    "alerts.list": alerts_list,
}

PERSONA_NOTE = (
    "人設與預設輸出風格是所有伺服器共用的（和 /persona 相同，那裡只有伺服器管理層能用）。\n"
    "有設定 BACKUP_DIR 時，改動會先備份被取代的版本；接著重建 Codex 的工作目錄，\n"
    "並讓所有進行中的對話串重新開始。persona clear 只有真的還原了東西才會這樣做"
    "（本來就是 image 預設時什麼都不變）。"
)
AS_MEMBER = (
    "以 --user 的身分執行：用他的模型、風格與記憶，算是他進行中的請求，"
    "他的 /stop 或 ❌ 可以取消它。\n"
)
ASK_TAGS = (
    "注意：回答裡的動作標籤照樣生效，就像成員自己問的一樣——會寫進他的記憶，\n"
    "設下的提醒與追蹤之後會在頻道裡通知他。"
)
SUMMARY_TAGS = (
    "注意：和 /summary 一樣，摘要不帶頻道執行：回答裡的記憶標籤、取消提醒，以及修改或取消\n"
    "既有追蹤的標籤會生效；新的提醒與新的追蹤不會設下。"
)
GROUPS: dict[str, str] = {
    "linkclean": "伺服器的連結洗參數模式（同 /linkclean）：get 查詢、set 設定",
    "embedfix": "伺服器的預覽修正開關（同 /embedfix）：get 查詢、set 開關",
    "persona": "所有伺服器共用的人設與預設輸出風格（同 /persona）：get、set、clear",
    "bot": "Bot 本身：status 系統狀態",
    "alerts": "告警：list 告警狀態",
}

SCHEMAS: dict[str, dict] = {
    "linkclean.get": {
        "help": "查詢伺服器的連結洗參數模式（同 /linkclean 查詢）",
        "examples": [f"linkclean get {EG}"],
        "args": {"guild": GUILD},
    },
    "linkclean.set": {
        "help": "設定伺服器的連結洗參數模式（同 /linkclean）",
        "description": (
            "設定 --guild 的連結洗參數模式：之後成員在這個伺服器貼的連結照新模式處理。\n"
            "這個指令本身不在頻道發訊息。"
        ),
        "examples": [f"linkclean set {EG} --mode links"],
        "args": {
            "guild": GUILD,
            "mode": {
                "type": "choice",
                "choices": list(MODES),
                "required": True,
                "help": "all＝全部清洗；links＝只清洗純連結；off＝全關",
            },
        },
    },
    "embedfix.get": {
        "help": "查詢伺服器的預覽修正開關（同 /embedfix 查詢）",
        "examples": [f"embedfix get {EG}"],
        "args": {"guild": GUILD},
    },
    "embedfix.set": {
        "help": "開關伺服器的預覽修正（同 /embedfix）",
        "description": (
            "開關 --guild 的預覽修正：把社群貼文連結換成 Discord 能預覽的版本。\n"
            "它隨連結洗參數一起投遞，連結洗參數為 off 時不會有作用。"
        ),
        "examples": [f"embedfix set {EG} --state on"],
        "args": {
            "guild": GUILD,
            "state": {
                "type": "choice",
                "choices": ["on", "off"],
                "required": True,
                "help": "on＝開；off＝關",
            },
        },
    },
    "persona.get": {
        "help": "查詢人設與預設輸出風格；給 --kind 則印出現行全文",
        "description": (
            "不給 --kind：兩者各是上傳的還是 image 預設、各有幾個字。\n"
            "給 --kind：印出那一份的現行全文（可以導到檔案，改完再用 persona set 上傳）。"
        ),
        "examples": ["persona get", "persona get --kind persona > persona.md"],
        "args": {"kind": KIND},
    },
    "persona.set": {
        "help": "換上新的人設或預設輸出風格（同 /persona 上傳）",
        "description": PERSONA_NOTE
        + "\n內容的檢查和 /persona 上傳 .md 檔相同。從檔案讀時 docker exec 要加 -i。",
        "examples": [
            "persona set --kind persona --text - < persona.md",
            "persona set --kind style --text - < style.md",
        ],
        "args": {
            "kind": {**KIND, "required": True},
            "text": {
                "type": "str",
                "required": True,
                # Only a bound on the request: the limit itself is the upload modal's, checked
                # by persona_set on the stripped text, with the modal's message.
                "max_len": instructions.MAX_UPLOAD_BYTES,
                "stdin": True,
                "help": f"全文（最多 {instructions.MAX_CHARS} 字）",
            },
        },
    },
    "persona.clear": {
        "help": "把人設或預設輸出風格還原成 image 預設（同 /persona 還原）",
        "description": PERSONA_NOTE,
        "examples": ["persona clear --kind style"],
        "args": {"kind": {**KIND, "required": True}},
    },
    "summary": {
        "help": "摘要頻道最近的訊息，結果只回到這裡（同 /summary）",
        "description": (
            "和 /summary 相同的摘要，但不貼到頻道，只印在這裡。預設最近 "
            f"{DEFAULT_MESSAGES} 則；--count 與 --hours 擇一。\n"
            "摘要是一次獨立的回答，不會變成成員在這個頻道的對話脈絡。\n" + AS_MEMBER + SUMMARY_TAGS
        ),
        "examples": [
            f"summary {EGCU}",
            f'summary {EGCU} --hours 24 --focus "有哪些待辦"',
        ],
        "timeout": 600,
        "args": {
            "guild": GUILD,
            "channel": CHANNEL,
            "user": MEMBER,
            "count": {"type": "int", "min": 5, "max": MAX_MESSAGES, "help": "最近幾則訊息"},
            "hours": {"type": "int", "min": 1, "max": 168, "help": "改成最近幾小時內的訊息"},
            "focus": {"type": "str", "max_len": OPTION_MAX_CHARS, "help": "特別想知道什麼"},
        },
    },
    "ask": {
        "help": "以成員的身分問 Bot，答案只回到這裡（同 /codex）",
        "description": (
            "和成員自己用 /codex 問一樣，接續他在 --channel 的對話（--new 則重來），"
            "之後他在頻道裡也會接著這段對話。\n"
            "答案不會貼到頻道，只印在這裡；回答產生的檔案不會附上：文字輸出只說有幾個，\n"
            "檔名在 --json 的 result.files 裡。\n" + AS_MEMBER + ASK_TAGS
        ),
        "examples": [
            f'ask {EGCU} --prompt "幫我整理今天討論的結論"',
            f"ask {EGCU} --new --effort high --prompt - < question.txt",
        ],
        "timeout": 600,
        "args": {
            "guild": GUILD,
            "channel": CHANNEL,
            "user": MEMBER,
            "prompt": {
                "type": "str",
                "required": True,
                "max_len": OPTION_MAX_CHARS,
                "stdin": True,
                "help": "問題",
            },
            "effort": {
                "type": "choice",
                "choices": list(REASONING_EFFORTS),
                "help": "這一題的推理強度（預設用成員自己的設定）",
            },
            "new": {"type": "bool", "help": "忽略之前的對話，從頭開始"},
        },
    },
    "help": {
        "help": "成員看到的指令說明（同 /help）",
        "description": "印出 /help 的內容；--sheet 改印注入給模型的精簡版（<HELP> 區塊）。",
        "examples": ["help", "help --sheet"],
        "args": {"sheet": {"type": "bool", "help": "改看注入給模型的精簡版"}},
    },
    "bot.status": {
        "help": "Bot 的系統狀態：額度、備援、自動挑選與背景工作",
        "description": (
            "只有執行中的 Bot 知道的狀態：連線與延遲、進行中的請求、"
            "Codex／Grok／agy 額度與暫停標記、\n"
            "最近一次備援、自動挑選的路由表，以及每個背景工作是否還活著（⚠️ 表示該看一下）。"
        ),
        "examples": ["bot status", "bot status --json"],
    },
    "alerts.list": {
        "help": "告警狀態：連續失敗、等恢復通知的告警、最近送出的告警",
        "description": (
            "告警私訊給誰、各後端連續失敗幾次、哪些在等恢復通知，以及這次啟動後送出過的告警。"
        ),
        "examples": ["alerts list"],
    },
}
