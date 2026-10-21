from __future__ import annotations

import asyncio
import contextlib
import hashlib
import io
import json
import logging
import logging.handlers
import re
import time
from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass, replace
from datetime import UTC, timedelta
from datetime import datetime as _dt
from pathlib import Path
from typing import NamedTuple
from urllib.parse import urlsplit

import aiohttp
import discord
from discord import app_commands

from . import agy as agy_module
from . import (
    apis,
    characters,
    embedfix,
    gemini,
    grok,
    health,
    imagegen,
    instructions,
    quota,
    routing,
    sandbox,
    search,
    tags,
    xsearch,
)
from .access import check_access
from .admin import admin_root
from .admin_server import AdminServer
from .agy import AgyUnavailable, read_usage, run_agy
from .alerts import Alerter, login_watch
from .announce import announce_loop
from .attachments import (
    download_attachment,
    extract_text,
    remove_dir,
    remove_request_dir,
    request_dir,
    sweep_forever,
    validate_attachment,
)
from .backends import (
    AGY,
    AGY_FAMILIES,
    BACKEND_LABELS,
    CODEX,
    GROK,
    LOCAL,
    OPENROUTER,
    ORCAROUTER,
    ROUTER_BACKENDS,
    ROUTER_LABELS,
    BackendUnavailable,
    ModelChoice,
    Resolved,
    choices,
    fallback_chain,
    grok_choice,
    parse_choice,
    resolve,
    router_choice,
    run_batch,
    split_stored,
)
from .backup import backup_forever, export_memory_zip
from .clock import sleep_for
from .codex import (
    QUOTE_FENCE,
    BackendError,
    CodexResult,
    CodexServerOverloaded,
    CodexUnauthorized,
    CodexUsageLimit,
    codex_login_status,
    defang,
    run_codex,
    split_quoted,
)
from .config import REASONING_EFFORTS, Config, load_config
from .consolidate import consolidate_forever, seconds_until
from .digest import digest_forever
from .emoji import CUSTOM as CUSTOM_EMOJI
from .emoji import EmojiStore, backfill_channel, describe_pending
from .harvest import _quota_ok, harvest_forever, transcript_turns
from .help import render_guide, render_sheet
from .imagegen import (
    IMAGE_TAG,
    ImageGenError,
    ImageRequest,
    ImageService,
    ImageSettings,
    extract_image_tags,
    request_from_tag,
)
from .linkclean import MAX_URLS, MODES, SwitchStore, deliver, is_link_only, plan, spoilered
from .links import (
    FETCH_TAG,
    Preview,
    _guarded_session,
    extract_fetch_tags,
    fetch_or_render,
    find_urls,
    has_video,
    link_blocks,
    strip_tracking,
    understand_video,
    unreadable,
)
from .memory import (
    MEMORY_TAG,
    RECALL_TAG,
    SCOPES,
    SEARCH_TAG,
    STYLE_MAX_CHARS,
    MemoryLimits,
    MemoryStore,
    PermanentMemory,
    extract_memory_tags,
    extract_read_requests,
)
from .openrouter import ROUTERS, Catalog, QueueBusy, run_router, trim_history
from .output import DISCORD_MESSAGE_LIMIT, format_reply, split_discord_message, tail, truncate
from .queue import QueueFullError, SerialQueue
from .reminders import (
    CANCEL_TAG,
    PAST,
    REMIND_TAG,
    TOO_FAR,
    ReminderStore,
    describe,
    extract_reminder_tags,
    parse_when,
    reminder_loop,
    render_pending,
)
from .reminders import (
    targets as reminder_targets,
)
from .routing import CONTEXT_TURNS, MAX_EARLIER, Route, Table, Verdict
from .summary import (
    DEFAULT_MESSAGES,
    MAX_MESSAGES,
    recent_messages,
    render_transcript,
    summary_prompt,
    window_label,
)
from .threads import ThreadStore
from .tracking import (
    CANCEL_TRACK_TAG,
    INTEREST_POLICY,
    MAX_TRACK_TAGS,
    TRACK_AT_TAG,
    TRACK_EVERY_TAG,
    TRACK_TAG,
    OutboxMessage,
    ProviderError,
    RutenFetcher,
    Source,
    TrackerStore,
    TwitchFetcher,
    WebFetcher,
    XFetcher,
    YouTubeFetcher,
    cadence,
    extract_track_tags,
    parse_ruten_locator,
    parse_times,
    parse_twitch_locator,
    parse_web_locator,
    parse_x_locator,
    parse_youtube_locator,
    render_watches,
    tracking_loop,
)
from .ui import (
    CANCELLED,
    AnswerButton,
    AnswerView,
    CancelView,
    CharacterView,
    InstructionsModal,
    StyleModal,
    recover_exchange,
)
from .usage import probe_rate_limits, read_rate_limits

LOGGER = logging.getLogger(__name__)
# Closes every recall round; a spare taking over mid-recall gets the round's results without it.
ANSWER_NOW = "\n\nNow answer the member's question."
QUEUE_FULL_MESSAGE = "目前排隊已滿，請稍後再試。"
# Discord closes an interaction's token 15 minutes after the command or click. A local model may
# take longer (LOCAL_LLM_TIMEOUT_SECONDS), so past this an answer is posted in the channel.
INTERACTION_LIFETIME = timedelta(minutes=14)


LATE_NOTICE = "回答超過 14 分鐘，完成後會另外發在這個頻道。"


def interaction_expired(interaction) -> bool:
    created = getattr(interaction, "created_at", None)
    return created is not None and discord.utils.utcnow() - created >= INTERACTION_LIFETIME


class SlashPlaceholder:
    """A slash command's deferred response, painted while the answer runs. At
    INTERACTION_LIFETIME it is replaced once by LATE_NOTICE, the cancel button kept, and painted
    no more: the token closes soon after, and the answer then goes to the channel."""

    def __init__(self, interaction) -> None:
        self.interaction = interaction
        self.retired = False
        self.view = None

    async def show(self, text: str, view) -> None:
        if self.retired and view is not None:  # a cancellation (no view) still gets through
            return
        self.view = view
        with contextlib.suppress(discord.HTTPException):
            await self.interaction.edit_original_response(content=text, view=view)

    async def retire_when_late(self) -> None:
        created = getattr(self.interaction, "created_at", None)
        if created is None:
            return
        left = created + INTERACTION_LIFETIME - discord.utils.utcnow()
        await asyncio.sleep(max(0.0, left.total_seconds()))
        await self.retire()

    async def retire(self) -> None:
        if self.retired:
            return
        self.retired = True
        with contextlib.suppress(discord.HTTPException):
            await self.interaction.edit_original_response(content=LATE_NOTICE, view=self.view)


def failure_text(backend: str, error: BaseException, prefix: str) -> str:
    """What the member reads when a turn fails, naming the backend that failed: its own words
    when it wrote them for members (BackendError), otherwise who can look into it. Only Codex
    has a login /status can check."""
    if isinstance(error, BackendError):
        return str(error)
    check = f"用 /{prefix}-status 檢查登入狀態，並" if backend == CODEX else ""
    label = BACKEND_LABELS.get(backend, backend)
    return f"{label} 執行失敗。請{check}通知 Bot 管理者查看 container log。"


SCOPE_CHOICES = [app_commands.Choice(name=label, value=value) for value, label in SCOPES.items()]
LINKCLEAN_CHOICES = [app_commands.Choice(name="查詢", value="status")] + [
    app_commands.Choice(name=label, value=value) for value, label in MODES.items()
]
INSTRUCTION_CHOICES = [
    app_commands.Choice(name=label, value=value)
    for value, label in (
        ("status", "查詢"),
        ("upload", "上傳"),
        ("reset_persona", "還原人設為預設"),
        ("reset_style", "還原輸出風格為預設"),
    )
]
PERSONA_CHOICES = [
    app_commands.Choice(name=label, value=value)
    for value, label in (("keep", "保留人設"), ("off", "關閉人設"))
]
IMAGE_ASPECT_CHOICES = [app_commands.Choice(name=a, value=a) for a in imagegen.ASPECTS]
IMAGE_UPSCALE_CHOICES = [app_commands.Choice(name=u, value=u) for u in imagegen.UPSCALES]
IMAGE_ENHANCE_LABELS = {"local": "地端改寫", "cloud": "雲端改寫", "off": "不改寫"}
IMAGE_ENHANCE_CHOICES = [
    app_commands.Choice(name=IMAGE_ENHANCE_LABELS[e], value=e) for e in imagegen.ENHANCERS
]
IMAGE_OFF = "圖片生成尚未啟用；管理者需設定 IMAGE_GEN_URL。"
IMAGE_STARTED = "🎨 圖片生成中，完成後會貼在這個頻道。"
LOCAL_THREADS_KEPT = 1000
# The host's GPU queue holds one running and three waiting items; a tag past that would only be
# refused by the service and post one more failure message each, so an answer runs at most these.
IMAGE_TAGS_RUN = 4
IMAGE_NOT_ASKED = "（沒有用地端生圖：要用的話請明講「地端」或模型名稱，或用 /{prefix}-image）"
# `/image character:` (characters.py): the member's private yes/next/cancel, unanswered = cancel.
CHARACTER_CONFIRM_SECONDS = 300
CHARACTER_TOO_LATE = "確認角色的時間已到，請重新下一次指令。"
CHARACTER_LOOKING = "🔎 查角色中…"
CHARACTER_ASKING = "🔎 請在只有你看得到的訊息裡確認角色…"
# A bot-only switch (not a service field): /image shows it under this name.
FORCE_LORA_NAME = "強制lora"
FORCE_LORA_ALONE = f"「{FORCE_LORA_NAME}」要搭配 character 一起用。"
CHARACTER_WITH_REF = (
    f"character 和{imagegen.shown('ref_image')}只能擇一：要用自己的角色圖就不用填 character。"
)


class CharacterDeclined(Exception):
    """The member cancelled the character question, or let it time out."""


EMBEDFIX_CHOICES = [
    app_commands.Choice(name=label, value=value)
    for value, label in (("status", "查詢"), ("on", "開啟"), ("off", "關閉"))
]
# Discord allows 25 choices per option; Codex + 14 agy slugs = 15. Built with the default
# Codex model name, which is also what load_config() falls back to.
EFFORT_CHOICES = [app_commands.Choice(name=v, value=k) for k, v in REASONING_EFFORTS.items()]
VIDEO_INTERIM = "🎬 影片較長，前輩正在看，稍等…"
THINKING = "🤔 思考中…"
STREAM_EDIT_SECONDS = 1.5  # Discord edits per placeholder while an answer streams in


class Fallback(NamedTuple):
    """The last time a turn left its backend: when, why, which backend failed, and what
    answered instead. Shown on /status; with MODEL_CHAIN any backend can be the failed one."""

    at: float
    why: BackendUnavailable
    failed: str
    spare: Resolved


# Why the last request fell back, for /status only. type() match, not isinstance: these are
# sibling subclasses, and anything not listed (CodexServiceError, future kinds) reads as generic.
_FALLBACK_LABEL = {
    CodexUsageLimit: "額度用完",
    CodexServerOverloaded: "模型滿載",
    CodexUnauthorized: "登入失效",
}
STREAM_SHOW_CHARS = 1900
PROVIDER_CHOICES = [
    app_commands.Choice(name="Grok（xAI）", value=GROK),
    app_commands.Choice(name="Codex", value="codex"),
    app_commands.Choice(name="Antigravity（Gemini／Claude）", value=AGY),
    app_commands.Choice(name="OpenRouter（免費模型）", value=OPENROUTER),
    app_commands.Choice(name="OrcaRouter（免費模型）", value=ORCAROUTER),
    app_commands.Choice(name="地端模型（本機）", value=LOCAL),
]
FREE_MODEL_NOTE = "免費模型可能隨時不穩或下架，失敗時請換一個。"

# What /model with only provider Antigravity picks; checked against AGY_FAMILIES at use, so a
# renamed family asks the member to pick instead of quietly saving the server default.
AGY_DEFAULT_FAMILY = "gemini-3.8-flash"


X_ITEM_KINDS = ("post", "reply", "repost")  # what XFetcher produces


def tracking_provider(locator: str) -> str:
    """Return the provider for a supported locator without doing network I/O. The specific
    providers are tried first; anything else that is a public page is tracked as a page."""
    for provider, parser in (
        ("youtube", parse_youtube_locator),
        ("twitch", parse_twitch_locator),
        ("x", parse_x_locator),
        ("ruten", parse_ruten_locator),
        ("web", parse_web_locator),
    ):
        try:
            parser(locator)
        except ValueError:
            continue
        return provider
    raise ValueError(
        "追蹤需要 YouTube 頻道、Twitch 頻道、X 帳號、露天賣場網址，或任何 http(s) 網頁網址。"
    )


def tracking_message(message: OutboxMessage, source_label: str = "") -> str:
    """One notification in the Bot's own voice: who, what, and the link. Category, confidence
    and reasoning are log material — read on request, never pushed at the member. Social text
    is escaped before Discord sees it."""
    item, decision, watch = message.item, message.decision, message.watch
    # Titles are other people's text (an X post's is its whole first line): no mentions, and no
    # markdown either, so a masked link cannot pose as something else.
    title = discord.utils.escape_markdown(discord.utils.escape_mentions(item.title.strip()))
    title = title[:300] or "（無標題）"
    # What the model wrote, having read the content, the policy and the source. The fallback is
    # only for a decision made before the model was asked for wording.
    said = discord.utils.escape_mentions(decision.message.strip())[:600]
    if not said:
        who = discord.utils.escape_mentions(source_label.strip())[:80] or "追蹤的頻道"
        what = discord.utils.escape_mentions(decision.category.strip())[:60] or "新內容"
        said = f"前輩發現{who}有{what}了"
    # The owner always; anyone else only because the member named them when asking.
    targets = [uid for uid in (watch.user_id, *watch.mention_ids) if uid]
    mention = " ".join(f"<@{uid}>" for uid in targets)
    body = f"{said}\n**{title}**\n{item.url}"
    if item.kind in X_ITEM_KINDS and urlsplit(item.url).hostname == "x.com":
        # Read out of X search by a model (xsearch sidecar), not from an API: say so.
        body += "\n（X 搜尋取得，作者與內容未經驗證）"
    return truncate(f"{mention} {body}" if mention else body, 1900)


def render_decision_log(rows: list[dict]) -> str:
    """The judgement history for one member, shown only to them. This is the log behind the
    notifications: every item that was looked at, and why it was or was not worth interrupting."""
    if not rows:
        return "還沒有判斷紀錄。追蹤只看建立之後的新內容，所以剛建立時這裡是空的。"
    lines = ["你的追蹤判斷紀錄（最新在上，只有你看得到）："]
    for row in rows:
        mark = "🔔 提醒了" if row["notify"] else "🔇 沒提醒"
        when = str(row["at"])[:16].replace("T", " ")
        title = discord.utils.escape_mentions(str(row["title"]))[:120]
        reason = discord.utils.escape_mentions(str(row["reason"]))[:200] or "未提供理由"
        source = discord.utils.escape_mentions(str(row["source"]))[:60]
        lines.append(f"{mark} · {when} · {source}\n　{title}\n　理由：{reason}\n　{row['url']}")
    return "\n".join(lines)


def instructions_version(config: Config) -> str:
    """Fingerprint of the instruction files Codex bakes into a thread at its start."""
    digest = hashlib.sha256()
    for path in (
        config.codex_workspace / "AGENTS.md",
        config.codex_workspace_plain / "AGENTS.md",
        instructions.style_path(config),
    ):
        try:
            digest.update(path.read_bytes())
        except OSError:
            pass
        digest.update(b"\0")
    return digest.hexdigest()[:12]


def strip_mention(content: str, bot_id: int) -> str:
    """Remove every <@id> / <@!id> mention of the bot so only the question remains."""
    return re.sub(rf"<@!?{bot_id}>", "", content).strip()


def mentions_explicitly(content: str, bot_id: int) -> bool:
    """An @ typed into the text -- as opposed to the ping Discord adds to a reply, which also
    lands in `message.mentions` but says nothing about who the member is talking to."""
    return re.search(rf"<@!?{bot_id}>", content) is not None


def with_quoted_message(prompt: str, author: str, content: str, image_count: int) -> str:
    """Fold a replied-to member message into the prompt so the model sees what was pointed at.
    It is fenced (codex.QUOTE_FENCE): _prompt puts it in QUOTED_MESSAGE, never USER_MESSAGE, so
    memory is never built from another member's words. Links in it are still fetched."""
    quoted = " ".join(content.split())
    parts = []
    if quoted:
        parts.append(f"（後輩回覆了 {author} 的訊息：「{quoted}」）")
    if image_count:
        parts.append(f"（那則訊息附了 {image_count} 張圖，已一併附上）")
    if not parts:
        return prompt
    return "\n".join([QUOTE_FENCE, *parts, QUOTE_FENCE, prompt or "請看這則訊息。"])


READ_TAGS = (SEARCH_TAG, RECALL_TAG, FETCH_TAG, search.WEB_TAG, sandbox.RUN_TAG, apis.API_TAG)
# A read tag in a shape its parser does not take. Still a request — answered with the reason, so
# the model can send it again — never an answer to post with the raw tag in it.
_READ_ATTRS = r'(?:(?:[^<>"]|"[^"]{0,2000}"){0,4000}|[^<>]{0,2500})'  # stray quote: the plain one
_READ_NAME = r"(?:search|recall|fetch|web|api)(?![a-z_])"
BROKEN_READ = re.compile(
    # A <run> body goes to its </run> but never past the next <run>: an answer full of unclosed
    # ones would otherwise be read to the end again from each of them.
    rf"<run(?![a-z_]){_READ_ATTRS}>(?:(?!<run(?![a-z_]))[\s\S]){{0,20000}}?</run>"
    # The others may carry their value as a body (<web>q</web>) instead of an attribute.
    rf"|<{_READ_NAME}{_READ_ATTRS}>(?:[^<]{{0,2000}}</{_READ_NAME}>)?"
    # Cut off: a <run> whose </run> never came, another read tag stopping before its ">".
    rf"|<run(?![a-z_])[^<>]{{0,2000}}>(?:(?!<run(?![a-z_]))[\s\S]){{0,20000}}\Z"
    rf"|<{_READ_NAME}\s[^<>\n]{{0,2500}}$",
    re.M,
)
WEB_PER_ROUND = 2
# Every read round spent and the model still asking: what it asked for last is not read.
READS_SPENT = (
    '<RESULT kind="error">\nThe read limit for this question is used up; nothing more will be'
    " read. Answer with what you have.\n</RESULT>"
)
READS_SPENT_MESSAGE = "（⚠️ 查詢次數用完了，還沒整理出答案；請換個問法再問一次。）"


def request_only(answer: str) -> bool:
    """True when the reply is nothing but read/fetch tags — the protocol for asking the Bot to
    read something. A tag embedded in prose (quoted from a fetched page, say) is just text."""
    rest = answer
    for tag in READ_TAGS:
        rest = tag.sub("", rest)
    return bool(answer.strip()) and not BROKEN_READ.sub("", rest).strip()


def unserved_reads(answer: str, config: Config, has_apis: bool) -> list[str]:
    """The requests in a request-only reply the Bot will not carry out this round, each with the
    reason: written in a shape no parser takes, for a tool this Bot does not have, or past a
    round's limit. They used to end the read loop, and the reply — raw tags — was posted."""
    rest = answer
    for tag in READ_TAGS:
        rest = tags.strip(tag, rest)
    problems = [f"{tag}: not in the documented form" for tag in tags.findall(BROKEN_READ, rest)]
    if sandbox.extract_runs(answer) and not sandbox.available(config):
        problems.append("<run>: there is no sandbox here; answer without running code")
    calls = list(dict.fromkeys(tags.findall(apis.API_TAG, answer)))
    if calls and not has_apis:
        problems.append("<api>: no APIs are registered here")
    limits = (
        ("fetch", len(extract_fetch_tags(answer)), config.link_max_urls),
        ("web", len(search.extract_web_queries(answer)), WEB_PER_ROUND),
        ("api", len(calls) if has_apis else 0, apis.MAX_CALLS_PER_ROUND),
        (
            "run",
            len(tags.findall(sandbox.RUN_TAG, answer)) if sandbox.available(config) else 0,
            sandbox.MAX_RUNS_PER_ROUND,
        ),
    )
    for name, asked, limit in limits:
        if asked > limit:
            problems.append(
                f"<{name}>: only the first {limit} are carried out per round; send the rest again"
            )
    return problems


# A control tag the model made up. Every real one it wrote is handled and stripped before this
# runs, and one it only shows (tags.py) is left alone here too, so whatever is still here is an
# operation the Bot cannot perform — and the sentence above it has
# already told the member it was performed. The underscore is what makes this safe to match on:
# the Bot's own vocabulary uses it (track_every, cancel_reminder, cancel_track) and no HTML or
# XML element a member might be shown does, so quoted markup is never mistaken for an order.
INVENTED_TAG = re.compile(r'<([a-z]{1,15}_[a-z_]{1,15})(?:\s+[a-z_]{1,16}="[^"]{0,200}")*\s*/?>')


# A tag of the Bot's own name left over after its parser ran: written in a shape the parser does
# not take (an unquoted value, a body instead of text=), so nothing was done for it. A quoted value
# is skipped whole: who="<@id>" is the documented form and holds both < and >; so is a mention
# written without quotes (who=<@id>).
_OWN = (
    r"(?:remind|cancel_reminder|track|track_every|track_at|cancel_track|memory|image_gen)"
    r"(?![a-z_])"
)
_MENTION = r"<[@#][!&]?[0-9]{1,25}>"
OWN_NAME = re.compile(_OWN)
NO_WATCH = (
    "this member has no such watch; send another id only if the member named that one,"
    " otherwise reply with nothing"
)
# A stray quote inside a value (text="他說"好") breaks the quoted reading; the plain one is the
# fallback. A tag cut off before its ">" (the answer ended, or a quote was never closed) is one
# too when it runs to the end of its line.
BROKEN_TAG = re.compile(
    rf'<{_OWN}(?:(?:[^<>"]|"[^"]{{0,2000}}"|{_MENTION}){{0,4000}}>'
    rf"|(?:[^<>]|{_MENTION}){{0,2500}}>"
    rf"|\s(?:[^<>\n]|{_MENTION}){{0,2500}}$)",
    re.M,
)
_BROKEN_CLOSE = re.compile(rf"</{_OWN}>")
# The operations an answer can carry out; a retry is read for these and nothing else.
ACTION_TAGS = (
    MEMORY_TAG,
    REMIND_TAG,
    CANCEL_TAG,
    TRACK_TAG,
    TRACK_EVERY_TAG,
    TRACK_AT_TAG,
    CANCEL_TRACK_TAG,
    IMAGE_TAG,
)
# One more turn for the operations the Bot could not read, with the reason, so the model can send
# them again instead of the member being told it was done when it was not.
TAG_RETRY = (
    "<TAG_ERRORS>\n{errors}\n</TAG_ERRORS>\n"
    "The Bot could not carry out the tags above from your last answer (each says why); nothing"
    " was done for them. Every other tag in that answer was carried out: do not send those"
    " again. Reply with only the corrected tags, in exactly the documented form, and no other"
    " text — or with nothing if they cannot be fixed, or if a tag above was only quoted or"
    " explained rather than meant as an action."
)


_ERRORS_NAME = re.compile(r"(tag)([\s_-]*errors)", re.I)


def fence_errors(text: str) -> str:
    """The failed tags as data inside TAG_ERRORS: a tag that quotes a member or a page may hold
    "</TAG_ERRORS>", which would end the envelope early and speak as the Bot to the retry."""
    return _ERRORS_NAME.sub(lambda m: m[1] + "\u2060" + m[2], text)


_REFUSED = re.compile(r"(?:(\d+) )?<([a-z_]+)")


def unread_left(unread: list[str], refused: list[str], resent: list[str]) -> int:
    """How many unreadable tags the retry did not send back. A resent tag goes first to a refusal
    of its name (each says which tag it was, and how many), then to an unreadable tag of its
    name; a made-up name can be fixed by any tag. A fixed refusal, or some other tag, must not
    hide an operation that never happened."""
    owed: dict[str, int] = {}
    for line in refused:
        if found := _REFUSED.match(line):
            owed[found[2]] = owed.get(found[2], 0) + int(found[1] or 1)
    need: dict[str, int] = {}
    wild = 0
    for line in unread:
        name = _REFUSED.match(line)
        if name and OWN_NAME.fullmatch(name[2]):
            need[name[2]] = need.get(name[2], 0) + 1
        else:
            wild += 1
    for tag in resent:
        name = re.match(r"<([a-z_]+)", tag)
        key = name[1] if name else ""
        if owed.get(key, 0) > 0:
            owed[key] -= 1
        elif need.get(key, 0) > 0:
            need[key] -= 1
        elif wild > 0:
            wild -= 1
    return sum(need.values()) + wild


def action_tags(text: str) -> list[str]:
    """The live action tags of `text` in order, everything around them dropped. A tag inside
    another one (a member's words quoted in a <memory> body) is that tag's data, not an order."""
    found = sorted((m for p in ACTION_TAGS for m in tags.live(p, text)), key=lambda m: m.start())
    kept: list[re.Match[str]] = []
    for match in found:
        if not kept or match.start() >= kept[-1].end():
            kept.append(match)
    return [m.group(0) for m in kept]


# The attributes the Bot's own tags take. An own name with none of them is HTML or C++ in the
# answer's prose (<track kind="captions">, #include <memory>), not an order that failed.
_KEYS = re.compile(
    r"\b(?:when|text|who|source|interest|every|at|id|minutes|times|scope|name"
    r"|prompt|model|aspect|count|seed|negative)\s*=",
    re.I,
)


def _ends_a_line(text: str, match: re.Match[str]) -> bool:
    """Is `match` where an operation goes — after the words of its line, alone or with other
    tags? The prompt has the model append its tags after the answer. One with more words after
    it on its line is being talked about (a member's syntax, a page's), not done."""
    stop = text.find("\n", match.end())
    after = text[match.end() : len(text) if stop < 0 else stop]
    return not INVENTED_TAG.sub("", BROKEN_TAG.sub("", after)).strip()


def _spaced(tag: str) -> str:
    """A tag with its spacing evened out, so a resend that differs by a space is still the same."""
    return " ".join(tag.split())


def _cut(text: str, matches: list[re.Match[str]]) -> str:
    parts, at = [], 0
    for match in matches:
        parts.append(text[at : match.start()])
        at = match.end()
    parts.append(text[at:])
    return "".join(parts)


def strip_broken_tags(text: str) -> tuple[str, list[str]]:
    """(text without the leftover tags of the Bot's own names, those tags as written)."""
    broken = [
        m for m in tags.live(BROKEN_TAG, text) if _KEYS.search(m.group(0)) and _ends_a_line(text, m)
    ]
    if not broken:
        return text, []
    return tags.strip(_BROKEN_CLOSE, _cut(text, broken)).strip(), [m.group(0) for m in broken]


def strip_invented_tags(text: str) -> tuple[str, list[str]]:
    """(text with the leftover tags removed, the names that were in it).

    2026-09-19: asked in the channel to stop a watch, the model wrote "星街的追蹤取消了" and
    `<cancel_track id="1"/>`. Cancelling was slash-only, so nothing happened, the raw tag was
    posted, and the watch kept notifying. `<cancel_track>` is real now, but the next invented
    tag is a matter of time; what must not survive is a claim the Bot never carried out.
    Stripping alone would make that worse — the visible tag is the only reason this was caught
    at all — so the member gets told, and the log gets the name to fix next.
    """
    found = [m for m in tags.live(INVENTED_TAG, text) if _ends_a_line(text, m)]
    if not found:
        return text, []
    return _cut(text, found).strip(), list(dict.fromkeys(m.group(1) for m in found))


def _few(ids: list[int], shown: int = 3) -> str:
    """A reminder's people for a listing: a group of ninety would not fit a message."""
    head = " ".join(f"<@{t}>" for t in ids[:shown])
    return head if len(ids) <= shown else f"{head} 等 {len(ids)} 人"


def _with_notes(clean: str, notes: list[str], notes_out: list[str] | None) -> str:
    """`clean` with the confirmations appended, or handed to `notes_out` when the caller wants
    them kept apart from the model's words."""
    if notes_out is not None:
        notes_out.extend(notes)
        return clean
    return f"{clean}\n\n" + "\n".join(notes)


def _for_other(item: dict) -> bool:
    """A reminder set for someone other than the member who set it."""
    return reminder_targets(item) != [item["user_id"]]


def _bot_sees(guild: discord.Guild | None, channel) -> bool:
    """True when the Bot itself can view `channel` in `guild`: the one rule server memory and
    emoji samples share. No guild or no channel means not seen. A thread whose parent is not
    cached raises discord.ClientException, which each caller treats as not seen (the digest
    leaves the channel out; an emoji event handler stops there)."""
    if guild is None or not isinstance(channel, (discord.abc.GuildChannel, discord.Thread)):
        return False
    return bool(channel.permissions_for(guild.me).view_channel)


def _reply_reference(message: discord.Message) -> discord.MessageReference | None:
    """The reference a repost of `message` must carry to stay a reply to the same message.
    Replacing a link-only reply would otherwise drop it out of its conversation, which is why
    replies used to be left alone entirely. A forward is not a reply and carries content the
    repost cannot reproduce, so those still are. `fail_if_not_exists=False`: a replied-to
    message deleted in the meantime costs the reply arrow, not the repost."""
    reference = message.reference
    if reference is None or reference.message_id is None:
        return None
    kind = getattr(reference, "type", None)
    if kind is not None and kind != discord.MessageReferenceType.default:
        return None
    return discord.MessageReference(
        message_id=reference.message_id,
        channel_id=reference.channel_id,
        guild_id=reference.guild_id,
        fail_if_not_exists=False,
    )


def previews_from(messages) -> dict[str, Preview]:
    """Discord link embeds of `messages` as previews keyed by the embedded URL."""
    out: dict[str, Preview] = {}
    for message in messages:
        for embed in message.embeds:
            if not embed.url or embed.type not in ("article", "link", "rich", "video"):
                continue
            picture = embed.thumbnail if embed.thumbnail and embed.thumbnail.url else embed.image
            image_url = ""
            if picture and picture.url:
                image_url = picture.proxy_url or picture.url
            out.setdefault(
                embed.url,
                Preview(embed.url, embed.title or "", embed.description or "", image_url),
            )
    return out


async def _model_autocomplete(
    interaction: discord.Interaction, current: str
) -> list[app_commands.Choice[str]]:
    provider = getattr(interaction.namespace, "provider", None) or "codex"
    return await interaction.client.model_options(provider, current)


async def _image_model_autocomplete(
    interaction: discord.Interaction, current: str
) -> list[app_commands.Choice[str]]:
    client = interaction.client
    if client._access(interaction.guild_id, interaction.channel, interaction.channel_id):
        return []  # no request to the image service for a place the Bot does not serve
    return await client.image_model_options(current)


async def _memory_autocomplete(
    interaction: discord.Interaction, current: str
) -> list[app_commands.Choice[str]]:
    client = interaction.client
    if client._access(interaction.guild_id, interaction.channel, interaction.channel_id):
        return []
    scope = getattr(interaction.namespace, "scope", None)
    if scope not in SCOPES:
        return []
    return client.memory_options(interaction.guild_id, interaction.user.id, scope, current)


NO_X_NOTE = "（這次沒有 X 搜尋：Grok 現在不能用，以上不是從 X 上查到的。）"


@dataclass(frozen=True, slots=True)
class RoutePlan:
    """The router's pick for one request (DiscordCodexClient._route)."""

    target: Resolved
    value: str  # "<backend>:<model>" that will hold the thread (ThreadStore model)
    route: Route
    resume: str
    history: str  # replayed to the target: it does not hold this conversation
    replay: str  # the same conversation, for a stand-in or spare taking over a resumed turn
    spares: tuple[Resolved, ...]
    log: dict
    no_x: bool  # an X question: say so when something other than Grok answers it


class DiscordCodexClient(discord.Client):
    def __init__(self, config: Config) -> None:
        intents = discord.Intents.none()
        intents.guilds = True
        # @mention entry point: message_content is needed to read the question and its images.
        intents.guild_messages = True
        intents.message_content = True
        intents.emojis_and_stickers = True  # keeps guild.emojis populated for the 記住 button
        if config.emoji_enabled:
            intents.guild_reactions = True  # emoji.py: a reaction with the server's emoji
        # What the model writes is sent as is: an answer quoting @everyone, a role or a member
        # must not ping them. Only replying pings the member who asked; a send
        # that means to mention someone (reminders, tracking) says so with its own value.
        mentions = discord.AllowedMentions(everyone=False, users=False, roles=False)
        super().__init__(intents=intents, allowed_mentions=mentions)
        self.config = config
        prefix = config.command_prefix
        self.tree = app_commands.CommandTree(self)
        self.queue = SerialQueue(config.max_queued_jobs)
        # Local-model turns run outside the queue (`_answer`); a conversation's lock and how
        # many turns hold or wait for it.
        self._thread_turns: dict[str, list] = {}
        self.threads = ThreadStore(
            config.codex_home / "discord_threads.json",
            config.thread_ttl_minutes * 60,
            instructions_version(config),
        )
        limits = MemoryLimits(
            config.memory_index_max_lines,
            config.memory_index_max_bytes,
            config.memory_user_max_bytes,
            config.memory_guild_max_bytes,
            config.memory_read_max_lines,
            config.memory_read_max_bytes,
            config.memory_search_max_matches,
            config.memory_search_context_lines,
        )
        self.memory = MemoryStore(config.codex_home / "memory", limits)
        self.permanent = PermanentMemory(
            config.permanent_memory_dir, limits, config.data_dir / "topics"
        )
        # The server's custom emoji and how members use them (emoji.py); None while off.
        self.emoji = EmojiStore(config.emoji_db_path) if config.emoji_enabled else None
        # channel id -> (id, text) of its latest message: the "previous" of an emoji sample.
        self._last_message: dict[int, tuple[int, str]] = {}
        self.active: dict[str, asyncio.Task] = {}  # in-flight request per member+channel
        self.alerts = Alerter(self, config)
        self._started_at = time.time()  # an --after-deploy announcement waits for a later start
        # The most recent request answered on a spare backend: (at, why, failed backend, spare).
        # Shown by /status only; the answer itself carries no notice.
        self._last_fallback: Fallback | None = None
        # Grok sessions the sidecar refused, for the circuit breaker in _grok_usable.
        self._grok_refusals: list[float] = []
        self._grok_off_until = 0.0
        # Checked here so a bad routing table stops the Bot at start-up instead of quietly
        # sending every member to DEFAULT_MODEL.
        self.routing: Table | None = (
            routing.load_table(config.routing_path) if config.jev_enabled else None
        )
        self._emoji_cache: dict[int, dict[str, discord.PartialEmoji]] = {}
        self.reminders = ReminderStore(config.codex_home / "reminders.json")
        self.apis = apis.load_registry(config.apis_path, config.data_dir)
        self.openrouter = Catalog(config)
        self.orcarouter = Catalog(config, ROUTERS[ORCAROUTER])
        self.local = Catalog(config, ROUTERS[LOCAL])
        self.catalogs = {
            OPENROUTER: self.openrouter,
            ORCAROUTER: self.orcarouter,
            LOCAL: self.local,
        }
        self.tracker = TrackerStore(config.tracking_db_path) if config.tracking_enabled else None
        self.linkclean = SwitchStore(config.codex_home / "linkclean.sqlite3")
        # Image generation (imagegen.py): the service client, each guild's switch and spoiler
        # setting in linkclean's database, and detached delivery tasks.
        self.images = ImageService(config)
        self.image_settings = ImageSettings(self.linkclean)
        self.character_web: characters.Web = characters.HttpWeb(config)  # Danbooru, Civitai
        self._image_tasks: set[asyncio.Task] = set()  # <image_gen/> deliveries, kept referenced
        # Conversations (thread ids) where the member asked for the local image service: a
        # follow-up there ("now 16:9") counts as asking again. Oldest dropped past the cap.
        self._local_image_threads: dict[str, None] = {}
        self.agy_quota = quota.AgyQuota(float(config.agy_reserve_percent))
        quota.shared = self.agy_quota  # for background jobs' agy fallback (backends.run_batch)
        # Persona uploads and resets run one at a time: backup, save and the rebuild interleaved
        # could leave the older text built last.
        self._instructions_lock = asyncio.Lock()
        self.youtube_tracker = YouTubeFetcher(config.youtube_api_key)
        self.twitch_tracker = TwitchFetcher(config.twitch_client_id, config.twitch_client_secret)
        self.web_tracker = WebFetcher(self._read_page)
        self.ruten_tracker = RutenFetcher()
        self.x_tracker = XFetcher(
            lambda handle: xsearch.lookup_user(config, handle),
            lambda handle, since_id, **page: xsearch.recent_posts(config, handle, since_id, **page),
            config.x_tracking_interval_minutes,
        )
        # Operator commands (python -m discord_codex_bot.admin) run here, in this process, so
        # they go through the same in-memory stores and checks as members' own commands.
        self.admin = AdminServer(self, admin_root(config))
        self.tree.add_command(
            app_commands.Command(
                name=f"{prefix}-remember",
                description="記住一件事（個人或整個伺服器）",
                callback=self.remember_command,
            )
        )
        self.tree.add_command(
            app_commands.Command(
                name=f"{prefix}-forget",
                description="刪除一則記憶（從名稱選單選取；取消追蹤用 -track）",
                callback=self.forget_command,
            )
        )
        self.tree.add_command(
            app_commands.Command(
                name=f"{prefix}-memory",
                description="查看 Bot 記得的事（只有你看得到）",
                callback=self.memory_command,
            )
        )
        self.tree.add_command(
            app_commands.Command(
                name=f"{prefix}-model",
                description=(
                    "查看／選擇／清除你要用的模型（Codex、Antigravity、OpenRouter、OrcaRouter、地端模型）"
                ),
                callback=self.model_command,
            )
        )
        self.tree.add_command(
            app_commands.Command(
                name=f"{prefix}-style",
                description="查看／設定／清除你的個人回覆風格（覆蓋預設）",
                callback=self.style_command,
            )
        )
        self.tree.add_command(
            app_commands.Command(
                name=prefix,
                description="詢問此伺服器的 Codex agent",
                callback=self.codex_command,
            )
        )
        self.tree.add_command(
            app_commands.Command(
                name=f"{prefix}-help",
                description="所有指令的說明與範例用法",
                callback=self.help_command,
            )
        )
        self.tree.add_command(
            app_commands.Command(
                name=f"{prefix}-persona",
                description="管理員：查詢／上傳／還原全伺服器共用的人設與預設輸出風格",
                callback=self.persona_command,
            )
        )
        self.tree.add_command(
            app_commands.Command(
                name=f"{prefix}-status",
                description="看你的模型／風格／續接／記憶設定與系統狀態",
                callback=self.status_command,
            )
        )
        self.tree.add_command(
            app_commands.Command(
                name=f"{prefix}-reset",
                description="忘掉你在此頻道的對話脈絡，下一題從頭開始",
                callback=self.reset_command,
            )
        )
        self.tree.add_command(
            app_commands.Command(
                name=f"{prefix}-summary",
                description="摘要這個頻道最近的對話（重點、結論、待辦）",
                callback=self.summary_command,
            )
        )
        self.tree.add_command(
            app_commands.Command(
                name=f"{prefix}-remind",
                description="設定提醒：到時在這個頻道 @你；留空＝列出你的提醒",
                callback=self.remind_command,
            )
        )
        self.tree.add_command(
            app_commands.Command(
                name=f"{prefix}-export",
                description="把你在這個伺服器的個人記憶打包成 zip 給你（只有你看得到）",
                callback=self.export_command,
            )
        )
        self.tree.add_command(
            app_commands.Command(
                name=f"{prefix}-stop",
                description="取消你在此頻道進行中的請求（回答上也有 ❌ 按鈕）",
                callback=self.stop_command,
            )
        )
        self.tree.add_command(
            app_commands.Command(
                name=f"{prefix}-track",
                description="追蹤 YouTube／Twitch／X／露天賣場／網頁；留空列出，或用編號取消",
                callback=self.track_command,
            )
        )
        self.tree.add_command(
            app_commands.Command(
                name=f"{prefix}-linkclean",
                description="本伺服器的連結洗參數開關（只有伺服器的管理層）",
                callback=self.linkclean_command,
            )
        )
        self.tree.add_command(
            app_commands.Command(
                name=f"{prefix}-image",
                description="生成圖片：描述要畫什麼，可選模型、比例、張數",
                callback=self.image_command,
            )
        )
        self.tree.add_command(
            app_commands.Command(
                name=f"{prefix}-embedfix",
                description="本伺服器的預覽修正開關：社群貼文連結換成 Discord 能預覽的版本",
                callback=self.embedfix_command,
            )
        )

    async def setup_hook(self) -> None:
        # Scratch files of writes killed mid-way (atomic_write only cleans up after its errors).
        try:
            if removed := await asyncio.to_thread(self.memory.sweep_scratch):
                LOGGER.info("Removed %d stale memory scratch file(s)", removed)
        except OSError:
            LOGGER.exception("Memory scratch sweep failed")
        self.add_dynamic_items(AnswerButton)  # answer buttons keep working across restarts
        # Right after the quick scratch sweep and before the slow steps below (command sync,
        # the background loops), so the healthcheck's clock starts as early as it can.
        self._heartbeat = self.loop.create_task(health.beat_forever(self._gateway_up))
        self._sweeper = self.loop.create_task(sweep_forever(self.config))
        self._backup_loop = self.loop.create_task(backup_forever(self.config))
        if self.emoji is not None and self.config.emoji_describe_hour >= 0:
            self._emoji_describer = self.loop.create_task(self._describe_emoji_forever())
        if self.config.agy_usage_minutes > 0:
            self._agy_usage_loop = self.loop.create_task(self._read_agy_usage_forever())
        self._reminder_loop = self.loop.create_task(
            reminder_loop(self.reminders, self._fire_reminder, 30)
        )
        self._consolidator = self.loop.create_task(
            consolidate_forever(self.memory, self.config, self.queue.run)
        )
        self._digester = self.loop.create_task(
            digest_forever(
                self.threads, self.memory, self.config, self.queue.run, self._visible_channel
            )
        )
        self._harvest_wakeup = asyncio.Event()
        self._harvester = self.loop.create_task(
            harvest_forever(
                self.threads, self.memory, self.config, self.queue.run, self._harvest_wakeup
            )
        )
        if self.tracker is not None:
            self._tracking_loop = self.loop.create_task(self._tracking_forever())
        self.admin.start()
        # A guild that has not invited the bot yet (e.g. production before rollout) must not take
        # the whole client down; the runtime access check still rejects it until it is synced.
        for guild_id in self.config.allowed_guild_ids:
            guild = discord.Object(id=guild_id)
            self.tree.copy_global_to(guild=guild)
            try:
                commands = await self.tree.sync(guild=guild)
            except discord.HTTPException:
                LOGGER.exception("Command sync failed for guild %d; is the bot invited?", guild_id)
                continue
            LOGGER.info("Registered %d commands for guild %d", len(commands), guild_id)

    async def close(self) -> None:
        self.admin.stop()
        await super().close()

    def _gateway_up(self) -> bool:
        ws = self.ws
        return self.is_ready() and ws is not None and ws.open

    async def warm_emojis(self) -> None:
        """Cache each guild's custom emojis by name (REST, so it works even without the intent
        having delivered them yet)."""
        for guild in self.guilds:
            try:
                emojis = await guild.fetch_emojis()
            except discord.HTTPException as error:
                LOGGER.warning("fetch_emojis failed for guild %s: %s", guild.id, error)
                continue
            self._emoji_cache[guild.id] = {
                e.name: discord.PartialEmoji(name=e.name, id=e.id, animated=e.animated)
                for e in emojis
            }
        LOGGER.info("Emoji cache: %s", {g: len(m) for g, m in self._emoji_cache.items()})

    # ----- custom emoji understanding (emoji.py) ---------------------------------------------

    def _sync_emoji(self, guild: discord.Guild) -> None:
        if self.emoji is not None and guild.id in self.config.allowed_guild_ids:
            self.emoji.sync(guild.id, [(e.id, e.name, e.animated) for e in guild.emojis])

    async def on_guild_emojis_update(self, guild: discord.Guild, _before, _after) -> None:
        self._sync_emoji(guild)

    def _samples_from(self, guild_id: int | None, channel) -> bool:
        """Emoji samples come from every channel of an allowed guild the Bot can see, private
        ones included: anything the Bot can read may inform how the server uses its emoji."""
        if self.emoji is None or guild_id not in self.config.allowed_guild_ids:
            return False
        return _bot_sees(self.get_guild(guild_id), channel)

    async def _collect_emoji(self, message: discord.Message) -> None:
        """A member message with the server's own emoji is a sample, with the channel's message
        before it (any author, the Bot's own answers included)."""
        guild_id = message.guild.id if message.guild else None
        if not self._samples_from(guild_id, message.channel):
            return
        channel_id = message.channel.id
        before = self._last_message.get(channel_id)
        self._last_message[channel_id] = (message.id, message.content or "")
        if message.author.bot:
            return
        ids = sorted(
            {
                int(emoji_id)
                for animated, _n, emoji_id in CUSTOM_EMOJI.findall(message.content or "")
                if not animated
            }  # fmt: skip
        )
        ids = [emoji_id for emoji_id in ids if self.emoji.known(guild_id, emoji_id)]
        if not ids:
            return
        if before is None:  # the first message seen in this channel since the Bot started
            before = (None, "")
            try:
                async for earlier in message.channel.history(limit=1, before=message):
                    before = (earlier.id, earlier.content or "")
            except discord.HTTPException:
                pass
        for emoji_id in ids:
            self.emoji.add_sample(
                guild_id,
                emoji_id,
                "message",
                message.id,
                channel_id,
                message.content or "",
                before[1],
                message.created_at.timestamp(),
                previous_id=before[0],
            )

    async def on_raw_message_edit(self, payload: discord.RawMessageUpdateEvent) -> None:
        """The channel's latest message changed (an answer replacing its 「思考中」 placeholder,
        a member's edit): the next sample's "previous" is what it says now."""
        content = payload.data.get("content")
        if not isinstance(content, str):
            return  # an embed resolving, not a text change
        latest = self._last_message.get(payload.channel_id)
        if latest is not None and latest[0] == payload.message_id:
            self._last_message[payload.channel_id] = (payload.message_id, content)
        channel = self.get_channel(payload.channel_id)
        if channel is None or not self._samples_from(payload.guild_id, channel):
            return
        author = payload.data.get("author") or {}
        try:
            at = _dt.fromisoformat(payload.data["timestamp"]).timestamp()
        except (KeyError, TypeError, ValueError):
            at = time.time()
        self.emoji.edit_message(
            payload.guild_id,
            payload.message_id,
            payload.channel_id,
            content,
            at,
            member=not author.get("bot", False),
        )

    async def on_raw_reaction_add(self, payload: discord.RawReactionActionEvent) -> None:
        """Someone reacted with one of the server's emoji: the message it was put on is a sample
        (once; further reactions on it only count as uses)."""
        emoji_id = payload.emoji.id
        if emoji_id is None or payload.emoji.animated or payload.guild_id is None:
            return
        if self.user is not None and payload.user_id == self.user.id:
            return
        channel = self.get_channel(payload.channel_id)
        if channel is None or not self._samples_from(payload.guild_id, channel):
            return
        if not self.emoji.known(payload.guild_id, emoji_id):
            return
        if self.emoji.has_sample(emoji_id, payload.message_id, "reaction"):
            self.emoji.count_use(emoji_id)
            return
        message = discord.utils.get(self.cached_messages, id=payload.message_id)
        if message is None:
            try:
                message = await channel.fetch_message(payload.message_id)
            except (discord.HTTPException, AttributeError):
                return
        self.emoji.add_sample(
            payload.guild_id,
            emoji_id,
            "reaction",
            message.id,
            payload.channel_id,
            message.content or "",
            "",
            message.created_at.timestamp(),
        )

    async def on_raw_message_delete(self, payload: discord.RawMessageDeleteEvent) -> None:
        self._forget_messages(payload.channel_id, [payload.message_id])

    async def on_raw_bulk_message_delete(self, payload: discord.RawBulkMessageDeleteEvent) -> None:
        self._forget_messages(payload.channel_id, payload.message_ids)

    def _forget_messages(self, channel_id: int, message_ids) -> None:
        """Deleted messages leave no trace: not in samples, and not as the cached latest message
        the next sample would take as its "previous"."""
        if self.emoji is None:
            return
        ids = set(message_ids)
        latest = self._last_message.get(channel_id)
        if latest is not None and latest[0] in ids:
            del self._last_message[channel_id]
        self.emoji.forget_messages(ids)

    async def _backfill_emoji(self) -> None:
        """The one-off history read (emoji.backfill_channel) of every allowed guild: each
        channel, active and archived thread the Bot may read, back EMOJI_BACKFILL_DAYS from the
        first run. Channels it may not read are logged by name, never skipped silently."""
        days = self.config.emoji_backfill_days
        anchor = self.emoji.meta("backfill_anchor")
        if anchor is None:
            anchor = _dt.now(UTC).isoformat()
            self.emoji.set_meta("backfill_anchor", anchor)
        start = _dt.fromisoformat(anchor)
        horizon = start - timedelta(days=days)
        for guild in self.guilds:
            if guild.id not in self.config.allowed_guild_ids:
                continue
            channels: list = [*guild.text_channels, *guild.threads]
            unlisted = []  # parents whose archived threads Discord would not list
            # Public archived threads, and the private ones the Bot joined (only text channels have
            # private threads).
            listings = [(parent, {}) for parent in [*guild.text_channels, *guild.forums]]
            # All private archives need Manage Threads; without it, the ones the Bot joined.
            listings += [
                (parent, {"private": True})
                if parent.permissions_for(guild.me).manage_threads
                else (parent, {"private": True, "joined": True})
                for parent in guild.text_channels
            ]
            for parent, kind in listings:
                try:
                    async for thread in parent.archived_threads(limit=None, **kind):
                        if thread.archive_timestamp < horizon:
                            break
                        channels.append(thread)
                except discord.HTTPException as error:
                    which = " private" if kind else ""
                    unlisted.append(f"#{parent.name} ({parent.id}){which}: {error.status}")
            if unlisted:
                # Their archived threads are not read this run; the next start lists them again.
                LOGGER.warning(
                    "Emoji backfill guild=%s: archived threads not listed in %d channel(s): %s",
                    guild.id,
                    len(unlisted),
                    ", ".join(unlisted),
                )
            readable, unreadable = [], []
            for channel in channels:
                allowed = channel.permissions_for(guild.me)
                if allowed.view_channel and allowed.read_message_history:
                    readable.append(channel)
                else:
                    unreadable.append(channel)
            if unreadable:
                LOGGER.warning(
                    "Emoji backfill guild=%s: no View Channel / Read Message History in %d"
                    " channel(s): %s",
                    guild.id,
                    len(unreadable),
                    ", ".join(f"#{c.name} ({c.id})" for c in unreadable),
                )
            for channel in readable:
                try:
                    messages, samples = await backfill_channel(
                        self.emoji, guild.id, channel, start, days
                    )
                except discord.HTTPException:
                    LOGGER.exception("Emoji backfill of #%s (%s) failed", channel.name, channel.id)
                    continue
                if messages:
                    LOGGER.info(
                        "Emoji backfill #%s (%s): %d messages, %d samples",
                        channel.name,
                        channel.id,
                        messages,
                        samples,
                    )
            done = [row for row in self.emoji.status()["backfill"] if row["guild_id"] == guild.id]
            LOGGER.info(
                "Emoji backfill guild=%s done back %d days: %s; unreadable %d, archived threads"
                " not listed in %d",
                guild.id,
                days,
                done[0] if done else {},
                len(unreadable),
                len(unlisted),
            )

    async def _backfill_emoji_safely(self) -> None:
        try:
            await self._backfill_emoji()
        except asyncio.CancelledError:
            raise
        except Exception:
            LOGGER.exception("Emoji backfill stopped; the next start goes on from there")

    async def _emoji_image(self, emoji) -> Path | None:
        found = self.get_emoji(emoji.emoji_id)
        if found is None:
            return None
        self.config.attachment_dir.mkdir(parents=True, exist_ok=True)
        path = self.config.attachment_dir / f"emoji-{emoji.emoji_id}.png"
        try:
            await found.save(path)
        except (discord.HTTPException, discord.NotFound, OSError):
            return None
        return path

    async def _describe_emoji(self, prompt: str, image: Path) -> str:
        return await self.queue.run(
            lambda: run_batch(
                prompt,
                self.config,
                schema=self.config.emoji_schema_path,
                isolated=True,
                images=[image],
            )
        )

    async def _describe_emoji_forever(self) -> None:
        """Daily at EMOJI_DESCRIBE_HOUR, behind the same quota gate as harvest: up to
        EMOJI_DESCRIBE_MAX emoji with new samples, one queued turn each."""
        while True:
            await sleep_for(
                seconds_until(self.config.emoji_describe_hour, self.config.consolidate_timezone)
            )
            try:
                if not await _quota_ok(self.config):
                    continue
                stats = await describe_pending(
                    self.emoji,
                    self._describe_emoji,
                    self._emoji_image,
                    self.config.emoji_describe_max,
                    f"bot:{self.config.codex_model}",
                    self.config.allowed_guild_ids,
                )
                LOGGER.info("Emoji descriptions: %s", stats)
            except asyncio.CancelledError:
                raise
            except Exception:
                LOGGER.exception("Emoji descriptions failed")

    def _start_emoji(self) -> None:
        """On every ready: the guilds' emoji as they are now, and the backfill unless one is
        running (it goes on from where the last one stopped)."""
        if self.emoji is None:
            return
        for guild in self.guilds:
            self._sync_emoji(guild)
        task = getattr(self, "_emoji_backfill_task", None)
        if self.config.emoji_backfill_days > 0 and (task is None or task.done()):
            self._emoji_backfill_task = asyncio.create_task(self._backfill_emoji_safely())

    async def on_ready(self) -> None:
        LOGGER.info("Discord bot ready as %s", self.user)
        await self.warm_emojis()
        self._start_emoji()
        LOGGER.info("%s", await codex_login_status(self.config))
        if grok.enabled(self.config):  # the model list /model offers and effort mapping uses
            # never hold up startup on the sidecar; kept so the task is not garbage-collected
            # on_ready fires again on every re-identify: one loader at a time.
            task = getattr(self, "_grok_models_task", None)
            if task is None or task.done():
                self._grok_models_task = asyncio.create_task(self._load_grok_models())
        if self.config.default_model.startswith(f"{GROK}:") and not self.config.model_chain:
            LOGGER.warning(
                "DEFAULT_MODEL is Grok but MODEL_CHAIN is empty: turns Grok cannot take (GIF/WebP "
                "images, past the weekly reserve, sidecar down) will fail instead of falling back"
            )
        LOGGER.info("Alerts go to user %s", await self.alerts.resolve_owner() or "(none)")
        if not getattr(self, "_login_watch", None):
            self._login_watch = asyncio.create_task(
                login_watch(
                    self.alerts,
                    self.config,
                    codex_login_status,
                    self.config.alert_login_check_minutes * 60,
                )
            )
        if not getattr(self, "_announce_task", None):
            # The owner-approved queue (announce.py): posts each item at most once; nothing is
            # posted just because the Bot started.
            self._announce_task = asyncio.create_task(
                announce_loop(self, self.config, self.alerts, self._started_at)
            )

    async def _tracking_forever(self) -> None:
        await self.wait_until_ready()
        if self.tracker is None:
            return
        await tracking_loop(
            self.tracker,
            self._fetch_tracking_source,
            self._classify_tracking,
            self._deliver_tracking,
            self.config.tracking_interval_seconds,
            self.config.tracking_keep_days,
        )

    async def _read_page(self, url: str) -> tuple[str, bool]:
        """(one page as text with its links kept inline, whether it was read at all). No out_dir:
        a background poll has nobody to show a screenshot to, and nothing to clean up afterwards."""
        text, _shots = await fetch_or_render(url, self.config, None, False)
        return text, not unreadable(text)

    async def _fetch_tracking_source(self, source: Source):
        fetcher = {
            "youtube": self.youtube_tracker,
            "twitch": self.twitch_tracker,
            "x": self.x_tracker,
            "ruten": self.ruten_tracker,
            "web": self.web_tracker,
        }.get(source.provider)
        if fetcher is None:
            raise ProviderError(f"unsupported provider: {source.provider}")
        if source.provider != "ruten":
            return await fetcher.fetch(source)
        # Ruten is read through an undocumented API: a change there must reach the operator,
        # not just the log, or a broken source looks exactly like a quiet store. Counted per
        # source: one shared streak was reset by any healthy store.
        alert_key = f"露天追蹤 {source.external_id}"
        try:
            result = await fetcher.fetch(source)
        except (ProviderError, aiohttp.ClientError, TimeoutError) as error:
            await self.alerts.record_failure(alert_key, str(error) or type(error).__name__)
            raise
        await self.alerts.record_success(alert_key)
        return result

    async def _classify_tracking(self, prompt: str) -> str:
        if self.tracker is None:
            raise RuntimeError("tracking is disabled")

        async def classify() -> str:
            gate = self.config.tracking_min_remaining_percent
            if gate > 0:
                # Only worth probing when a gate is actually set: with a spare backend a spent
                # subscription is survivable, so "quota unknown" no longer has to stop the pass.
                limits = await probe_rate_limits(self.config)
                if limits is None:
                    raise RuntimeError("Codex usage is unknown; tracking classification deferred")
                remaining = min(
                    100.0 - limits.primary_used_percent,
                    100.0 - limits.secondary_used_percent,
                )
                if remaining < gate:
                    raise RuntimeError("Codex remaining quota is below the tracking gate")
            return await run_batch(
                prompt,
                self.config,
                schema=self.config.tracking_schema_path,
                effort=self.config.tracking_reasoning_effort,
                isolated=True,
            )

        return await self.queue.run(classify)

    async def _deliver_tracking(self, message: OutboxMessage) -> None:
        channel = self.get_channel(message.watch.channel_id) or await self.fetch_channel(
            message.watch.channel_id
        )
        channel_guild = getattr(getattr(channel, "guild", None), "id", None)
        reason = self._access(message.watch.guild_id, channel, message.watch.channel_id)
        if channel_guild != message.watch.guild_id or reason:
            raise RuntimeError("tracking destination is outside the configured allowlist")
        mentioned = [message.watch.user_id, *message.watch.mention_ids]
        allowed_users = [discord.Object(id=uid) for uid in mentioned if uid] or False
        source = self.tracker.get_source(message.watch.source_id) if self.tracker else None
        label = ""
        if source is not None:
            label = str(
                source.state.get("title") or source.state.get("login") or source.external_id
            )
        await channel.send(
            tracking_message(message, label),
            allowed_mentions=discord.AllowedMentions(
                users=allowed_users, everyone=False, roles=False, replied_user=False
            ),
        )

    # ----- shared pipeline -------------------------------------------------------------------

    def _access(self, guild_id: int | None, channel: object, channel_id: int | None) -> str:
        """Empty string when allowed, otherwise the user-facing rejection reason."""
        parent_id = getattr(channel, "parent_id", None)
        decision = check_access(
            guild_id=guild_id,
            channel_id=channel_id,
            parent_channel_id=parent_id,
            config=self.config,
        )
        if not decision.allowed:
            # Every command funnels through here, so this is the only place a refusal can be
            # recorded. Without it a mistyped allowlist turns the Bot off for a whole guild and
            # leaves nothing in the host log to find it by.
            LOGGER.info(
                "Access refused guild=%s channel=%s parent=%s: %s",
                guild_id,
                channel_id,
                parent_id,
                decision.reason,
            )
        return "" if decision.allowed else decision.reason

    def _validate(self, prompt: str, attachments: Sequence[discord.Attachment]) -> str:
        """Empty string when the request is acceptable, otherwise the rejection reason."""
        if not prompt or len(prompt) > self.config.max_prompt_chars:
            return f"prompt 必須介於 1 到 {self.config.max_prompt_chars} 個字元。"
        if len(attachments) > self.config.max_attachments:
            return f"一次最多 {self.config.max_attachments} 個附件。"
        for attachment in attachments:
            kind, detail = validate_attachment(
                attachment.content_type, attachment.filename, attachment.size, self.config
            )
            if not kind:
                return detail
        return ""

    def _command_rows(self) -> list[tuple[str, str, list[str]]]:
        return [
            # display_name: what Discord shows (app_commands.rename), which HELP must match
            (c.name, c.description, [p.display_name for p in c.parameters])
            for c in sorted(self.tree.get_commands(), key=lambda c: c.name)
        ]

    def help_sheet(self) -> str:
        """What this Bot can do, generated from the registered commands so it never drifts from
        the code; injected as HELP so the model can explain itself truthfully."""
        sheet = render_sheet(
            self.config.command_prefix,
            self._command_rows(),
            self._default_label(),
            image_order=self._image_order(),
            emoji=self.emoji is not None,
            emoji_reply=self.config.emoji_reply,  # read only with emoji on (_features)
            image=self.images.enabled,
        )
        doc = apis.render_doc(self.apis)
        return f"{sheet}\n{doc}" if doc else sheet

    def help_guide(self) -> str:
        """The detailed member guide behind /<prefix>-help (same source as the model's sheet)."""
        return render_guide(
            self.config.command_prefix,
            [row[0] for row in self._command_rows()],
            self._default_label(),
            image_order=self._image_order(),
            emoji=self.emoji is not None,
            emoji_reply=self.config.emoji_reply,  # read only with emoji on (_features)
            image=self.images.enabled,
        )

    def _image_order(self) -> str:
        """The routing table's image list as members read it ("Codex、Grok、Gemini 3.8 Flash");
        "" while routing is off. From the table, so help never drifts from what runs."""
        if self.routing is None:
            return ""
        names = []
        for entry in self.routing.image:
            choice = routing.choice_of(entry, self.config.codex_model)
            name = AGY_FAMILIES[choice.family][0].split("（")[0] if choice.backend == AGY else (
                BACKEND_LABELS.get(choice.backend, choice.backend)
            )  # fmt: skip
            names.append(name)
        return "、".join(names)

    def _default_label(self) -> str:
        """DEFAULT_MODEL as members read it (\"Grok · Grok 4.7\"), for the help texts."""
        return self._choice(self.config.default_model).label

    async def _understand_videos(
        self,
        urls: list[str],
        out_dir: Path,
        on_video_slow: Callable[[], Awaitable[None]] | None,
    ) -> str:
        """Describe the videos among `urls` (YouTube, X clips) as untrusted background blocks.
        The work races a timer: a clip that takes longer than VIDEO_INTERIM_AFTER_SECONDS fires
        on_video_slow once (the caller shows a "still watching" notice) and then finishes."""
        targets = [u for u in urls if has_video(u)][: self.config.link_max_urls]
        if not targets or not gemini.available(self.config):
            return ""

        async def describe() -> list[str]:
            done = await asyncio.gather(
                *(
                    understand_video(u, self.config, out_dir / f"vid{i}")
                    for i, u in enumerate(targets)
                )
            )
            return [
                f'<VIDEO url="{u}">\n{d}\n</VIDEO>' for u, d in zip(targets, done, strict=True) if d
            ]

        task = asyncio.create_task(describe())
        try:
            try:
                blocks = await asyncio.wait_for(
                    asyncio.shield(task), self.config.video_interim_after_seconds
                )
            except TimeoutError:
                if on_video_slow is not None:
                    await on_video_slow()
                blocks = await task
        except asyncio.CancelledError:
            task.cancel()  # the shield is against the timer only, not against the member's ❌
            raise
        return "\n\n".join(blocks)

    async def _answer(
        self,
        prompt: str,
        attachments: Sequence[discord.Attachment],
        guild_id: int | None,
        user_id: int,
        effort: str = "",
        resume: str = "",
        previews: dict[str, Preview] | None = None,
        on_video_slow: Callable[[], Awaitable[None]] | None = None,
        on_delta: Callable[[str], Awaitable[None]] | None = None,
        channel_id: int | None = None,
        routed: Resolved | None = None,
        routed_spares: Sequence[Resolved] = (),
        history: str = "",
        spare_history: str = "",
    ) -> CodexResult:
        """Run one validated request through the member's backend; always returns text.
        `on_delta` receives the accumulated answer while a backend streams it (agy, routers).
        `routed` is a target the Bot picked for a member who chose no model: it replaces the
        member's backend and falls back along the whole chain (see `fallback_chain`), after
        the router's own stand-ins in `routed_spares`. `history` is the conversation so far,
        replayed to a backend that did not hold it; `spare_history` is what a spare taking over
        from the routed target gets instead of the target's thread. The result's `via` is what
        answered."""
        if self.emoji is not None and guild_id is not None:
            # `<:name:id>` as the model can read it; never the emoji's image, which would make
            # this an image turn (emoji.py).
            prompt = self.emoji.rewrite(prompt, guild_id)
        emoji_out = self._emoji_reply(guild_id)
        if emoji_out is not None and on_delta is not None:
            painted = on_delta

            async def on_delta(text: str) -> None:
                # Each part-way answer too: a member never sees `:name:` turn into the emoji.
                await painted(emoji_out(text, True))

        stored = self._stored(guild_id, user_id)
        choice = self._choice(stored)
        target = resolve(
            choice, effort or split_stored(stored)[1] or self.config.codex_reasoning_effort
        )
        if routed is not None:
            target = routed
            choice = ModelChoice("", "", routed.backend, routed.model)  # only its backend counts
            if resume and self.threads.backend_of(resume) != routed.backend:
                # A thread id belongs to the backend that created it: the router moving a
                # conversation to another backend (or an id the store cannot place) starts over.
                resume = ""
        spares = fallback_chain(
            choice,
            self.config.model_chain,
            self.config.codex_model,
            self.config.codex_reasoning_effort,
            self.config.codex_fallback_model,
            routed=routed is not None,
        )
        if (
            target.backend == GROK
            and grok.cached_models()
            and grok.cached_model(target.model) is None
        ):
            # A stored Grok model the plan no longer offers: the default Grok model, not an error
            # on every message until the member notices and runs /model.
            fallback = self._choice(self.config.default_model)
            if fallback.backend == GROK and grok.cached_model(fallback.family) is not None:
                target = resolve(fallback, target.effort)
        if routed is not None:
            ahead = [*routed_spares, *spares]
            spares = [s for i, s in enumerate(ahead) if s != target and s not in ahead[:i]]
        fell_back = False
        # A routed Grok turn was gated when it was routed (an X question may use the whole
        # weekly quota; anything else got its stand-in there), so only a member's own is here.
        if routed is None and target.backend == GROK and spares and not await self._grok_usable():
            # Straight to the next backend: the reserve for X lookups, or the breaker. Still a
            # fallback: the gk- id is not resumed there, and the spare's thread id is not kept
            # under the Grok model (Grok would later resume a foreign id and start over).
            LOGGER.info("Skipping Grok for this turn; answering with %s", spares[0].model)
            target, spares, fell_back = spares[0], spares[1:], True
        if routed is None:
            # A member's own model never falls *into* Grok: the weekly quota it would spend is
            # shared with X lookups. A routed turn may (X lookups may use the whole weekly
            # quota, so the reserve is not absolute), but only past the same `_grok_usable`
            # gate the Grok default turn goes through (`next_spare`).
            spares = [spare for spare in spares if spare.backend != GROK]
        drop_resume = fell_back  # only the caller's id is foreign; later recall rounds chain
        first: tuple[str, dict] | None = None  # the question as first asked, for a spare
        results: list[str] = []  # every recall round's results so far, oldest first
        result_images: list[Path] = []  # and the screenshots / sandbox images they came with

        async def next_spare():
            nonlocal spares
            while spares:
                spare, spares = spares[0], spares[1:]
                if spare.backend != GROK or await self._grok_usable():
                    return spare
                LOGGER.info("Skipping Grok as a spare for this turn")
            return None

        async def run_on(via, text: str, **kw) -> CodexResult:
            """One backend call. Every backend but the local model runs in the SerialQueue; a
            local turn may wait a long time in the host's GPU queue behind image jobs, and
            holding the SerialQueue meanwhile would stop every member's answer on every model.
            That GPU queue serializes local turns instead; `_thread_turn` keeps two turns of one
            conversation apart, as the SerialQueue did, so neither loses the other's exchange
            from its transcript."""
            if via.backend == LOCAL:
                async with self._thread_turn(kw.get("resume") or ""):
                    return await call_backend(via, text, **kw)
            return await self.queue.run(lambda: call_backend(via, text, **kw))

        async def call_backend(via, text: str, **kw) -> CodexResult:
            if via.backend == GROK:
                return await grok.run_grok(text, self.config, via.model, effort=via.effort, **kw)
            if via.backend == AGY:
                kw.pop("effort", None)
                # A timeout, an empty reply or a model out of quota is AgyUnavailable and falls
                # back; broken settings stay a plain error and reach the failure alert.
                # A pool under its reserve is skipped here for every turn, a member's own model
                # and MODEL_CHAIN's agy entries included, not only automatic choice.
                agy_module.check_pool(via.model, self.agy_quota)
                try:
                    return await run_agy(text, self.config, via.model, **kw)
                except AgyUnavailable as error:
                    agy_module.note_spent(via.model, error, self.agy_quota)
                    raise
            if via.backend in ROUTER_BACKENDS:
                catalog = self.catalogs[via.backend]
                await catalog.free_models()  # image / effort capability lookup
                return await run_router(
                    ROUTERS[via.backend],
                    text,
                    self.config,
                    via.model,
                    effort=via.effort,
                    catalog=catalog,
                    **kw,
                )
            return await run_codex(text, self.config, effort=via.effort, **kw)

        async def turn(text: str, **kw) -> CodexResult:
            nonlocal target, fell_back, spares, drop_resume, first
            kw.setdefault("on_delta", on_delta)
            if kw.get("raw"):
                results.append(text.removesuffix(ANSWER_NOW))
                result_images.extend(kw.get("images", ()))
            else:
                first = (text, dict(kw))
            if drop_resume:
                kw.pop("resume", None)  # a thread id means nothing to another backend
                drop_resume = False
            while True:
                try:
                    return await run_on(target, text, **kw)
                except BackendUnavailable as unavailable:
                    spare = await next_spare()
                    if spare is None:
                        raise
                    # Answer on the next backend for the rest of this request. Switching
                    # mid-request keeps the recall loop's resume ids on one backend, since a
                    # thread id means nothing to another backend.
                    LOGGER.warning(
                        "%s unavailable (%s: %s); answering with %s",
                        target.backend,
                        type(unavailable).__name__,
                        unavailable,
                        spare.model,
                    )
                    failed = target.backend
                    target, fell_back = spare, True
                    self._last_fallback = Fallback(time.time(), unavailable, failed, target)
                    if isinstance(unavailable, CodexUnauthorized):
                        # Quota comes back by itself; a lost login does not. Tell the operator
                        # now rather than let the spare hide it until the next login check.
                        await self.alerts.login_lost("Codex", str(unavailable))
                    elif isinstance(unavailable, grok.GrokLogin):
                        await self.alerts.login_lost("Grok", str(unavailable))
                    elif isinstance(unavailable, grok.GrokRefused):
                        await self._grok_refused(unavailable)
                    if kw.get("raw") and first is not None:
                        # A recall round is only results and "now answer" for the thread that
                        # asked; the spare holds none of it. Ask it the whole question again,
                        # with every result read so far.
                        text, asked = first
                        kw = {
                            **asked,
                            "on_delta": kw["on_delta"],
                            "images": [*asked.get("images", ()), *result_images],
                            "links": "\n\n".join(b for b in (asked.get("links"), *results) if b),
                        }
                        kw.pop("resume", None)
                        if spare_history:
                            kw.setdefault("history", spare_history)
                    elif kw.pop("resume", None) and spare_history and not kw.get("raw"):
                        kw.setdefault("history", spare_history)  # the thread stays behind

        images: list[Path] = []
        self.config.attachment_dir.mkdir(parents=True, exist_ok=True)
        link_dir = request_dir(self.config)
        deliver_dir = request_dir(self.config)
        delivered: list[Path] = []  # sandbox-made files handed to the member with the answer
        handed_over = False  # deliver_dir became the answer's generated_dir: the caller removes it
        try:
            documents: list[str] = []
            for attachment in attachments:
                kind, suffix = validate_attachment(
                    attachment.content_type, attachment.filename, attachment.size, self.config
                )
                if kind == "image":
                    images.append(await download_attachment(attachment, suffix, self.config))
                elif kind == "document":
                    saved = await download_attachment(attachment, suffix, self.config, "doc")
                    try:
                        text = await asyncio.to_thread(
                            extract_text, saved, self.config.link_max_chars
                        )
                    finally:
                        remove_request_dir(saved)
                    documents.append(f'<FILE name="{attachment.filename}">\n{text}\n</FILE>')
            files = "\n\n".join(documents)
            permanent = self.permanent.index_text()
            pending = render_pending(self.reminders.for_user(user_id))
            tracked = self._tracked_lines(user_id)
            memory = "\n\n".join(
                section
                for section in (
                    f"[永久記憶索引]\n{permanent}" if permanent else "",
                    self.memory.render(guild_id, user_id),
                    self.emoji.block(guild_id, emoji_out is not None)
                    if self.emoji is not None
                    else "",
                    f"[待辦提醒]\n{pending}" if pending else "",
                    f"[社群追蹤]\n{tracked}" if tracked else "",
                )
                if section
            )
            style = self.memory.get_style(guild_id, user_id)
            persona_off = self.memory.get_persona_off(guild_id, user_id)
            found = find_urls(prompt, self.config.link_max_urls)
            links, shots = await link_blocks(found, self.config, link_dir, previews)
            images.extend(shots)
            video = await self._understand_videos(found, link_dir, on_video_slow)
            links = "\n\n".join(block for block in (links, video) if block)
            replay = {"history": history} if history else {}
            result = await turn(
                prompt,
                **replay,
                images=images,
                resume=resume,
                memory=memory,
                personal_style=style,
                plain=persona_off,
                links=links,
                help=self.help_sheet(),
                files=files,
                speaker=user_id,  # shared reply threads: who said it (harvest, digest)
            )
            # On-demand reads (search snippets / paged recall): the Bot executes the request and
            # feeds the result back into the same thread. Bounded by MEMORY_RECALL_ROUNDS.
            # Registered-API answers are kept for the whole turn: a model that re-sends the same
            # query (one sent an identical Leaguepedia query three times) must not spend the
            # source's quota again to be told the same thing.
            api_seen: dict[tuple[str, str], str] = {}
            for _ in range(self.config.memory_recall_rounds):
                if not request_only(result.text):
                    break  # an answer that merely quotes a tag (e.g. from a page) is an answer
                wanted = extract_read_requests(result.text)
                urls = extract_fetch_tags(result.text)[: self.config.link_max_urls]
                queries = search.extract_web_queries(result.text)[:WEB_PER_ROUND]
                runs = sandbox.extract_runs(result.text) if sandbox.available(self.config) else []
                api_calls = apis.extract_api_calls(result.text) if self.apis else []
                unserved = unserved_reads(result.text, self.config, bool(self.apis))
                if not (wanted or urls or queries or runs or api_calls or unserved):
                    break
                blocks = [
                    f'<RESULT kind="{kind}" scope="{scope}" target="{target}">\n'
                    + self._read(kind, scope, guild_id, user_id, target, offset, lines)
                    + "\n</RESULT>"
                    for kind, scope, target, offset, lines in wanted
                ]
                if unserved:
                    LOGGER.warning(
                        "Unserved reads guild=%s user=%s: %s",
                        guild_id,
                        user_id,
                        " | ".join(p[:80] for p in unserved),
                    )
                    blocks.append('<RESULT kind="error">\n' + "\n".join(unserved) + "\n</RESULT>")
                for query in queries:
                    provider, hits = await search.search_web(query, self.config)
                    blocks.append(search.render_results(query, provider, hits))
                for name, path in api_calls:
                    body = api_seen.get((name, path))
                    if body is None:
                        body = await apis.call_api(name, path, self.apis, self.config)
                        api_seen[(name, path)] = body
                    else:
                        body = f"（與稍早完全相同的查詢，沿用當時的結果）\n{body}"
                    blocks.append(apis.render_result(name, path, body))
                extra: list[Path] = []
                for i, (lang, code) in enumerate(runs):
                    try:
                        ran = await sandbox.run_code(lang, code, self.config, link_dir / f"run{i}")
                    except (aiohttp.ClientError, TimeoutError, ValueError) as error:
                        ran = sandbox.RunResult(
                            -1, False, "", f"沙盒無法使用：{type(error).__name__}"
                        )
                    blocks.append(sandbox.render_result(lang, ran))
                    for produced in ran.files:
                        delivered.append(self._keep_for_member(produced, deliver_dir))
                        if produced.suffix.lower() in (".png", ".jpg", ".jpeg", ".webp", ".gif"):
                            extra.append(produced)
                for i, (url, render) in enumerate(urls):
                    fetched, shots = await fetch_or_render(
                        url, self.config, link_dir / f"fetch{i}", render
                    )
                    blocks.append(f'<LINK url="{url}">\n{fetched}\n</LINK>')
                    extra.extend(shots)
                recalled = "\n\n".join(blocks)
                recalled = defang(recalled)  # results are not the member: no USER_MESSAGE in them
                result = await turn(
                    recalled + ANSWER_NOW,
                    images=tuple(extra),
                    resume=result.thread_id,
                    raw=True,
                    personal_style=style,
                    plain=persona_off,
                )
            else:
                if request_only(result.text):
                    LOGGER.warning("Read rounds spent guild=%s user=%s", guild_id, user_id)
                    result = await turn(
                        READS_SPENT + ANSWER_NOW,
                        resume=result.thread_id,
                        raw=True,
                        personal_style=style,
                        plain=persona_off,
                    )
                    if request_only(result.text):
                        result = replace(result, text=READS_SPENT_MESSAGE)
            own = split_quoted(prompt)[1]  # a replied-to message is not the member asking
            text, unread, refused = await self._apply_action_tags(
                result.text, guild_id, channel_id, user_id, own, result.thread_id
            )
            if unread or refused:
                LOGGER.warning(
                    "Unread or refused tags guild=%s channel=%s user=%s tags=%s",
                    guild_id,
                    channel_id,
                    user_id,
                    " | ".join(p[:80] for p in unread + refused),
                )
                errors = fence_errors(defang("\n".join(unread + refused)))
                try:
                    # Straight to the backend that answered, never through `turn`: a spare holds
                    # none of the thread and would be asked the whole question again, and every
                    # action already carried out would come back in its answer.
                    retry = await run_on(
                        target,
                        TAG_RETRY.format(errors=errors),
                        resume=result.thread_id,
                        raw=True,
                        personal_style=style,
                        plain=persona_off,
                    )
                except Exception:  # the answer itself is fine; the member is told below
                    LOGGER.exception("Tag retry failed guild=%s", guild_id)
                    retry = None
                resent: list[str] = []
                if retry is not None:
                    if retry.generated_dir not in (None, result.generated_dir):
                        remove_dir(retry.generated_dir)
                    # A retry that could not resume started a thread holding nothing but the
                    # correction; the conversation goes on from the original one.
                    if retry.resumed and retry.thread_id:
                        result = replace(result, thread_id=retry.thread_id)
                    # Only what is new: a tag already carried out, sent back word for word, is
                    # not done twice (a memory note has no duplicate check of its own).
                    seen = {_spaced(t) for t in action_tags(result.text)}
                    resent = []
                    for tag in action_tags(retry.text):  # nor twice within the retry itself
                        if _spaced(tag) not in seen:
                            seen.add(_spaced(tag))
                            resent.append(tag)
                    done, _, _ = await self._apply_action_tags(
                        "\n".join(resent), guild_id, channel_id, user_id, own, result.thread_id
                    )
                    if done:
                        text = f"{text}\n\n{done}"
                # A refusal was already told to the member in its own words; this is for the
                # tags the Bot could not even read.
                if unread and unread_left(unread, refused, resent):
                    text += (
                        "\n\n（⚠️ 上面說的操作有的其實沒有執行——我用了 Bot 看不懂的指令。"
                        "請直接用斜線指令，或再說一次。）"
                    )
            if emoji_out is not None:
                text = emoji_out(text, False)  # before any cut: `<:name:id>` is the longer one
            await self.alerts.record_success(target.backend)
            generated_dir = result.generated_dir
            outgoing = tuple(result.images)  # not `images`: that list is cleaned up in finally
            if delivered:
                if generated_dir is None:
                    generated_dir, handed_over = deliver_dir, True  # removed after sending
                else:
                    delivered = [self._keep_for_member(path, generated_dir) for path in delivered]
                outgoing += tuple(delivered)
            # A fallback is not announced in the answer: the member gains nothing from it, and
            # /status shows the last one. The thread id is dropped so nothing tries to resume it
            # back on Codex.
            return CodexResult(
                truncate(text, self.config.max_response_chars),
                outgoing,
                generated_dir,
                # A routed turn keeps its spare's thread: the caller records it under the spare
                # that made it (`_routed_memo`), and the conversation goes on from there.
                "" if fell_back and routed is None else result.thread_id,
                result.resumed and not (fell_back and routed is None),
                via=target,
            )
        except QueueFullError:
            return CodexResult(QUEUE_FULL_MESSAGE)
        except QueueBusy as busy:  # the host's GPU queue: busy, not broken, so no failure alert
            LOGGER.info("Local queue busy guild=%s: %s", guild_id, busy)
            return CodexResult(str(busy))
        except Exception as error:
            LOGGER.exception("Request failed backend=%s guild=%s", target.backend, guild_id)
            await self.alerts.record_failure(target.backend, str(error))
            prefix = self.config.command_prefix
            return CodexResult(failure_text(target.backend, error, prefix))
        finally:
            for path in images:
                remove_request_dir(path)
            remove_dir(link_dir)
            if not handed_over:  # the sweep skips a live directory, so every path releases it
                remove_dir(deliver_dir)

    @contextlib.asynccontextmanager
    async def _thread_turn(self, thread_id: str):
        """Hold `thread_id` for one turn: a second turn on the same conversation waits until the
        first has saved its transcript. A new conversation ("") holds nothing."""
        if not thread_id:
            yield
            return
        entry = self._thread_turns.setdefault(thread_id, [asyncio.Lock(), 0])
        entry[1] += 1
        try:
            async with entry[0]:
                yield
        finally:
            entry[1] -= 1
            if not entry[1]:
                self._thread_turns.pop(thread_id, None)

    def _emoji_reply(self, guild_id: int | None) -> Callable[[str, bool], str] | None:
        """How an answer's `:name:` becomes the emoji (EMOJI_REPLY), or None: off, a DM, or a
        guild off the allowlist."""
        store = self.emoji
        if (
            store is None
            or not self.config.emoji_reply
            or guild_id is None
            or guild_id not in self.config.allowed_guild_ids
        ):
            return None
        # Part-way versions share one lookup; the answer itself reads the server again, so an
        # emoji deleted while it was being written is not sent.
        known = store.by_name(guild_id)
        return lambda text, streaming: store.reply(
            text, guild_id, streaming, known if streaming else None
        )

    def _read(
        self,
        kind: str,
        scope: str,
        guild_id: int | None,
        user_id: int,
        target: str,
        offset: int,
        lines: int | None,
    ) -> str:
        if scope == "permanent":
            if kind == "search":
                return self.permanent.search(target)
            return self.permanent.recall(target, offset, lines)
        if kind == "search":
            return self.memory.search(scope, guild_id, user_id, target)
        return self.memory.recall(scope, guild_id, user_id, target, offset, lines)

    async def _linkclean(self, message: discord.Message) -> None:
        """Strip tracking params from the links a member just shared, per the guild mode.
        `all`: a links-only message is replaced (original deleted, clean links reposted with
        the author @'d); anything else keeps its text and only the changed links are appended
        below; a links-only message the Bot cannot delete degrades to the append. `links`:
        only the replacement, never an append -- a message with text, or one the Bot cannot
        replace, is left exactly as posted. `off`: nothing. The caller contains failures so
        mention handling continues."""
        guild = message.guild
        if guild is None:
            return
        # Same access rules as every other surface, checked directly (not via _access) because
        # _access logs a refusal per message and this one runs on all of them.
        if not check_access(
            guild_id=guild.id,
            channel_id=message.channel.id,
            parent_channel_id=getattr(message.channel, "parent_id", None),
            config=self.config,
        ).allowed:
            return
        mode = self.linkclean.mode(guild.id)
        if mode == "off":
            return
        raw = find_urls(message.content, MAX_URLS, clean=False)
        clean = [strip_tracking(url) for url in raw]
        spoil = [False] * len(raw)
        if self.linkclean.embedfix(guild.id):
            clean, spoil = await self._embedfix(clean)
        outcome = plan(message.content, raw, clean)
        if outcome is None:
            return
        _, _, links_only = outcome
        if mode == "links" and not links_only:
            return
        member = guild.me
        replacement = message.content
        for original, cleaned, spoiler in zip(raw, clean, spoil, strict=True):
            # The member's own ||bars|| stay in the text; a rating-forced spoiler adds them.
            wrapped = deliver(cleaned, spoiler and not spoilered(message.content, original))
            replacement = replacement.replace(original, wrapped)
        replacement = f"<@{message.author.id}>\n{replacement}"
        reply_to = _reply_reference(message)
        can_replace = (
            links_only
            and member is not None
            and message.channel.permissions_for(member).manage_messages
            and not message.attachments
            and not message.stickers
            and (message.reference is None or reply_to is not None)
            and message.thread is None
            and len(replacement) <= 2000
        )
        if can_replace:
            # Never remove the only copy: delivery must succeed before deletion.
            posted = await message.channel.send(
                replacement,
                allowed_mentions=discord.AllowedMentions(
                    users=[message.author], everyone=False, roles=False, replied_user=True
                ),
                **({"reference": reply_to} if reply_to is not None else {}),
            )
            self._remember_repost(posted)
            await message.delete()
        elif mode == "links":
            return  # nothing to replace means nothing to do: this mode never appends
        else:
            # Individual URLs keep each send within Discord's limit. Oversized URLs remain
            # in the untouched original instead of being truncated into broken links.
            for original, cleaned, spoiler in zip(raw, clean, spoil, strict=True):
                if cleaned == original:
                    continue
                text = deliver(cleaned, spoiler or spoilered(message.content, original))
                if len(text) <= 2000:
                    posted = await message.channel.send(
                        text, allowed_mentions=discord.AllowedMentions.none()
                    )
                    self._remember_repost(posted)

    def _remember_repost(self, posted: object) -> None:
        message_id = getattr(posted, "id", None)
        if isinstance(message_id, int):
            self.linkclean.remember_repost(message_id)

    async def _replies_to_repost(self, message: discord.Message) -> bool:
        """Whether `message` replies to one of the Bot's cleaned-link reposts: by id for those
        posted since the table existed, else by shape (the Bot's own message that is nothing
        but an optional leading @author line and links) for the ones posted before."""
        replied_to = message.reference.message_id if message.reference else None
        if replied_to is None:
            return False
        if self.linkclean.is_repost(replied_to):
            return True
        quoted = await self._referenced(message)
        if quoted is None or quoted.author != self.user:
            return False
        body = re.sub(r"^<@!?\d+>\n", "", quoted.content or "")
        urls = find_urls(body, MAX_URLS, clean=False)
        return bool(urls) and is_link_only(body, urls)

    async def _embedfix(self, urls: list[str]) -> tuple[list[str], list[bool]]:
        """Each link in its embed-fixer proxy form when a proxy page verifiably carries
        media for Discord's crawler (embedfix.pick), else as given; plus, per link, whether
        it must be delivered spoilered (an age-restricted work)."""
        if not any(embedfix.candidates(url) for url in urls):
            return urls, [False] * len(urls)
        async with _guarded_session(self.config) as session:

            async def fetch(url: str, headers: dict[str, str]) -> str | None:
                return await embedfix.fetch_text(session, url, headers)

            fixes = [await embedfix.pick(url, fetch) for url in urls]
        return (
            [fix.url if fix else url for fix, url in zip(fixes, urls, strict=True)],
            [bool(fix and fix.spoiler) for fix in fixes],
        )

    async def _previews(
        self, message: discord.Message, quoted: discord.Message | None
    ) -> dict[str, Preview]:
        """Link previews Discord attached to the request and the quoted message. Discord adds
        embeds a moment after the message arrives, so a message with links but no embeds yet is
        re-fetched once after a short wait."""
        if not message.embeds and find_urls(message.content, 1):
            await asyncio.sleep(self.config.link_preview_wait_seconds)
            try:
                message = await message.channel.fetch_message(message.id)
            except discord.HTTPException:
                pass
        return previews_from([m for m in (message, quoted) if m is not None])

    @staticmethod
    async def _referenced(message: discord.Message) -> discord.Message | None:
        """The message this one replies to, fetched if Discord did not resolve it inline."""
        if message.reference is None or message.reference.message_id is None:
            return None
        resolved = message.reference.resolved
        if isinstance(resolved, discord.Message):
            return resolved
        try:
            return await message.channel.fetch_message(message.reference.message_id)
        except discord.HTTPException:
            return None

    def _effort_label(self, target) -> str:
        """What the member sees as the effort: the level applied, or why there is none."""
        if target.backend in ROUTER_BACKENDS:
            info = self.catalogs[target.backend].get(target.model)
            if info is not None and not info.reasoning:
                return "無"
        return REASONING_EFFORTS.get(target.effort, target.effort) if target.effort else "固定"

    def _describe(self, value: str, level: str) -> str:
        chosen = self._choice(value)
        target = resolve(chosen, level or self.config.codex_reasoning_effort)
        text = f"{chosen.label} · {self._effort_label(target)} → `{target.model}`"
        if chosen.backend in ROUTER_BACKENDS:
            info = self.catalogs[chosen.backend].get(chosen.family)
            sees = "看得到" if info is None or info.image else "看不到"
            if ROUTERS[chosen.backend].free_tier:
                text += f"（免費，{sees}圖片）\n{FREE_MODEL_NOTE}"
            else:
                text += f"（{sees}圖片）"
        return text

    def _own_model(self, guild_id: int | None, user_id: int) -> str:
        """The member's own /model choice; "" for none, and for one agy no longer offers (it
        would otherwise mean DEFAULT_MODEL in silence and keep them out of routing)."""
        own = self.memory.get_model(guild_id, user_id)
        return "" if own and routing.retired(own) else own

    def _stored(self, guild_id: int | None, user_id: int) -> str:
        """The member's stored "<backend>:<model>|<effort>", or DEFAULT_MODEL when they have
        none: what a member who never ran /model gets is a setting, not a code path."""
        return self._own_model(guild_id, user_id) or self.config.default_model

    def _choice(self, stored: str):
        return parse_choice(stored, self.config.codex_model, self.config.default_model)

    def _model(self, guild_id: int | None, user_id: int) -> str:
        return self._choice(self._stored(guild_id, user_id)).value

    def _member_effort(self, guild_id: int | None, user_id: int) -> str:
        stored = self._stored(guild_id, user_id)
        return split_stored(stored)[1] or self.config.codex_reasoning_effort

    async def _load_grok_models(self, attempts: int = 30, wait: float = 60.0) -> None:
        """Load the catalog; after a reboot the sidecar may still be starting, and an empty
        catalog leaves /status and /help showing model ids instead of names, so retry."""
        for attempt in range(attempts):
            catalog = await grok.models(self.config)
            if catalog or attempt == attempts - 1:
                break
            await asyncio.sleep(wait)
        LOGGER.info("Grok models: %s", ", ".join(m.id for m in catalog) or "(unavailable)")

    async def _read_agy_usage_forever(self) -> None:
        """agy's own /usage every AGY_USAGE_MINUTES, for the reserves (quota.AgyQuota). It spends
        nothing; a failed read keeps the last reading."""
        while True:
            try:
                reading = await read_usage(self.config)
                if reading is not None:
                    self.agy_quota.update(reading)
                    LOGGER.info("agy usage: %s", self.agy_quota.summary())
            except Exception:  # one bad read must not end the loop and freeze the last one
                LOGGER.exception("agy usage read failed")
            await asyncio.sleep(self.config.agy_usage_minutes * 60)

    async def _grok_usable(self) -> bool:
        """Whether a turn may go to Grok right now. Not past the weekly share reserved for X
        lookups, and not while the circuit breaker is open after repeated refused sessions.
        (Images it cannot take — GIF, WebP, too many — fall back from run_grok itself.)"""
        if not grok.enabled(self.config) or time.time() < self._grok_off_until:
            return False
        return self._grok_chat_open(await grok.usage(self.config))

    def _grok_chat_open(self, reading: dict | None) -> bool:
        """Chat may use Grok's weekly quota down to the share kept for X lookups, a share that
        shrinks as the reset nears (quota.reserve_percent). No reading: no gate."""
        percent = (reading or {}).get("weekly_percent")
        if not isinstance(percent, (int, float)):
            return True
        base = 100.0 - self.config.grok_chat_max_weekly_percent  # the share kept for X lookups
        reset_at = (reading or {}).get("reset_at")
        reset = quota.parse_time(reset_at) if reset_at else None
        return quota.may_spend(100.0 - percent, base, reset, _dt.now(UTC))

    async def _grok_refused(self, error: Exception) -> None:
        """A refused session is either an injection that got a tool call through, or Grok's
        output changed under the sidecar. One alert, and after three in ten minutes Grok is
        switched off for an hour so a format change does not mean a refusal per message."""
        now = time.time()
        self._grok_refusals = [t for t in self._grok_refusals if now - t < 600] + [now]
        if len(self._grok_refusals) >= 3 and now >= self._grok_off_until:
            self._grok_off_until = now + 3600
            LOGGER.error("Grok refused 3 sessions in 10 minutes; off for an hour")
            await self.alerts.alert_now(
                GROK,
                "breaker",
                "🛑 **Grok** 的工作階段 10 分鐘內被安全檢查拒絕 3 次，已暫停一小時"
                "（xsearch log 有 REFUSED）",
                outage=True,
            )
        elif len(self._grok_refusals) == 1:
            await self.alerts.alert_now(
                GROK, "refused", f"🛑 **Grok** 的工作階段被安全檢查拒絕：{str(error)[:300]}"
            )

    def _grok_alive(self) -> bool:
        """Whether Grok can take an X question at all: configured and the breaker closed. The
        weekly chat share does not apply: X questions may use the whole weekly quota."""
        return grok.enabled(self.config) and time.time() >= self._grok_off_until

    def _default_entry(self) -> str:
        """DEFAULT_MODEL as a routing entry, with CODEX_REASONING_EFFORT where it names none
        (as `_answer` applies it to a member's own model)."""
        value, effort = split_stored(self.config.default_model or CODEX)
        return f"{value}|{effort or self.config.codex_reasoning_effort}"

    async def _route(
        self,
        key: str,
        replied_to: int | None,
        guild_id: int | None,
        user_id: int,
        prompt: str,
        attachments: Sequence[discord.Attachment],
        plain: bool,
        fresh: bool = False,
        effort: str = "",
    ) -> RoutePlan | None:
        """Where this request goes when the router picks the model; None when it does not (Jev
        off, or the member chose a model with /model). Every message is judged; a conversation
        never drops a band and follows its latest message of substance (routing.decide). An
        image is not judged (Jev reads text only): that message goes down the table's `image`
        list (Codex → Grok → agy; a conversation already on a Codex cell keeps it), and the
        conversation's route is left for the next message. No judgement otherwise — a Jev
        timeout, error, odd answer or low confidence — keeps a routed conversation where it is
        and sends a new one to DEFAULT_MODEL, without retrying Jev."""
        table = self.routing
        if table is None or self._own_model(guild_id, user_id):
            return None
        current = None if fresh else self.threads.routed(key, replied_to, plain)
        route = Route.from_dict(current["route"]) if current else None
        if route is not None and routing.retired(route.entry):
            # Its model is gone (agy dropped it): kept, it would quietly resolve to Codex for as
            # long as nothing moved the conversation. Start over instead.
            LOGGER.info("Stored route %s is on a retired model; starting over", route.entry)
            current = route = None
        sources = [*route.earlier, current["thread_id"]] if current and route else []
        turns = await asyncio.to_thread(
            lambda: [turn for thread in sources for turn in transcript_turns(self.config, thread)]
        )
        pointed, own = split_quoted(prompt)
        own = own.strip()
        said = [t.text for t in turns if t.role == "user" and t.speaker in (None, user_id)]
        images = any((a.content_type or "").startswith("image/") for a in attachments)
        if images:
            # Before the effort check: an image always takes the image list (a member's effort
            # then only sets the strength there, below).
            verdict = Verdict("image")
        elif effort:
            verdict = Verdict("effort")  # the member set this message's effort: no move
        elif not own:
            verdict = Verdict("no-text")
        else:
            verdict = await routing.judge(own, said[-CONTEXT_TURNS:], self.config)
        image_turn = verdict.status == "image"
        if verdict.status == "ok":
            route, why = routing.decide(table, verdict.kind, routing.band(verdict.score), route)
        elif image_turn:
            route, why = route or Route(table.image[0], "M", ""), "image"
        elif route is not None:
            why = "kept"  # no judgement: stay; never down on a missing answer
        else:
            route, why = Route(self._default_entry(), "M", ""), "default"
        # What this message is for the gates below: the verdict, or with none, the type that put
        # the conversation where it stays (an X conversation keeps its X handling). An image is
        # none of them: it goes the image list's order under the ordinary gates, never the X
        # ones (no X stand-in, no weekly-share exemption, no "no X search" note).
        if verdict.status == "ok":
            kind = verdict.kind
        else:
            kind = "" if image_turn else route.kind
        entry, degraded = route.entry, []
        if image_turn and routing.backend_of(entry) != CODEX:
            # This message only (the route stays for the next one). The image list starts with
            # Codex (checked at load); a conversation already on a Codex cell keeps its cell.
            entry = table.image[0]
        if not image_turn:
            # An agy pool under its reserve (or spent) is skipped before it is tried: Argon goes
            # straight to the cell it replaced, Claude to its own stand-in.
            if (
                routing.is_argon(entry)
                and route.kind in table.argon_failed
                and not self.agy_quota.usable(quota.group_of("gemini-4-argon"))
            ):
                entry = table.argon_failed[route.kind]
                degraded.append("argon-reserve")
            if (
                routing.is_claude(entry)
                and route.kind in table.claude_failed
                and not self.agy_quota.usable(quota.group_of("claude-"))
            ):
                entry = table.claude_failed[route.kind]
                degraded.append("claude-reserve")
        backend = routing.backend_of(entry)
        codex_tight: bool | None = None  # read once, for the entry or a Codex stand-in

        async def near_codex_quota() -> bool:
            nonlocal codex_tight
            if codex_tight is None:
                limits = await asyncio.to_thread(read_rate_limits, self.config)
                codex_tight = limits is not None and (
                    limits.primary_used_percent >= table.codex_five_hour
                    or limits.secondary_used_percent >= table.codex_seven_day
                )
            return codex_tight

        async def codex_effort(cell: str) -> str:
            """A Codex cell one effort level down near its quota (an effort the member set is
            theirs). The same rule for the entry and for a Codex stand-in."""
            if routing.backend_of(cell) != CODEX or effort or not await near_codex_quota():
                return cell
            lowered = routing.lower_effort(cell)
            if lowered != cell and "codex-quota" not in degraded:
                degraded.append("codex-quota")
            return lowered

        if backend == CODEX:
            entry = await codex_effort(entry)
        elif backend == GROK and kind == "live-x":
            if not self._grok_alive():
                entry = table.live_x_without_grok
                degraded.append("grok-off")
        elif backend == GROK and not await self._grok_usable():
            entry = table.grok_spent
            degraded.append("grok-spent")
        model = self.config.codex_model
        target = routing.resolved(entry, model)
        if effort:
            target = resolve(routing.choice_of(entry, model), effort)
        value = routing.choice_of(entry, model).value
        spares: list[Resolved] = []
        if target.backend == GROK and kind == "live-x":
            spares.append(routing.resolved(table.live_x_without_grok, model))
        stand_in = entry
        if not image_turn and routing.is_argon(entry) and route.kind in table.argon_failed:
            # Argon's weekly pool is small: spent, the question goes back to the cell Argon
            # replaced, and from a Claude cell on to Claude's own stand-in.
            stand_in = table.argon_failed[route.kind]
            if (
                routing.is_claude(stand_in)
                and route.kind in table.claude_failed
                and not self.agy_quota.usable(quota.group_of("claude-"))
            ):
                # Claude under its reserve too. Not logged as degraded: this is a spare, and
                # Argon may well answer itself.
                stand_in = table.claude_failed[route.kind]
            spares.append(routing.resolved(await codex_effort(stand_in), model))
        if not image_turn and routing.is_claude(stand_in) and route.kind in table.claude_failed:
            spares.append(routing.resolved(table.claude_failed[route.kind], model))
        # An image turn takes only the image list's order (no type stand-ins), then MODEL_CHAIN;
        # _answer drops repeats.
        if image_turn:
            spares.extend(routing.resolved(spare, model) for spare in table.image[1:])
        replay = ""
        if turns:
            # Another backend gets the conversation replayed, defanged by the prompt builder like
            # every other block; the earlier thread ids stay listed so a later move replays all
            # of it. A resumed turn keeps it too, for a stand-in that answers if its model fails.
            messages = [{"role": t.role, "content": t.text} for t in turns]
            replay = grok.render_history(trim_history(messages, self.config.grok_history_chars))
        resume = current["thread_id"] if current and current["model"] == value else ""
        history = "" if resume else replay
        earlier = route.earlier
        if current and not resume:
            earlier = (*route.earlier, current["thread_id"])[-MAX_EARLIER:]
        stored = Route(route.entry, route.band, route.kind, earlier)
        return RoutePlan(
            target=target,
            value=value,
            route=stored,
            resume=resume,
            history=history,
            replay=replay,
            spares=tuple(spares),
            log={
                "key": key,
                "turn": len(said) + 1,
                "status": verdict.status,
                "type": verdict.kind,
                "difficulty": round(verdict.score + 1, 2) if verdict.kind else None,
                "band": routing.band(verdict.score) if verdict.kind else None,
                "confidence": round(verdict.confidence, 3) if verdict.kind else None,
                "why": why,
                "cell": route.entry,
                "target": entry,
                "degraded": degraded,
                "replayed": bool(history),
                "jev_ms": verdict.ms,
                "input_tokens": verdict.input_tokens,
                "text": own,
                # A reply to someone's message: Jev saw only the member's words (as evaluated);
                # the holdout measures how often that misroutes "幫我看這個".
                "quoted": bool(pointed),
                # Files the answering model gets — the member's own plus those of a message they
                # replied to (on_message folds them in) — none of which Jev sees, as evaluated;
                # with `quoted`, a holdout can tell the two cases apart.
                "files": len(attachments),
            },
            # An image goes the owner's order, not because Grok is out: no "no X search" note.
            no_x=kind == "live-x" and not image_turn,
        )

    def _routed_memo(
        self, route_plan: RoutePlan | None, result: CodexResult, model: str
    ) -> tuple[str, Route | None]:
        """(ThreadStore model, route) to record a request's thread under. A routed thread is
        recorded under the backend that made it: the target, or the spare that answered for it
        (then the target's own thread, if this turn resumed one, joins the replay sources)."""
        if route_plan is None:
            return model, None
        via, target = result.via, route_plan.target
        if via is None or (via.backend, via.model) == (target.backend, target.model):
            return route_plan.value, route_plan.route
        value = parse_choice(f"{via.backend}:{via.model}", self.config.codex_model).value
        route = route_plan.route
        if route_plan.resume:
            earlier = (*route.earlier, route_plan.resume)[-MAX_EARLIER:]
            route = Route(route.entry, route.band, route.kind, earlier)
        return value, route

    def _slash_tag(
        self, route_plan: RoutePlan | None, result: CodexResult, model: str, effort: str
    ) -> str:
        """The model named above a slash answer: the one that answered. A routed request that
        nothing answered names none, rather than a model that was never tried."""
        if route_plan is not None:
            return f"自動選擇：{self._via_label(result.via)}" if result.via else ""
        target = result.via or resolve(self._choice(model), effort)
        shown = self._effort_label(target)
        return shown if target.backend == CODEX else f"{target.model} · {shown}"

    @staticmethod
    def _routed_kw(route_plan: RoutePlan | None) -> dict:
        """_answer's routing arguments for a routed request; none for a member's own model."""
        if route_plan is None:
            return {}
        return {
            "routed": route_plan.target,
            "routed_spares": route_plan.spares,
            "history": route_plan.history,
            "spare_history": route_plan.replay,
        }

    def _log_route(self, route_plan: RoutePlan, result: CodexResult) -> None:
        """One structured line per routed message. It carries the member's text (nothing private
        is said in the server) with the thread id and the turn, so a holdout can be lined up with
        what Jev said at the time."""
        via = result.via
        record = {
            **route_plan.log,
            "thread": result.thread_id,
            "via": f"{via.backend}:{via.model}|{via.effort}" if via else None,
            "fallback": via is not None and via != route_plan.target,
        }
        LOGGER.info("Route %s", json.dumps(record, ensure_ascii=False))

    def _via_label(self, via: Resolved) -> str:
        """What answered, as a member reads it: "Gemini 3.8 Flash · 低"."""
        if via.backend == AGY:
            name = next(
                (label for label, by in AGY_FAMILIES.values() if via.model in by.values()),
                via.model,
            )
        elif via.backend == GROK:
            found = grok.cached_model(via.model)
            name = f"Grok · {found.name if found else via.model}"
        elif via.backend in ROUTER_BACKENDS:
            name = f"{ROUTER_LABELS[via.backend]} · {via.model}"
        else:
            name = f"Codex · {via.model}"
        return f"{name} · {self._effort_label(via)}"

    def _routed_text(self, route_plan: RoutePlan, result: CodexResult) -> str:
        """The answer with what the router picked under it, and the note an X question gets
        when Grok could not answer it."""
        text = result.text
        if result.via is None:
            return text
        if route_plan.no_x and result.via.backend != GROK:
            text += f"\n\n{NO_X_NOTE}"
        return f"{text}\n-# 自動選擇：{self._via_label(result.via)}"

    def _remember(
        self,
        key: str,
        thread_id: str,
        message_id: int | None,
        plain: bool,
        model: str,
        route: Route | None = None,
    ) -> None:
        """Record the thread; a switch retires the old one, so harvest it without waiting."""
        switched = self.threads.switched(key, thread_id)
        self.threads.remember(
            key,
            thread_id,
            message_id,
            plain=plain,
            model=model,
            route=route.as_dict() if route else None,
        )
        if switched and hasattr(self, "_harvest_wakeup"):
            self._harvest_wakeup.set()

    @staticmethod
    def _files(result: CodexResult) -> list[discord.File]:
        # Discord caps a message at 10 attachments; generated images are deleted after sending.
        return [discord.File(path) for path in result.images[:10]]

    # ----- slash commands --------------------------------------------------------------------

    async def _run_tracked(self, key: str, owner_id: int, show, start):
        """Run one answer as a task the member can cancel (❌ on the placeholder, or -stop).
        `show(text, view)` paints the placeholder; `start(on_video_slow)` returns the answer
        coroutine. Returns the CodexResult, or None when the request was cancelled."""
        config = self.config
        longest = max(
            config.codex_timeout_seconds,
            config.local_llm_timeout_seconds + config.local_llm_queue_wait_seconds,
            config.image_gen_timeout_seconds + config.image_gen_queue_wait_seconds,
        )
        view = CancelView(owner_id, timeout=longest + 300)  # queueing and setup on top
        await show(THINKING, view)

        async def on_video_slow() -> None:
            await show(VIDEO_INTERIM, view)

        task = asyncio.create_task(start(on_video_slow, self._streamer(show, view)))
        view.task = task
        self.active[key] = task
        try:
            return await task
        except asyncio.CancelledError:
            await show(CANCELLED, None)
            return None
        finally:
            if self.active.get(key) is task:
                self.active.pop(key, None)

    @staticmethod
    def _streamer(show, view):
        """on_delta callback: paint the accumulated answer into the placeholder at most every
        STREAM_EDIT_SECONDS, never a tag-only interim (a <fetch>/<search> request is not an
        answer), and only the tail that fits a Discord message."""
        state = {"at": float("-inf")}

        async def on_delta(text: str) -> None:
            opening = text.lstrip()
            if opening.startswith("<") and not CUSTOM_EMOJI.match(opening):
                # A tag-only interim; an answer that opens with an emoji is still an answer.
                return
            now = time.monotonic()
            if now - state["at"] < STREAM_EDIT_SECONDS:
                return
            state["at"] = now
            await show(tail(text, STREAM_SHOW_CHARS) + " ▌", view)

        return on_delta

    def _remember_emoji(self, guild_id: int | None):
        """The guild's custom emoji named REMEMBER_EMOJI_NAME, or None for the default 👍."""
        wanted = self.config.remember_emoji_name
        cached = self._emoji_cache.get(guild_id or 0, {}).get(wanted)
        if cached is not None:
            return cached
        guild = self.get_guild(guild_id) if guild_id else None
        for emoji in getattr(guild, "emojis", ()) or ():
            if emoji.name == wanted:
                return discord.PartialEmoji(name=emoji.name, id=emoji.id, animated=emoji.animated)
        return None

    def _answer_view(self, guild_id: int | None, user_id: int, prompt: str, result) -> AnswerView:
        return AnswerView(user_id, self._remember_emoji(guild_id))

    async def _exchange_of(self, message: discord.Message) -> tuple[str, str]:
        """The question and answer behind one of the Bot's answer messages: an @mention answer
        replies to the member's message (the question), a slash answer quotes it at the top."""
        reference = message.reference
        if reference is not None and reference.message_id is not None:
            original = reference.resolved
            if not isinstance(original, discord.Message):
                try:
                    original = await message.channel.fetch_message(reference.message_id)
                except discord.HTTPException:
                    original = None
            if isinstance(original, discord.Message) and self.user is not None:
                question = strip_mention(original.content, self.user.id)
                if question:
                    return question, message.content
        return recover_exchange(message.content)

    async def handle_answer_button(
        self, interaction: discord.Interaction, action: str, user_id: int
    ) -> None:
        """🔁 / 👍 on an answer (owner already checked by the button)."""
        # The button outlives the channel's place on the allowlist, so it is checked like a command.
        reason = self._access(interaction.guild_id, interaction.channel, interaction.channel_id)
        if reason:
            await interaction.response.send_message(reason, ephemeral=True)
            return
        question, answer = await self._exchange_of(interaction.message)
        if action == "remember":
            LOGGER.info("Button remember guild=%s user=%s", interaction.guild_id, user_id)
            name = (question or answer).strip().splitlines()[0][:30] or "對話"
            text = f"問：{question.strip()[:200]}\n答：{answer.strip()[:600]}"
            line = self.memory.add("user", interaction.guild_id, user_id, name, text)
            await interaction.response.send_message(f"已記進你的個人記憶：{line}", ephemeral=True)
            return
        if not question:
            await interaction.response.send_message(
                "找不到原本的問題（訊息太舊或格式不對），請直接再問一次。", ephemeral=True
            )
            return
        await interaction.response.defer(thinking=True)
        # 🔁 continues the conversation the original answer came from: the member is asking for
        # another attempt at that question, not a stranger's take on it with the context stripped.
        # The answer's own message id is the link; if that thread is no longer resumable (TTL,
        # style or model change) fall back to the member's current thread here, then to none.
        key = ThreadStore.key(interaction.guild_id, interaction.channel_id, user_id)
        plain = self.memory.get_persona_off(interaction.guild_id, user_id)
        model = self._model(interaction.guild_id, user_id)
        resume = self.threads.by_message(
            getattr(interaction.message, "id", None), plain=plain, model=model
        ) or self.threads.current(key, plain=plain, model=model)
        # The member's own words are not always the whole question: asking by replying to someone
        # else's message folds what they pointed at into the prompt (on_message does this), and a
        # video link usually lives *there*, not in their text. Rebuild it the same way, or the
        # redo re-asks "整理一下影片大綱" with no video to look at.
        prompt, previews = question, {}
        asked = await self._referenced(interaction.message)
        pointed = await self._referenced(asked) if asked is not None else None
        if pointed is not None and pointed.author != self.user:
            # The quoted images are not re-downloaded here, so they are not announced as attached.
            prompt = with_quoted_message(question, pointed.author.display_name, pointed.content, 0)
        reason = self._validate(prompt, [])
        if reason:  # after the public defer, so the refusal is public too (as on_message)
            await interaction.followup.send(reason)
            return
        if asked is not None:
            previews = await self._previews(asked, pointed)
        routed = await self._route(
            key,
            getattr(interaction.message, "id", None),
            interaction.guild_id,
            user_id,
            prompt,
            [],
            plain,
        )
        if routed is not None:
            resume = routed.resume
        LOGGER.info(
            "Button redo guild=%s user=%s resume=%s quoted=%s",
            interaction.guild_id,
            user_id,
            bool(resume),
            pointed is not None,
        )
        # The deferred "thinking" response would hang past the interaction's lifetime.
        placeholder = SlashPlaceholder(interaction)
        late_notice = asyncio.create_task(placeholder.retire_when_late())
        try:
            result = await self._answer(
                prompt,
                [],
                interaction.guild_id,
                user_id,
                resume=resume,
                channel_id=interaction.channel_id,
                previews=previews,
                **self._routed_kw(routed),
            )
        finally:
            late_notice.cancel()
        if routed is not None:
            self._log_route(routed, result)
            result = replace(result, text=self._routed_text(routed, result))
        late = interaction_expired(interaction) and interaction.channel is not None
        if late:
            await placeholder.retire()
        sent = await self.send_answer(
            interaction.channel if late else interaction.followup,
            question,
            result,
            interaction.guild_id,
            user_id,
        )
        # Link the redo answer too, so replying to *it* continues the same thread.
        self._remember(
            key,
            result.thread_id,
            getattr(sent, "id", None),
            plain,
            *self._routed_memo(routed, result, model),
        )

    async def send_answer(self, destination, prompt: str, result, guild_id, user_id):
        """Post an answer (with its buttons) through any `.send`-able destination — used by the
        🔁 button, which lands the new answer as a follow-up. Returns the message the destination
        handed back (when it does), so the caller can link it to the thread. The question is
        quoted on top as on a slash answer: a follow-up is no reply, and 🔁 / 👍 on it find the
        question there."""
        chunks = split_discord_message(format_reply(prompt, result.text, resumed=result.resumed))
        try:
            sent = await destination.send(
                chunks[0],
                files=self._files(result),
                view=self._answer_view(guild_id, user_id, prompt, result),
            )
            for chunk in chunks[1:]:
                await destination.send(chunk)
            return sent
        finally:
            remove_dir(result.generated_dir)

    @app_commands.describe(
        count=f"最近幾則（預設 {DEFAULT_MESSAGES}，最多 {MAX_MESSAGES}）",
        hours="改成最近幾小時內的訊息（給了就不看 count）",
        focus="選填：特別想知道什麼（例如「誰答應了什麼」）",
    )
    async def summary_command(
        self,
        interaction: discord.Interaction,
        count: app_commands.Range[int, 5, MAX_MESSAGES] = DEFAULT_MESSAGES,
        hours: app_commands.Range[int, 1, 168] | None = None,
        focus: str | None = None,
    ) -> None:
        reason = self._access(interaction.guild_id, interaction.channel, interaction.channel_id)
        if reason:
            await interaction.response.send_message(reason, ephemeral=True)
            return
        await interaction.response.defer(thinking=True)
        try:
            fetched, cut = await recent_messages(interaction.channel, count, hours)
        except discord.HTTPException:
            await interaction.edit_original_response(content="讀不到這個頻道的訊息紀錄。")
            return
        transcript = render_transcript(fetched)
        if not transcript:
            await interaction.edit_original_response(content="這段時間裡沒有可摘要的訊息。")
            return
        prompt = summary_prompt(
            getattr(interaction.channel, "name", "此頻道"), transcript, len(fetched), focus or ""
        )
        key = ThreadStore.key(interaction.guild_id, interaction.channel_id, interaction.user.id)

        async def show(text: str, view) -> None:
            try:
                await interaction.edit_original_response(content=text, view=view)
            except discord.HTTPException:
                pass

        # A fresh, unremembered turn: the summary must not become the member's conversation.
        result = await self._run_tracked(
            key,
            interaction.user.id,
            show,
            lambda on_video_slow, on_delta: self._answer(
                prompt,
                [],
                interaction.guild_id,
                interaction.user.id,
                resume="",
                on_delta=on_delta,
            ),
        )
        if result is None:
            return
        label = window_label(len(fetched), hours, cut)
        # The header is part of the first message: split after adding it, or that part is over
        # Discord's limit and refused.
        chunks = split_discord_message(f"**頻道摘要{label}**\n{result.text}")
        shown = False
        try:
            await interaction.edit_original_response(content=chunks[0], view=None)
            shown = True
            for chunk in chunks[1:]:
                await interaction.followup.send(chunk)
        except discord.HTTPException:
            LOGGER.exception("Summary delivery failed guild=%s", interaction.guild_id)
            if not shown:  # the placeholder is still up: say so instead of leaving it
                with contextlib.suppress(discord.HTTPException):
                    await interaction.edit_original_response(
                        content="摘要送不出去，請再試一次。", view=None
                    )
        finally:
            remove_dir(result.generated_dir)

    @staticmethod
    def _keep_for_member(path: Path, into: Path) -> Path:
        """Copy a sandbox-made file out of the per-request scratch dir so it survives cleanup
        until the answer (and the file) has been sent."""
        into.mkdir(parents=True, exist_ok=True)
        target, counter = into / path.name, 2
        while target.exists():  # two runs in one request may both write out.png
            target = into / f"{path.stem}-{counter}{path.suffix}"
            counter += 1
        target.write_bytes(path.read_bytes())
        return target

    async def _apply_action_tags(
        self,
        text: str,
        guild_id: int | None,
        channel_id: int | None,
        user_id: int,
        asked: str = "",
        thread_id: str = "",
    ) -> tuple[str, list[str], list[str]]:
        """Carry out the memory, reminder, tracking and image tags in `text`: (the text without
        them, confirmations appended; each tag that could not be read, with the reason; each one
        read but refused for a reason the model can fix — a time, an id, a missing value)."""
        text, facts = extract_memory_tags(text)
        for scope, name, fact in facts:
            self.memory.add(scope, guild_id, user_id, name, fact)
        # Leftover tags are looked for in the model's words only. A confirmation quotes text from
        # outside (a reminder's words, a watched channel's title), and a tag in there is not the
        # model's: sent back as "your tag", the retry would turn it into an order.
        notes: list[str] = []
        refused: list[str] = []
        text = self._apply_reminder_tags(text, guild_id, channel_id, user_id, notes, refused)
        text = await self._apply_tracking_tags(text, guild_id, channel_id, user_id, notes, refused)
        text = await self._apply_image_tags(
            text, guild_id, channel_id, user_id, notes, refused, asked, thread_id
        )
        text, invented = strip_invented_tags(text)
        text, broken = strip_broken_tags(text)
        problems = [
            f"<{name} …/>: "
            + ("not in the documented form" if OWN_NAME.match(name) else "the Bot has no such tag")
            for name in invented
        ]
        problems += [f"{tag}: not in the documented form" for tag in broken]
        if notes:
            text = f"{text.strip()}\n\n" + "\n".join(notes)
        return text.strip(), problems, refused

    def _apply_reminder_tags(
        self,
        text: str,
        guild_id: int | None,
        channel_id: int | None,
        user_id: int,
        notes_out: list[str] | None = None,
        refused: list[str] | None = None,
    ) -> str:
        """Create / cancel the reminders the model asked for and append a confirmation — or,
        given `notes_out`, put the confirmations there and return the text without them. A
        refusal the model can fix also goes to `refused`, for it to send the tag again."""
        clean, creates, cancels = extract_reminder_tags(text)
        if not creates and not cancels:
            return text
        notes: list[str] = []
        fixable = refused if refused is not None else []
        for when_text, what, who in creates:
            if who is None:
                notes.append(f"（要提醒的人寫的不是 @提及，「{what[:40]}」的提醒沒有設）")
                continue
            due = parse_when(when_text)
            if due is None:
                notes.append(f"（時間「{when_text}」看不懂，提醒沒有設）")
                fixable.append(
                    f'<remind when="{when_text}">: the time cannot be read; write'
                    ' "YYYY-MM-DD HH:MM" in Taipei time'
                )
                continue
            if not what.strip():
                notes.append("（沒寫要提醒什麼，提醒沒有設）")
                fixable.append(f'<remind when="{when_text}">: text= is missing')
                continue
            if channel_id is None:
                notes.append("（這裡沒辦法設提醒）")
                continue
            item = self.reminders.add(guild_id, channel_id, user_id, due, what, who)
            if isinstance(item, str):
                notes.append(f"（提醒沒有設：{item}）")
                if item in (PAST, TOO_FAR):
                    fixable.append(f'<remind when="{when_text}">: {item}')
            else:
                names = ["你" if t == user_id else f"<@{t}>" for t in reminder_targets(item)]
                whom = ("提醒" if names[0] == "你" else "提醒 ") + "、".join(names)
                notes.append(f"⏰ 已設定 #{item['id']}：{describe(due)} {whom}：{item['text']}")
        # An id the member does not have is told, not sent back: a retry could only guess at
        # another of their reminders, and cancel one they never named.
        for reminder_id in cancels:
            done = self.reminders.cancel(user_id, reminder_id)
            notes.append(
                f"⛔ 已取消提醒 #{reminder_id}" if done else f"（找不到你的提醒 #{reminder_id}）"
            )
        return _with_notes(clean, notes, notes_out)

    async def _add_watch(
        self,
        locator: str,
        interest: str,
        mention_ids: Sequence[int],
        guild_id: int | None,
        channel_id: int | None,
        user_id: int,
        interval_minutes: int = 0,
        times: Sequence[str] = (),
        why: list[str] | None = None,
    ) -> tuple[str, object | None]:
        """Resolve one source and start watching it. Returns (message, watch or None);
        shared by the slash command and the <track> tag so both enforce the same limits. A
        refusal about the source itself (another one could work) is also noted in `why`."""
        fixable = why if why is not None else []
        store = self.tracker
        if store is None:
            return "社群追蹤尚未啟用；管理者需設定 TRACKING_ENABLED=true 與 provider 憑證。", None
        if guild_id is None or channel_id is None:
            return "這裡沒辦法建立追蹤。", None
        policy = interest.strip() or INTEREST_POLICY
        if len(locator) > 500 or len(policy) > 4000:
            fixable.append("too long")
            return "網址或追蹤條件太長。", None
        if len(store.watches(user_id=user_id, active_only=True)) >= (
            self.config.tracking_max_per_user
        ):
            return f"每人最多 {self.config.tracking_max_per_user} 個追蹤。", None
        try:
            provider = tracking_provider(locator)
        except ValueError as error:
            fixable.append("source")
            return str(error), None
        if provider == "youtube" and not self.config.youtube_api_key:
            return "管理者尚未設定 YOUTUBE_API_KEY。", None
        if provider == "twitch" and not (
            self.config.twitch_client_id and self.config.twitch_client_secret
        ):
            return "管理者尚未設定 TWITCH_CLIENT_ID／TWITCH_CLIENT_SECRET。", None
        if provider == "x" and not xsearch.enabled(self.config):
            return "管理者尚未設定 XSEARCH_URL（X 查詢服務），無法追蹤 X 帳號。", None
        resolver = {
            "youtube": self.youtube_tracker,
            "twitch": self.twitch_tracker,
            "x": self.x_tracker,
            "ruten": self.ruten_tracker,
            "web": self.web_tracker,
        }[provider]
        try:
            external_id, state = await resolver.resolve(locator)
            tracked = store.add_source(provider, external_id, locator, state)
            watch = store.add_watch(
                tracked.id,
                guild_id,
                channel_id,
                user_id,
                policy,
                mention_ids=mention_ids,
                interval_minutes=interval_minutes or self.config.tracking_classify_interval_minutes,
                times=times,
            )
        except (
            ProviderError,
            xsearch.XSearchError,
            aiohttp.ClientError,
            TimeoutError,
            ValueError,
        ) as error:
            LOGGER.warning("Tracking source resolution failed (%s)", type(error).__name__)
            fixable.append("unreadable")
            return "無法讀取這個來源；請確認網址與 provider 憑證後再試。", None
        label = str(state.get("title") or state.get("login") or external_id)
        also = (
            "，也會 @ " + "、".join(f"<@{uid}>" for uid in watch.mention_ids)
            if watch.mention_ids
            else ""
        )
        return (
            f"已新增追蹤 #{watch.id}：{provider} · {label}{also}\n"
            f"從現在起有符合的新內容就會通知（{cadence(watch)}，"
            "建立之前的舊內容不算）。判斷過程用 "
            f"/{self.config.command_prefix}-track log:{watch.id} 查，只有你看得到。",
            watch,
        )

    async def _apply_tracking_tags(
        self,
        text: str,
        guild_id: int | None,
        channel_id: int | None,
        user_id: int,
        notes_out: list[str] | None = None,
        refused: list[str] | None = None,
    ) -> str:
        """Create / retime / cancel the watches the model asked for and append a confirmation —
        or, given `notes_out`, put the confirmations there and return the text without them. A
        refusal the model can fix also goes to `refused`, for it to send the tag again."""
        clean, adds, intervals, schedules, cancels = extract_track_tags(text)
        fixable = refused if refused is not None else []
        # A tag the parser read but dropped used to vanish from the answer with nothing said:
        # one written wrong (no source, an id or minutes that is not a number) and one past the
        # per-answer limit. Only the Bot's own count — an operation's attribute, where an
        # operation goes — so HTML's <track kind src> in prose is not taken for a broken one.
        wrong, over = [], []
        for name, pattern, index, rule in (
            ("track", TRACK_TAG, 1, 'needs source="…"'),
            ("track_every", TRACK_EVERY_TAG, 2, 'needs id="N" minutes="M", digits'),
            ("track_at", TRACK_AT_TAG, 3, 'needs id="N" times="HH:MM"'),
            ("cancel_track", CANCEL_TRACK_TAG, 4, 'needs id="N"'),
        ):
            found = tags.live(pattern, text)
            read = [m for m in found if extract_track_tags(m.group(0))[index]]
            bad = sum(
                1
                for m in found
                if m not in read and _KEYS.search(m.group(0)) and _ends_a_line(text, m)
            )
            if bad:
                wrong.append((name, bad, rule))
            if len(read) > MAX_TRACK_TAGS:
                over.append((name, len(read) - MAX_TRACK_TAGS))
        if not (adds or intervals or schedules or cancels or wrong or over):
            return text
        store = self.tracker
        if store is None:  # nothing the model sends again could be carried out either
            return _with_notes(clean, ["（社群追蹤尚未啟用。）"], notes_out)
        notes: list[str] = []
        for name, count, rule in wrong:
            notes.append(f"（有 {count} 個追蹤指令格式不對，沒有執行）")
            fixable.append(f"{count} <{name}> tag(s) were not carried out: {rule}")
        for name, count in over:
            what = {"track": "新增", "track_every": "改頻率", "track_at": "改時間"}
            what = what.get(name, "取消")
            notes.append(f"（{what}追蹤一次最多 {MAX_TRACK_TAGS} 個，多的 {count} 個沒有執行）")
        for locator, interest, who, every, times in adds:
            if times is None:
                notes.append(
                    f"（{locator[:80]} 的時間看不懂，沒有建立追蹤；請用像 12:01、20:01 這樣的寫法）"
                )
                fixable.append(f'<track source="{locator[:80]}">: at= cannot be read; "HH:MM"')
                continue
            why: list[str] = []
            note, watch = await self._add_watch(
                locator, interest, who, guild_id, channel_id, user_id, every, times, why
            )
            if watch is None and why:  # a source it can correct, not a limit or a setting
                fixable.append(f'<track source="{locator[:80]}">: {note[:200]}')
            # A refusal only ever reached the member as text: the log recorded that a tag had
            # been parsed and nothing more, so a watch that was never created looked exactly
            # like one that was. Record the outcome, not just the intent.
            LOGGER.info(
                "Tracking add guild=%s channel=%s user=%s source=%r -> %s",
                guild_id,
                channel_id,
                user_id,
                locator[:120],
                f"#{getattr(watch, 'id', '?')}" if watch is not None else note[:100],
            )
            notes.append(note)
        for watch_id, minutes in intervals:
            done = store.set_watch_interval(watch_id, minutes, user_id)
            notes.append(
                f"⏱️ 追蹤 #{watch_id} 改成每 {max(1, minutes)} 分鐘判斷一次。"
                if done
                else f"（找不到你的追蹤 #{watch_id}）"
            )
            if not done:
                fixable.append(f'<track_every id="{watch_id}">: {NO_WATCH}')
        for watch_id, times in schedules:
            if not times:
                notes.append(f"（追蹤 #{watch_id} 的時間看不懂，請用像 12:01、20:01 這樣的寫法）")
                fixable.append(f'<track_at id="{watch_id}">: times= cannot be read; "HH:MM"')
                continue
            done = store.set_watch_times(watch_id, times, user_id)
            notes.append(
                f"⏱️ 追蹤 #{watch_id} 改成每天 {'、'.join(times)}（台灣時間）判斷，"
                "從下一個時間開始。"
                if done
                else f"（找不到你的追蹤 #{watch_id}）"
            )
            if not done:
                fixable.append(f'<track_at id="{watch_id}">: {NO_WATCH}')
        for watch_id in cancels:
            # Scoped to the asking member, exactly like the slash command: one member can never
            # talk the Bot into dropping someone else's watch. An unknown id is told, not sent
            # back, for the same reason as a reminder's.
            done = store.delete_watch(watch_id, user_id)
            LOGGER.info(
                "Tracking cancel guild=%s channel=%s user=%s watch=%s -> %s",
                guild_id,
                channel_id,
                user_id,
                watch_id,
                "deleted" if done else "not found",
            )
            notes.append(
                f"⛔ 已取消追蹤 #{watch_id}。" if done else f"（找不到你的追蹤 #{watch_id}）"
            )
        LOGGER.info(
            "Tracking tags guild=%s channel=%s user=%s adds=%d every=%d at=%d cancels=%d",
            guild_id,
            channel_id,
            user_id,
            len(adds),
            len(intervals),
            len(schedules),
            len(cancels),
        )
        return _with_notes(clean, notes, notes_out)

    def _tracked_lines(self, user_id: int) -> str:
        """The member's watches for the prompt, so the model can act on one without guessing."""
        if self.tracker is None:
            return ""
        watches = self.tracker.watches(user_id=user_id, active_only=True)
        if not watches:
            return ""
        labels = {}
        for watch in watches:
            source = self.tracker.get_source(watch.source_id)
            if source is not None:
                labels[watch.source_id] = f"{source.provider} · {source.locator}"
        return render_watches(watches, labels)

    async def _fire_reminder(self, item: dict) -> None:
        channel = self.get_channel(item["channel_id"]) or await self.fetch_channel(
            item["channel_id"]
        )
        # Checked again when it fires, like a tracking post: the allowlist may have changed since
        # the reminder was set, and the channel must still be in the guild it was set in.
        channel_guild = getattr(getattr(channel, "guild", None), "id", None)
        if channel_guild != item.get("guild_id") or self._access(
            item.get("guild_id"), channel, item["channel_id"]
        ):
            LOGGER.warning(
                "Reminder %s dropped: channel %s is no longer allowed",
                item.get("id"),
                item["channel_id"],
            )
            return
        who = reminder_targets(item)
        # Anyone pinged besides the setter is told who set it, a group the setter is in too.
        by = f"（{'<@' + str(item['user_id']) + '>'} 設的）" if who != [item["user_id"]] else ""
        end = f" 提醒：{item['text']}{by}"

        def line(ids: list[int], end: str) -> str:
            return f"⏰ {' '.join(f'<@{t}>' for t in ids)}{end}"

        # Only the targets are pinged; a mention inside the reminder text is shown, not sent.
        # Too many people for one message: the first carries the text, the rest only mentions.
        while who:
            take = 1
            while take < len(who) and len(line(who[: take + 1], end)) <= DISCORD_MESSAGE_LIMIT:
                take += 1
            batch, who = who[:take], who[take:]
            await channel.send(
                line(batch, end),
                allowed_mentions=discord.AllowedMentions(
                    users=[discord.Object(id=t) for t in batch], everyone=False, roles=False
                ),
            )
            end = ""

    @app_commands.describe(
        when="什麼時候：30分鐘後、2小時後、明天 9:30、後天下午3點、21:00、9/15 14:30",
        text="到時要提醒的內容",
        who="要 @ 的人（留空＝提醒你自己）",
        cancel="要取消的提醒編號（用留空的 /指令 查看）",
    )
    async def remind_command(
        self,
        interaction: discord.Interaction,
        when: str | None = None,
        text: str | None = None,
        who: discord.Member | None = None,
        cancel: int | None = None,
    ) -> None:
        reason = self._access(interaction.guild_id, interaction.channel, interaction.channel_id)
        if reason:
            await interaction.response.send_message(reason, ephemeral=True)
            return
        user_id = interaction.user.id
        if cancel is not None:
            done = self.reminders.cancel(user_id, cancel)
            message = f"已取消提醒 #{cancel}。" if done else f"找不到你的提醒 #{cancel}。"
        elif when and text:
            due = parse_when(when)
            if due is None:
                message = (
                    f"看不懂時間「{when}」。可以寫：30分鐘後、明天 9:30、後天下午3點、9/15 14:30。"
                )
            else:
                target = who.id if who is not None else None
                item = self.reminders.add(
                    interaction.guild_id, interaction.channel_id, user_id, due, text, target
                )
                whom = f"提醒 {who.display_name}" if who is not None else "提醒你"
                message = (
                    item
                    if isinstance(item, str)
                    else (f"好，{describe(due)} 在這個頻道{whom}：{item['text']}（#{item['id']}）")
                )
        elif when or text:
            message = "要同時給 when（時間）和 text（內容）。"
        else:
            mine = self.reminders.for_user(user_id)
            message = (
                "你沒有提醒。"
                if not mine
                else "你的提醒：\n"
                + "\n".join(
                    f"#{i['id']} {describe(_dt.fromisoformat(i['due']))}"
                    + (" → " + _few(reminder_targets(i)) if _for_other(i) else "")
                    + f" — {i['text']}"
                    for i in mine
                )
            )
        await interaction.response.send_message(
            truncate(message, DISCORD_MESSAGE_LIMIT), ephemeral=True
        )

    @app_commands.describe(
        source="YouTube／Twitch 頻道、X 帳號、露天賣場（可帶 ?q=關鍵字）或網頁網址；留空列出",
        interest="選填：你特別想知道的內容；留空使用預設重大事件政策",
        times="選填：每天固定判斷的時間（台灣時間），例如 12:01,20:01；留空依間隔判斷",
        cancel="取消你的追蹤編號",
        log="看判斷紀錄：這個追蹤最近判斷了什麼、為什麼提醒或不提醒（只有你看得到）",
    )
    async def track_command(
        self,
        interaction: discord.Interaction,
        source: str | None = None,
        interest: str | None = None,
        times: str | None = None,
        cancel: int | None = None,
        log: int | None = None,
    ) -> None:
        reason = self._access(interaction.guild_id, interaction.channel, interaction.channel_id)
        if reason:
            await interaction.response.send_message(reason, ephemeral=True)
            return
        store = self.tracker
        if store is None:
            await interaction.response.send_message(
                "社群追蹤尚未啟用；管理者需設定 TRACKING_ENABLED=true 與 provider 憑證。",
                ephemeral=True,
            )
            return
        actions = (
            int(bool(source and source.strip())) + int(cancel is not None) + int(log is not None)
        )
        if actions > 1 or ((interest or times) and not source):
            await interaction.response.send_message(
                "新增、取消、看判斷紀錄一次只能做一件；interest 和 times 必須和 source 一起使用。",
                ephemeral=True,
            )
            return
        slots = parse_times(times or "")
        if times and not slots:
            await interaction.response.send_message(
                "times 看不懂，請用像 12:01,20:01 這樣的寫法（台灣時間）。", ephemeral=True
            )
            return
        if cancel is not None:
            deleted = store.delete_watch(cancel, interaction.user.id)
            text = f"已取消追蹤 #{cancel}。" if deleted else f"找不到你的追蹤 #{cancel}。"
            await interaction.response.send_message(text, ephemeral=True)
            return
        if log is not None:
            rows = store.recent_decisions(interaction.user.id, 10, log or None)
            await interaction.response.send_message(
                truncate(render_decision_log(rows), 1900), ephemeral=True
            )
            return
        if not source or not source.strip():
            watches = store.watches(user_id=interaction.user.id, active_only=True)
            if not watches:
                text = "你目前沒有社群追蹤。"
            else:
                lines = []
                for watch in watches:
                    tracked = store.get_source(watch.source_id)
                    if tracked is None:
                        continue
                    lines.append(
                        f"#{watch.id} {tracked.provider} · {tracked.locator}（{cadence(watch)}）"
                    )
                text = "你的社群追蹤：\n" + "\n".join(lines)
            await interaction.response.send_message(truncate(text, 1900), ephemeral=True)
            return

        # Same path as the <track> tag, so both enforce the same limits and say the same things.
        await interaction.response.defer(ephemeral=True, thinking=True)
        text, _watch = await self._add_watch(
            source.strip(),
            interest or "",
            (),  # extra mentions come from asking in words, not from a command option
            interaction.guild_id,
            interaction.channel_id,
            interaction.user.id,
            times=slots,
        )
        await interaction.followup.send(text, ephemeral=True)

    async def export_command(self, interaction: discord.Interaction) -> None:
        reason = self._access(interaction.guild_id, interaction.channel, interaction.channel_id)
        if reason:
            await interaction.response.send_message(reason, ephemeral=True)
            return
        root = self.memory.scope_dir("user", interaction.guild_id, interaction.user.id)
        label = f"memory-{interaction.guild_id}-{interaction.user.id}"
        data = await asyncio.to_thread(export_memory_zip, root, label)
        if data is None:
            await interaction.response.send_message(
                "你在這個伺服器還沒有個人記憶。", ephemeral=True
            )
            return
        if len(data) > 8_000_000:
            await interaction.response.send_message(
                f"你的記憶壓縮後有 {len(data) // 1_000_000} MB，超過 Discord 附件上限，"
                "請找管理者拿。",
                ephemeral=True,
            )
            return
        await interaction.response.send_message(
            "這是你的個人記憶（索引、archive、每則內容）：",
            file=discord.File(io.BytesIO(data), filename=f"{label}.zip"),
            ephemeral=True,
        )

    async def stop_command(self, interaction: discord.Interaction) -> None:
        reason = self._access(interaction.guild_id, interaction.channel, interaction.channel_id)
        if reason:
            await interaction.response.send_message(reason, ephemeral=True)
            return
        key = ThreadStore.key(interaction.guild_id, interaction.channel_id, interaction.user.id)
        task = self.active.get(key)
        if task is not None and not task.done():
            task.cancel()
            text = "已取消你在這個頻道進行中的請求。"
        else:
            text = "你在這個頻道沒有進行中的請求。"
        await interaction.response.send_message(text, ephemeral=True)

    @app_commands.describe(action="查詢，或設為全部清洗／只清洗純連結／全關")
    @app_commands.choices(action=LINKCLEAN_CHOICES)
    async def linkclean_command(
        self, interaction: discord.Interaction, action: str = "status"
    ) -> None:
        reason = self._access(interaction.guild_id, interaction.channel, interaction.channel_id)
        if reason:
            await interaction.response.send_message(reason, ephemeral=True)
            return
        if interaction.guild is None:
            await interaction.response.send_message("只能在伺服器中使用。", ephemeral=True)
            return
        if not self._is_guild_admin(interaction):
            await interaction.response.send_message(
                "只有伺服器主人、管理員（Administrator／Manage Guild）或指定管理員可以切換。",
                ephemeral=True,
            )
            return
        action = action.strip().lower()
        if action == "status":
            label = MODES[self.linkclean.mode(interaction.guild.id)]
            await interaction.response.send_message(f"連結洗參數：{label}", ephemeral=True)
        elif action in MODES:
            self.linkclean.set(interaction.guild.id, action)
            await interaction.response.send_message(
                f"連結洗參數已設為「{MODES[action]}」。", ephemeral=True
            )
        else:
            await interaction.response.send_message(
                "action 請用 status、all、links 或 off。", ephemeral=True
            )

    @app_commands.describe(action="查詢、開啟或關閉")
    @app_commands.choices(action=EMBEDFIX_CHOICES)
    async def embedfix_command(
        self, interaction: discord.Interaction, action: str = "status"
    ) -> None:
        reason = self._access(interaction.guild_id, interaction.channel, interaction.channel_id)
        if reason:
            await interaction.response.send_message(reason, ephemeral=True)
            return
        if interaction.guild is None:
            await interaction.response.send_message("只能在伺服器中使用。", ephemeral=True)
            return
        if not self._is_guild_admin(interaction):
            await interaction.response.send_message(
                "只有伺服器主人、管理員（Administrator／Manage Guild）或指定管理員可以切換。",
                ephemeral=True,
            )
            return
        action = action.strip().lower()
        if action in ("on", "off"):
            self.linkclean.set_embedfix(interaction.guild.id, action == "on")
        state = "開" if self.linkclean.embedfix(interaction.guild.id) else "關"
        note = (
            ""
            if self.linkclean.mode(interaction.guild.id) != "off"
            else "（連結洗參數為全關時不會投遞）"
        )
        await interaction.response.send_message(f"預覽修正：{state}{note}", ephemeral=True)

    # ----- image generation ------------------------------------------------------------------

    async def image_model_options(self, current: str) -> list[app_commands.Choice[str]]:
        """The image service's models for the slash command's autocomplete; none while the
        service is off or cannot be reached."""
        if not self.images.enabled:
            return []
        try:
            catalog = await self.images.catalog()
        except ImageGenError:
            return []
        needle = current.strip().lower()
        choices = []
        for found in catalog.models:
            if needle in found.id.lower() or needle in found.name.lower():
                label = found.id if found.name == found.id else f"{found.name}（{found.id}）"
                choices.append(app_commands.Choice(name=label[:100], value=found.id))
        return choices[:25]  # Discord shows at most 25

    def _image_refusal(self, guild_id: int | None) -> str:
        """Check the service and guild switch before a slash-command or <image_gen/> job."""
        if not self.images.enabled:
            return IMAGE_OFF
        if guild_id is None:
            return "只能在伺服器中使用。"
        if not self.image_settings.enabled(guild_id):
            return "這個伺服器沒有開放圖片生成。"
        return ""

    async def _run_image(
        self,
        guild_id: int,
        user_id: int,
        request: ImageRequest,
        into: Path,
        max_bytes: int,
        on_progress: Callable[[imagegen.Job], Awaitable[None]] | None = None,
        inputs: Mapping[str, discord.Attachment] | None = None,
    ) -> imagegen.Result:
        """Run an admitted job (imagegen.run_job). `inputs` are the slash command's image, mask
        and reference image, read only once the request, their type and size included, has
        passed the catalog.
        """
        if inputs:
            imagegen.resolve(await self.images.catalog(), request)
        for option, attachment in (inputs or {}).items():
            request = replace(request, **{option: await self._image_input(attachment)})
        return await imagegen.run_job(
            self.images, request, into, max_bytes, guild_id, user_id, on_progress
        )

    @staticmethod
    def _attached(attachment: discord.Attachment | None) -> imagegen.InputImage | None:
        """An attachment as an input image before it is read: the type and size Discord
        reports, which `imagegen.resolve` checks; the bytes are read after it passes."""
        if attachment is None:
            return None
        mime = imagegen.stated_type(attachment.content_type, attachment.filename)
        return imagegen.InputImage(mime, attachment.size)

    @staticmethod
    async def _image_input(attachment: discord.Attachment) -> imagegen.InputImage:
        try:
            data = await attachment.read()
        except discord.HTTPException:
            LOGGER.warning("Image input %s could not be read", attachment.id)
            raise ImageGenError("讀不到附的圖，請重新上傳再試。", "bad_input") from None
        return imagegen.input_image(data)

    @staticmethod
    def _filesize_limit(guild: discord.Guild | None) -> int:
        """The largest file the Bot may post in `guild`; the service fits each image to it."""
        return (
            guild.filesize_limit
            if guild is not None
            else discord.utils.DEFAULT_FILE_SIZE_LIMIT_BYTES
        )

    @app_commands.describe(
        prompt="要畫什麼",
        character="選填：角色名稱（中英文或暱稱都可以）；先給你確認是哪個角色，再挑畫得出它的模型",
        force_lora=(
            "選填：搭配 character；不管模型認不認得角色，"
            "先找 Civitai 上的角色 LoRA（畫得不像時重試用）"
        ),
        ref_image="選填：角色圖；保留圖中的角色，照描述畫新的場景或姿勢",
        image="選填：要改的圖（PNG／JPEG／WebP）；保留構圖與姿勢，照描述重畫",
        model="選填：圖片模型（打字篩選；留空＝服務預設）",
        aspect="選填：長寬比",
        count="選填：張數",
        seed="選填：種子（同樣的描述與種子會畫出同樣的圖）",
        negative="選填：不要出現在圖裡的東西",
        enhance="選填：生成前怎麼改寫描述（留空＝模型預設）",
        strength="選填：0～1，「改這張圖」可以改多少",
        mask="選填：白色的地方重畫（要搭配「改這張圖」）",
        ref_strength="選填：0～1，多像「照這個角色」的圖（只有 WAI，編輯模型不支援）",
        upscale="選填：放大倍率",
        detailer="選填：細節修補",
    )
    @app_commands.rename(
        image=imagegen.SLASH_NAMES["init_image"],
        ref_image=imagegen.SLASH_NAMES["ref_image"],
        force_lora=FORCE_LORA_NAME,
        mask=imagegen.SLASH_NAMES["mask"],
        strength=imagegen.SLASH_NAMES["strength"],
        ref_strength=imagegen.SLASH_NAMES["ref_strength"],
    )
    @app_commands.choices(
        aspect=IMAGE_ASPECT_CHOICES, upscale=IMAGE_UPSCALE_CHOICES, enhance=IMAGE_ENHANCE_CHOICES
    )
    @app_commands.autocomplete(model=_image_model_autocomplete)
    async def image_command(
        self,
        interaction: discord.Interaction,
        prompt: str,
        character: str | None = None,
        force_lora: bool | None = None,
        ref_image: discord.Attachment | None = None,
        image: discord.Attachment | None = None,
        model: str | None = None,
        aspect: app_commands.Choice[str] | None = None,
        count: app_commands.Range[int, 1, imagegen.MAX_COUNT] | None = None,
        seed: app_commands.Range[int, 0, imagegen.MAX_SEED] | None = None,
        negative: str | None = None,
        enhance: app_commands.Choice[str] | None = None,
        strength: app_commands.Range[float, 0.0, 1.0] | None = None,
        mask: discord.Attachment | None = None,
        ref_strength: app_commands.Range[float, 0.0, 1.0] | None = None,
        upscale: app_commands.Choice[str] | None = None,
        detailer: bool | None = None,
    ) -> None:
        prompt = prompt.strip()
        character = (character or "").strip()
        request = ImageRequest(
            prompt,
            (model or "").strip(),
            negative=(negative or "").strip() or None,
            aspect=aspect.value if aspect is not None else None,
            count=count,
            seed=seed,
            enhance=enhance.value if enhance is not None else None,
            init_image=self._attached(image),
            strength=strength,
            mask=self._attached(mask),
            ref_image=self._attached(ref_image),
            ref_strength=ref_strength,
            upscale=upscale.value if upscale is not None else None,
            detailer=detailer,
        )
        attached = (("init_image", image), ("mask", mask), ("ref_image", ref_image))
        inputs = {k: v for k, v in attached if v is not None}
        reason = (
            self._access(interaction.guild_id, interaction.channel, interaction.channel_id)
            or self._validate(prompt, ())
            or self._image_refusal(interaction.guild_id)
            or self._character_refusal(character, ref_image, force_lora)
        )
        if reason:
            await interaction.response.send_message(reason, ephemeral=True)
            return
        into = None
        try:
            into = request_dir(self.config)
            await self._image_slash(interaction, request, into, inputs, character, bool(force_lora))
        finally:
            remove_dir(into)

    @staticmethod
    def _character_refusal(
        character: str, ref_image: discord.Attachment | None, force_lora: bool | None = None
    ) -> str:
        if not character:
            return FORCE_LORA_ALONE if force_lora else ""
        if ref_image is not None:
            return CHARACTER_WITH_REF
        if len(character) > characters.CHARACTER_MAX:
            return f"character 最多 {characters.CHARACTER_MAX} 字。"
        return ""

    async def _image_slash(
        self,
        interaction: discord.Interaction,
        request: ImageRequest,
        into: Path,
        inputs: Mapping[str, discord.Attachment] | None = None,
        character: str = "",
        force_lora: bool = False,
    ) -> None:
        """The slash command's job, painted into its placeholder: the queue position and steps
        while it runs, ❌ to cancel it (on the service too), and the images in place of the
        placeholder — or in the channel once the interaction has closed. Not in the SerialQueue:
        the image service keeps its own queue, and a picture must not hold up answers. With a
        `character`, the character is identified and confirmed first (`_character_jobs`), and
        the way it is drawn stays above the progress."""
        await interaction.response.defer(thinking=True)
        placeholder = SlashPlaceholder(interaction)
        late = asyncio.create_task(placeholder.retire_when_late())
        config = self.config
        longest = config.image_gen_timeout_seconds + config.image_gen_queue_wait_seconds
        view = CancelView(interaction.user.id, timeout=longest + CHARACTER_CONFIRM_SECONDS + 300)
        way = ""

        async def on_progress(job: imagegen.Job) -> None:
            text = imagegen.progress_text(job)
            await placeholder.show(f"{way}\n{text}" if way else text, view)

        max_bytes = self._filesize_limit(interaction.guild)
        guild_id, user_id = interaction.guild_id, interaction.user.id

        async def run() -> imagegen.Result:
            nonlocal way
            if not character:
                return await self._run_image(
                    guild_id, user_id, request, into, max_bytes, on_progress, inputs
                )
            await placeholder.show(CHARACTER_LOOKING, view)
            *first, last = await self._character_jobs(
                interaction, placeholder, view, request, character, into, force_lora
            )
            for job, way in first:
                await placeholder.show(way, view)
                try:
                    return await self._run_image(
                        guild_id, user_id, job, into, max_bytes, on_progress, inputs
                    )
                except ImageGenError as error:
                    if error.code != "lora_unavailable":
                        raise
                    LOGGER.info("Character LoRA unavailable guild=%s; the next way", guild_id)
            job, way = last
            await placeholder.show(way, view)
            return await self._run_image(
                guild_id, user_id, job, into, max_bytes, on_progress, inputs
            )

        task = asyncio.create_task(run())
        view.task = task
        try:
            result = await task
        except (asyncio.CancelledError, CharacterDeclined):
            await placeholder.show(CANCELLED, None)
            LOGGER.info(
                "Cancelled image guild=%s user=%s", interaction.guild_id, interaction.user.id
            )
            return
        except ImageGenError as error:
            await placeholder.show(f"⚠️ {error}", None)
            return
        except Exception:  # a file that could not be written, say: still an answer, not silence
            LOGGER.exception("Image job failed guild=%s", interaction.guild_id)
            await placeholder.show(f"⚠️ {imagegen.FAILED}", None)
            return
        finally:
            late.cancel()
        hide = imagegen.spoilered(result, self.image_settings.spoiler(interaction.guild_id))
        files = [discord.File(path, spoiler=hide) for path in result.paths[:10]]
        try:
            if interaction_expired(interaction) and interaction.channel is not None:
                await placeholder.retire()
                await interaction.channel.send(
                    imagegen.caption(request.prompt, result, by=interaction.user.id), files=files
                )
            else:
                await interaction.edit_original_response(
                    content=imagegen.caption(request.prompt, result), attachments=files, view=None
                )
        except discord.HTTPException:
            LOGGER.exception("Image delivery failed guild=%s", interaction.guild_id)
            with contextlib.suppress(discord.HTTPException):
                await interaction.followup.send(
                    "圖片送出失敗（Discord 錯誤），請再試一次。", ephemeral=True
                )
        LOGGER.info(
            "Completed image guild=%s channel=%s user=%s",
            interaction.guild_id,
            interaction.channel_id,
            interaction.user.id,
        )

    async def _character_judge(
        self, prompt: str, schema: Path, images: Sequence[Path], web_search: bool = False
    ) -> str:
        """characters.Judge: one isolated turn in the Codex queue, on agy's Argon while Codex is
        unavailable. The prompt holds only the character's name or Danbooru and Civitai data.
        Waiting and answering together get characters.JUDGE_SECONDS."""
        try:
            async with asyncio.timeout(characters.JUDGE_SECONDS):
                return await self.queue.run(
                    lambda: run_batch(
                        prompt,
                        self.config,
                        schema=schema,
                        isolated=True,
                        images=images,
                        spare=characters.JUDGE_SPARE,
                        web_search=web_search,
                    )
                )
        except TimeoutError:
            LOGGER.warning("Character judge took over %ss", characters.JUDGE_SECONDS)
            raise ImageGenError(characters.JUDGE_BUSY, "judge_busy") from None

    async def _character_name_judge(self, prompt: str, schema: Path, images: Sequence[Path]) -> str:
        """The judge for a character's name, with the model's own web search on: the prompt holds
        only the name (and web results for it), never the member's scene."""
        return await self._character_judge(prompt, schema, images, web_search=True)

    async def _character_search(self, name: str) -> str:
        """characters.Search: the web results for a character's name (never the member's scene),
        through the same search providers as <web/>; "" without a provider or a hit."""
        if not search.available(self.config):
            return ""
        provider, hits = await search.search_web(name, self.config)
        return search.render_results(name, provider, hits) if hits else ""

    async def _character_jobs(
        self,
        interaction: discord.Interaction,
        placeholder: SlashPlaceholder,
        view: CancelView,
        request: ImageRequest,
        name: str,
        into: Path,
        force_lora: bool = False,
    ) -> list[tuple[ImageRequest, str]]:
        """The jobs that draw the character `name` stands for, each with the line that tells the
        member how: the chosen way, then (after a LoRA) the way to take when the service cannot
        get the LoRA. The member confirms the character first (`_confirm_character`);
        `force_lora` looks for a LoRA even when a model is taken to know the character."""
        catalog = await self.images.catalog()
        # An unknown model is refused before anything leaves the host; the options are checked
        # once the sample, the character or the LoRA is in the request.
        if request.model and catalog.get(request.model) is None:
            imagegen.resolve(catalog, request)
        web, judge = self.character_web, self._character_judge
        found = await characters.identify(
            name,
            self._character_name_judge,
            web,
            self.config.character_schema_path,
            self._character_search,
        )
        if found:
            await placeholder.show(CHARACTER_ASKING, view)
        tag, picture = await self._confirm_character(interaction, name, found)
        character = imagegen.Character(tag, await characters.traits(tag, web))
        choice = characters.choose(
            catalog,
            request.model,
            await characters.counts(tag, catalog, web),
            request.options(),
            request.enhance,
            force_lora,
        )
        no_way = ImageGenError(
            characters.NO_WAY.format(model=request.model or catalog.default_model),
            "unsupported_option",
            True,
        )
        if choice is None:
            raise no_way
        jobs: list[tuple[ImageRequest, str]] = []
        if choice.lora:
            await placeholder.show("🔎 找角色的 LoRA…", view)
            model = catalog.get(choice.model)
            reference = into / f"reference{imagegen.SUFFIXES[imagegen.sniff(picture)]}"
            await asyncio.to_thread(reference.write_bytes, picture)
            offer = await characters.find_lora(
                tag,
                model.lora_base if model else "",
                reference,
                judge,
                web,
                self.config.lora_schema_path,
                into,
            )
            if offer is not None:
                lora = imagegen.Lora(offer.version_id, None, offer.trigger)
                job = characters.build(request, choice, character, picture, lora)
                jobs.append((job, characters.way_text(choice, tag, offer.name)))
            choice = choice.fallback
        if choice is not None:
            job = characters.build(request, choice, character, picture)
            jobs.append((job, characters.way_text(choice, tag)))
        if not jobs:
            raise no_way
        LOGGER.info("Character %s for guild=%s: %s", tag, interaction.guild_id, jobs[0][0].model)
        return jobs

    async def _confirm_character(
        self, interaction: discord.Interaction, name: str, found: list[characters.Candidate]
    ) -> tuple[str, bytes]:
        """(tag, sample picture) of the candidate the member confirms on a private message,
        candidates in the judge's order; CharacterDeclined on cancel or time-out, and the
        not-found message when no candidate is left."""
        message = None
        outcome = "沒有其他候選了。"
        try:
            for candidate in found:
                picture = await characters.sample(
                    candidate.tag, self.character_web, characters.PICTURE_MAX_BYTES
                )
                if picture is None:
                    continue
                # The question lives on the slash command's token: every round must end before
                # INTERACTION_LIFETIME, not only the first.
                left = INTERACTION_LIFETIME - (discord.utils.utcnow() - interaction.created_at)
                if left.total_seconds() < 30:
                    outcome = CHARACTER_TOO_LATE
                    raise ImageGenError(CHARACTER_TOO_LATE, "character_too_late")
                wait = min(CHARACTER_CONFIRM_SECONDS, left.total_seconds())
                choice = CharacterView(interaction.user.id, wait)
                text = f"是這個角色嗎？`{candidate.tag}`（Danbooru 上 {candidate.posts} 張圖）"
                suffix = imagegen.SUFFIXES[imagegen.sniff(picture)]
                file = discord.File(io.BytesIO(picture), filename=f"character{suffix}")
                if message is None:
                    message = await interaction.followup.send(
                        text, file=file, view=choice, ephemeral=True, wait=True
                    )
                else:
                    await message.edit(content=text, attachments=[file], view=choice)
                await choice.wait()
                if choice.answer == "yes":
                    outcome = f"✅ 角色：`{candidate.tag}`"
                    return candidate.tag, picture
                if choice.answer != "next":
                    outcome = CANCELLED
                    raise CharacterDeclined
        except discord.HTTPException:
            LOGGER.exception("Character question failed guild=%s", interaction.guild_id)
            raise ImageGenError(imagegen.FAILED, "failed") from None
        except ImageGenError as error:  # Danbooru unreachable mid-way: say so, not "no one left"
            if outcome != CHARACTER_TOO_LATE:
                outcome = f"⚠️ {error}"
            raise
        except asyncio.CancelledError:
            outcome = CANCELLED
            raise
        finally:
            if message is not None:
                kept = {} if outcome.startswith("✅") else {"attachments": []}
                with contextlib.suppress(discord.HTTPException):
                    await message.edit(content=outcome, view=None, **kept)
        raise ImageGenError(characters.NOT_FOUND.format(name=name), "character_not_found")

    def _remember_local_thread(self, thread_id: str) -> None:
        self._local_image_threads.pop(thread_id, None)
        self._local_image_threads[thread_id] = None
        while len(self._local_image_threads) > LOCAL_THREADS_KEPT:
            del self._local_image_threads[next(iter(self._local_image_threads))]

    async def _apply_image_tags(
        self,
        text: str,
        guild_id: int | None,
        channel_id: int | None,
        user_id: int,
        notes_out: list[str] | None = None,
        refused: list[str] | None = None,
        asked: str = "",
        thread_id: str = "",
    ) -> str:
        """Start the pictures the model asked for with <image_gen/>, only when the member's own
        words (`asked`) ask for the local service (imagegen.asked_for_local), or they did earlier
        in this conversation (`thread_id`, remembered in memory). Each is checked
        now — the channel, the prompt, the catalog, the guild switch — so a refusal reaches
        the answer and one the model can fix goes to `refused`; the job then runs detached and is
        posted in the channel when it is done (`_deliver_image`). The answer gets only a note that
        it started: the picture does not exist yet."""
        clean, asks = extract_image_tags(text)
        if not asks:
            return text
        fixable = refused if refused is not None else []
        if not self.images.enabled:  # nothing the model sends again could run either
            return _with_notes(clean, ["（圖片生成尚未啟用。）"], notes_out)
        try:  # model names count as asking only when the catalog can be read
            catalog = await self.images.catalog()
        except ImageGenError:
            catalog = None
        if imagegen.asked_for_local(asked, catalog):
            if thread_id:
                self._remember_local_thread(thread_id)
        elif thread_id not in self._local_image_threads and (
            guild_id is None or self.image_settings.enabled(guild_id)  # else: "not open" below
        ):
            LOGGER.info("Image tag without an explicit local request guild=%s", guild_id)
            prefix = self.config.command_prefix
            return _with_notes(clean, [IMAGE_NOT_ASKED.format(prefix=prefix)], notes_out)
        notes: list[str] = []
        if len(asks) > IMAGE_TAGS_RUN:
            extra = len(asks) - IMAGE_TAGS_RUN
            notes.append(f"（地端佇列一次最多 {IMAGE_TAGS_RUN} 張，多的 {extra} 個沒有執行）")
        for attrs in asks[:IMAGE_TAGS_RUN]:
            label = attrs.get("prompt", "")[:60]
            request, problem = request_from_tag(attrs)
            if request is None:
                notes.append("（畫圖指令格式不對，沒有執行）")
                fixable.append(f'<image_gen prompt="{label}">: {problem}')
                continue
            if channel_id is None:
                notes.append("（這裡沒辦法生成圖片）")
                continue
            channel = self.get_channel(channel_id)
            reason = self._access(guild_id, channel, channel_id) or self._validate(
                request.prompt, ()
            )
            if reason:
                notes.append(f"（圖片沒有生成：{reason}）")
                continue
            try:
                imagegen.resolve(await self.images.catalog(), request)
            except ImageGenError as error:
                notes.append(f"（圖片沒有生成：{error}）")
                if error.fixable:
                    fixable.append(f'<image_gen prompt="{label}">: {error.detail}')
                continue
            if reason := self._image_refusal(guild_id):
                notes.append(f"（圖片沒有生成：{reason}）")
                continue
            task = asyncio.create_task(self._deliver_image(guild_id, user_id, request, channel_id))
            self._image_tasks.add(task)
            task.add_done_callback(self._image_tasks.discard)
            LOGGER.info(
                "Image tag started guild=%s channel=%s user=%s", guild_id, channel_id, user_id
            )
            notes.append(IMAGE_STARTED)
        return _with_notes(clean, notes, notes_out)

    async def _deliver_image(
        self, guild_id: int, user_id: int, request: ImageRequest, channel_id: int
    ) -> None:
        """Run an <image_gen/> job and post the pictures, or why there are none, in the channel
        it was asked in. The channel is checked again first, like a reminder: the allowlist may
        have changed while the job ran. The post pings no one."""
        into = None
        try:
            into = request_dir(self.config)
            max_bytes = self._filesize_limit(self.get_guild(guild_id))
            try:
                result = await self._run_image(guild_id, user_id, request, into, max_bytes)
                text, paths = imagegen.caption(request.prompt, result, by=user_id), result.paths
                hide = imagegen.spoilered(result, self.image_settings.spoiler(guild_id))
            except ImageGenError as error:
                text, paths, hide = f"<@{user_id}> 的圖片沒有生成：{error}", (), False
            channel = self.get_channel(channel_id) or await self.fetch_channel(channel_id)
            channel_guild = getattr(getattr(channel, "guild", None), "id", None)
            if channel_guild != guild_id or self._access(guild_id, channel, channel_id):
                LOGGER.warning(
                    "Image for user %s dropped: channel %s is no longer allowed",
                    user_id,
                    channel_id,
                )
                return
            await channel.send(
                text,
                files=[discord.File(path, spoiler=hide) for path in paths[:10]],
                allowed_mentions=discord.AllowedMentions.none(),
            )
        except Exception:
            LOGGER.exception("Image delivery failed guild=%s channel=%s", guild_id, channel_id)
        finally:
            remove_dir(into)

    def _instructions_status(self) -> str:
        persona = instructions.persona_text(self.config)
        style = instructions.style_text(self.config)
        lines = []
        for kind, text in ((instructions.PERSONA, persona), (instructions.OUTPUT_STYLE, style)):
            where = "上傳版" if instructions.is_uploaded(self.config, kind) else "image 預設"
            size = f"{len(text)} 字" if text else "空的"
            lines.append(f"{instructions.LABELS[kind]}：{where}，{size}")
        return "\n".join(lines)

    async def _apply_instructions(self) -> None:
        """Rebuild the Codex working directories and retire every live thread. Codex reads
        AGENTS.md once when a thread starts and never again on resume, so without this the old
        persona would answer in every conversation that is still inside its TTL."""
        await asyncio.to_thread(instructions.compose_workspaces, self.config)
        self.threads.set_version(instructions_version(self.config))

    @app_commands.describe(action="查詢、上傳，或把人設／輸出風格還原成 image 內的預設")
    @app_commands.choices(action=INSTRUCTION_CHOICES)
    async def persona_command(
        self, interaction: discord.Interaction, action: str = "status"
    ) -> None:
        reason = self._access(interaction.guild_id, interaction.channel, interaction.channel_id)
        if reason:
            await interaction.response.send_message(reason, ephemeral=True)
            return
        if interaction.guild is None:
            await interaction.response.send_message("只能在伺服器中使用。", ephemeral=True)
            return
        if not self._is_guild_admin(interaction):
            await interaction.response.send_message(
                "只有伺服器主人、管理員（Administrator／Manage Guild）或指定管理員可以修改。",
                ephemeral=True,
            )
            return
        action = action.strip().lower()
        if action == "upload":
            await interaction.response.send_modal(
                InstructionsModal(self._save_instructions, instructions.MAX_CHARS)
            )
            return
        if action in ("reset_persona", "reset_style"):
            kind = instructions.PERSONA if action == "reset_persona" else instructions.OUTPUT_STYLE
            _, text = await self._reset_instructions(kind, interaction.user.id)
            await interaction.response.send_message(text, ephemeral=True)
            return
        await interaction.response.send_message(self._instructions_status(), ephemeral=True)

    @staticmethod
    def _with_backup(text: str, kept: Path | None) -> str:
        return f"{text}\n備份：{kept.name}" if kept else text

    async def _reset_instructions(self, kind: str, who: int | str) -> tuple[bool, str]:
        """Put the persona or the default output style back to the image's: (whether anything
        was reset, the reply). Threads are retired only when something was. `who` is the
        member's id, or "admin" from the operator CLI."""
        async with self._instructions_lock:
            removed, kept = await asyncio.to_thread(instructions.reset, self.config, kind)
            if removed:
                await self._apply_instructions()
        label = instructions.LABELS[kind]
        head = (
            f"{label}已還原成 image 預設，所有對話串重新開始。"
            if removed
            else f"{label}本來就是 image 預設，沒有東西要還原。"
        )
        LOGGER.info("instructions reset kind=%s removed=%s by=%s", kind, removed, who)
        return removed, self._with_backup(f"{head}\n{self._instructions_status()}", kept)

    async def _save_instructions(self, uploads: dict[str, str], who: int | str) -> str:
        """Store one or both uploaded files, keep what they replaced, then rebuild and retire."""
        kept: list[Path] = []
        async with self._instructions_lock:
            for kind, text in uploads.items():
                backup = await asyncio.to_thread(instructions.save, self.config, kind, text)
                if backup is not None:
                    kept.append(backup)
                LOGGER.info("instructions uploaded kind=%s chars=%d by=%s", kind, len(text), who)
            await self._apply_instructions()
        names = "、".join(instructions.LABELS[kind] for kind in uploads)
        lines = [f"已更新 {names}，所有對話串重新開始。", self._instructions_status()]
        if kept:
            lines.append("備份：" + "、".join(path.name for path in kept))
        return "\n".join(lines)

    def _visible_channel(self, guild_id: int, channel_id: int) -> bool:
        """True when the Bot itself can see the channel, from the gateway cache: server memory
        is gathered from every such channel, private ones and the private threads the Bot is in
        included, the same rule emoji samples follow: whatever the Bot can read may inform
        server memory. A conversation in a private thread means the Bot posted there, which
        joins it. A channel the Bot cannot see or cannot find counts as not visible; an error
        (a thread whose parent is not cached) reaches the digest, which leaves the channel out."""
        guild = self.get_guild(guild_id)
        return _bot_sees(guild, guild.get_channel_or_thread(channel_id) if guild else None)

    def _is_guild_admin(self, interaction: discord.Interaction) -> bool:
        """Who may change operator settings: the server owner, a guild admin, or an id the
        operator listed in LINKCLEAN_ADMIN_IDS (covers a delegated owner who is not the
        Discord account that owns the server). Note this is a per-guild test applied to the
        persona, which is global: an admin of any allowed guild changes it for all of them, by
        design."""
        user_id = interaction.user.id
        guild = interaction.guild
        if guild is not None and guild.owner_id == user_id:
            return True
        member = interaction.user
        if isinstance(member, discord.Member) and (
            member.guild_permissions.administrator or member.guild_permissions.manage_guild
        ):
            return True
        return user_id in self.config.linkclean_admin_ids

    async def help_command(self, interaction: discord.Interaction) -> None:
        reason = self._access(interaction.guild_id, interaction.channel, interaction.channel_id)
        if reason:
            await interaction.response.send_message(reason, ephemeral=True)
            return
        chunks = split_discord_message(self.help_guide())
        await interaction.response.send_message(chunks[0], ephemeral=True)
        for chunk in chunks[1:]:
            await interaction.followup.send(chunk, ephemeral=True)

    async def status_command(self, interaction: discord.Interaction) -> None:
        reason = self._access(interaction.guild_id, interaction.channel, interaction.channel_id)
        if reason:
            await interaction.response.send_message(reason, ephemeral=True)
            return
        await interaction.response.defer(ephemeral=True, thinking=True)
        text = await self._status_text(
            interaction.guild_id, interaction.channel_id, interaction.user.id
        )
        await interaction.followup.send(text, ephemeral=True)

    async def _status_text(self, guild_id: int | None, channel_id: int | None, user_id: int) -> str:
        """Two sections: what this member has set (model, style, whether the next request
        continues a thread, memory sizes) and what the system offers. The system section carries
        the live Codex quota and the last spare-backend fallback, so a member who gets no notice
        in the answer itself can still see here that Codex is at its limit right now."""
        own = self._own_model(guild_id, user_id)
        stored = own or self.config.default_model
        chosen = self._choice(stored)
        level = split_stored(stored)[1]
        target = resolve(chosen, level or self.config.codex_reasoning_effort)
        origin = "你設定" if own else "伺服器預設"
        model_line = f"模型：{chosen.label}（{origin}）· 強度 {self._effort_label(target)}"
        auto = self.routing is not None and not own
        if auto:
            images = " → ".join(
                self._via_label(routing.resolved(entry, self.config.codex_model))
                for entry in self.routing.image
            )
            model_line = (
                f"模型：自動挑選（依每則問題的類型與難度）；附圖依序用 {images}；"
                f"判斷不出來的新對話用 {chosen.label} · 強度 {self._effort_label(target)}"
            )
        if chosen.backend in ROUTER_BACKENDS:
            info = self.catalogs[chosen.backend].get(chosen.family)
            if info is not None and not info.image:
                model_line += " · 看不到圖"
        style = self.memory.get_style(guild_id, user_id)
        persona_off = self.memory.get_persona_off(guild_id, user_id)
        style_line = f"風格：{style[:60]}" if style else "風格：無（用預設）"
        style_line += "；人設：關閉" if persona_off else "；人設：保留"

        key = ThreadStore.key(guild_id, channel_id, user_id)
        entry = self.threads.live_entry(key)
        routed = self.threads.routed(key, None, persona_off) if auto else None
        if routed is not None and entry is not None:
            minutes = max(0, int((time.time() - float(entry["at"])) // 60))
            thread_line = (
                f"續接：會接續 {minutes} 分鐘前的對話（目前用 "
                f"{self._choice(routed['model']).label}，更難的問題會換更強的模型）"
                f"；/{self.config.command_prefix} 的 new 可重來"
            )
        elif entry is None:
            thread_line = "續接：無，下一句會新開對話"
        else:
            minutes = max(0, int((time.time() - float(entry["at"])) // 60))
            previous = self._choice(str(entry.get("model", ""))).label
            if self.threads.current(key, plain=persona_off, model=chosen.value):
                thread_line = (
                    f"續接：會接續 {minutes} 分鐘前的對話（{previous}）"
                    f"；/{self.config.command_prefix} 的 new 可重來"
                )
            else:
                thread_line = f"續接：{minutes} 分鐘前的對話是 {previous}／另一種風格，下一句會新開"

        def scope_line(label: str, scope: str, owner: int | None, limit: int) -> str:
            shown = len(self.memory.entries(scope, guild_id, owner))
            total = len(self.memory.all_entries(scope, guild_id, owner))
            used = self.memory.usage_bytes(scope, guild_id, owner)
            text = f"{label} {total} 條 / {used // 1024} KB（上限 {limit // 1_000_000} MB）"
            return text + (f"，{total - shown} 條已推到 archive" if total > shown else "")

        memory_line = "記憶：" + " · ".join(
            (
                scope_line("個人", "user", user_id, self.config.memory_user_max_bytes),
                scope_line("伺服器", "guild", None, self.config.memory_guild_max_bytes),
                f"永久 {self.permanent.topic_count()} 主題",
            )
        )

        codex = await codex_login_status(self.config)
        limits = await probe_rate_limits(self.config)
        # Where a Codex turn goes right now, worked out as _answer does: from MODEL_CHAIN, not
        # just CODEX_FALLBACK_MODEL, and never into Grok.
        codex_choice = parse_choice(f"{CODEX}:{self.config.codex_model}", self.config.codex_model)
        spares = fallback_chain(
            codex_choice,
            self.config.model_chain,
            self.config.codex_model,
            self.config.codex_reasoning_effort,
            self.config.codex_fallback_model,
        )
        spare = next((target for target in spares if target.backend != GROK), None)
        if limits is not None:
            codex += (
                f" · 額度 5h {limits.primary_used_percent:.0f}%"
                f" / 7d {limits.secondary_used_percent:.0f}%"
            )
            # "Falling back right now" needs no memory: a spent window means the next request
            # goes to the spare. Derived from the live probe, so it survives a restart and covers
            # requests this process has not seen. Chat requests only: the batch jobs (tracking
            # classifier, memory) fall back to CODEX_FALLBACK_MODEL, agy only (run_batch).
            if max(limits.primary_used_percent, limits.secondary_used_percent) >= 100:
                codex += (
                    f"\n　└ 額度已達上限，現在的請求改用 {spare.model} 回答"
                    if spare is not None
                    else "\n　└ 額度已達上限，且沒有設定備援模型"
                )
        else:
            codex += " · 額度讀不到"
        routers = []
        for backend, catalog in self.catalogs.items():
            router = ROUTERS[backend]
            if router.enabled(self.config):
                count = len(await catalog.free_models())
                routers.append(f"{router.label} {count} 個{router.models_word}")
        system = [f"Codex：{codex}"]
        if grok.enabled(self.config):
            reading = await grok.usage(self.config)
            line = "Grok：可選"
            if reading and isinstance(reading.get("weekly_percent"), (int, float)):
                percent = reading["weekly_percent"]
                line += f" · 額度 7d {percent:.0f}%"
                if not self._grok_chat_open(reading):
                    line += "\n　└ 剩下的額度留給 X 查詢，聊天改用備援"
            else:
                line += " · 額度讀不到"
            if time.time() < self._grok_off_until:
                line += "\n　└ 安全檢查連續失敗，暫停中"
            system.append(line)
        if self._last_fallback is not None:
            # Any backend in MODEL_CHAIN can be the one that failed, so the line names it.
            when, why, failed, spare = self._last_fallback
            mins = max(0, int((time.time() - when) // 60))
            label = _FALLBACK_LABEL.get(type(why)) or getattr(why, "label", "服務異常")
            system.append(
                f"最近一次備援：{mins} 分鐘前 {BACKEND_LABELS.get(failed, failed)} {label}，"
                f"改用 {BACKEND_LABELS.get(spare.backend, spare.backend)}（{spare.model}）回答"
            )
        system += [
            "Antigravity：可選（Gemini／Claude）",
            " · ".join(routers) if routers else "OpenRouter／OrcaRouter／地端模型：未設定",
            f"影片理解：{'開' if gemini.available(self.config) else '關'} · 讀連結：開"
            f" · 搜尋：{'／'.join(n for n, _ in search.providers(self.config)) or '關'}"
            f" · 沙盒：{'開' if sandbox.available(self.config) else '關'}"
            f" · 資料 API：{'／'.join(self.apis) or '無'}",
        ]
        return "\n".join(
            (
                "【你的設定】",
                model_line,
                style_line,
                thread_line,
                memory_line,
                "",
                "【系統】",
                *system,
            )
        )

    @app_commands.describe(
        scope="個人＝只對你；伺服器＝這裡所有人", name="短標題", text="要記住的內容"
    )
    @app_commands.choices(scope=SCOPE_CHOICES)
    async def remember_command(
        self,
        interaction: discord.Interaction,
        scope: app_commands.Choice[str],
        name: str,
        text: str,
    ) -> None:
        reason = self._access(interaction.guild_id, interaction.channel, interaction.channel_id)
        if reason:
            await interaction.response.send_message(reason, ephemeral=True)
            return
        line = self.memory.add(scope.value, interaction.guild_id, interaction.user.id, name, text)
        await interaction.response.send_message(f"已記住：{line}", ephemeral=True)

    def memory_options(
        self, guild_id: int | None, user_id: int, scope: str, current: str
    ) -> list[app_commands.Choice[str]]:
        needle = current.strip().casefold()
        entries = self.memory.all_entries(scope, guild_id, user_id)
        return [
            app_commands.Choice(name=f"{e.name} · {e.file}"[:100], value=e.file)
            for e in entries
            if not needle or needle in f"{e.name} {e.file} {e.hook}".casefold()
        ][:25]

    def _forget_guidance(self, guild_id: int | None, user_id: int, name: str) -> str:
        """Identify feature IDs without mutating either feature's independent store."""
        ids = set(re.findall(r"(?<![0-9])[#＃]?([0-9]{1,9})(?![0-9])", name))
        prefix = self.config.command_prefix
        lines = []
        watches = self.tracker.watches(user_id=user_id, active_only=True) if self.tracker else []
        for watch in watches:
            if watch.guild_id == guild_id and str(watch.id) in ids:
                source = self.tracker.get_source(watch.source_id)
                title = source.locator if source else ""
                lines.append(f"追蹤 #{watch.id} {title}：`/{prefix}-track cancel:{watch.id}`")
        for item in self.reminders.for_user(user_id):
            if item.get("guild_id") == guild_id and str(item["id"]) in ids:
                lines.append(
                    f"提醒 #{item['id']} {item['text']}：`/{prefix}-remind cancel:{item['id']}`"
                )
        if not lines:
            return ""
        return "\n這些是追蹤／提醒編號，不是記憶編號；請選擇要取消的項目：\n" + "\n".join(lines)

    @app_commands.describe(scope="個人或伺服器", name="從選單選記憶，或輸入完整名稱；不是追蹤編號")
    @app_commands.choices(scope=SCOPE_CHOICES)
    @app_commands.autocomplete(name=_memory_autocomplete)
    async def forget_command(
        self, interaction: discord.Interaction, scope: app_commands.Choice[str], name: str
    ) -> None:
        reason = self._access(interaction.guild_id, interaction.channel, interaction.channel_id)
        if reason:
            await interaction.response.send_message(reason, ephemeral=True)
            return
        name = name.strip()
        entries = self.memory.all_entries(scope.value, interaction.guild_id, interaction.user.id)
        matches = [e for e in entries if e.file == name] or [e for e in entries if e.name == name]
        outcome = "not_found"
        if len(matches) == 1:
            entry = matches[0]
            forgot = self.memory.forget(
                scope.value, interaction.guild_id, interaction.user.id, entry.file, by_file=True
            )
            outcome = "deleted" if forgot else "not_found"
            text = (
                f"已刪除{SCOPES[scope.value]}記憶「{entry.name}」。"
                if forgot
                else "記憶已不存在，請重新選取。"
            )
        else:
            if matches:
                outcome = "ambiguous"
                text = f"有多條同名記憶「{name}」，請從 name 選單選取個別項目。沒有刪除任何資料。"
            else:
                text = f"找不到{SCOPES[scope.value]}記憶「{name}」。沒有刪除任何資料。"
                text += self._forget_guidance(interaction.guild_id, interaction.user.id, name)
            choices = self.memory_options(
                interaction.guild_id, interaction.user.id, scope.value, ""
            )
            if choices:
                text += "\n可選記憶（最多列 25 條；輸入文字可篩選）：\n" + "\n".join(
                    c.name for c in choices
                )
            text += f"\n記憶用名稱選取；完整清單：`/{self.config.command_prefix}-memory`。"
        LOGGER.info(
            "Memory forget guild=%s user=%s scope=%s target=%r outcome=%s",
            interaction.guild_id,
            interaction.user.id,
            scope.value,
            name[:100],
            outcome,
        )
        await interaction.response.send_message(truncate(text, 1900), ephemeral=True)

    def _memory_text(self, guild_id: int | None, user_id: int, scope: str = "") -> str:
        """List all removable notes, including archived notes, without positional IDs."""
        sections = []
        for kind in (scope,) if scope else SCOPES:
            entries = self.memory.all_entries(kind, guild_id, user_id)
            if entries:
                lines = [f"- **{e.name}**（{e.file}）— {e.hook}" for e in entries]
                sections.append(f"[{SCOPES[kind]}記憶索引]\n" + "\n".join(lines))
        if not sections:
            return f"目前沒有{SCOPES[scope] if scope else ''}記憶。"
        prefix = self.config.command_prefix
        sections.append(
            f"刪除記憶：`/{prefix}-forget`，選 scope 後從 name 選單選取。\n"
            f"追蹤／提醒另存，編號不適用於記憶；取消用 `/{prefix}-track cancel:編號`"
            f" 或 `/{prefix}-remind cancel:編號`。刪除記憶不會清除既有對話脈絡。"
        )
        return "\n\n".join(sections)

    @app_commands.describe(scope="只看個人或伺服器；留空＝兩者都列")
    @app_commands.choices(scope=SCOPE_CHOICES)
    async def memory_command(
        self, interaction: discord.Interaction, scope: app_commands.Choice[str] | None = None
    ) -> None:
        reason = self._access(interaction.guild_id, interaction.channel, interaction.channel_id)
        if reason:
            await interaction.response.send_message(reason, ephemeral=True)
            return
        text = self._memory_text(
            interaction.guild_id, interaction.user.id, scope.value if scope else ""
        )
        # Every chunk, so a long personal index no longer pushes the server section off the end.
        chunks = split_discord_message(text)
        await interaction.response.send_message(chunks[0], ephemeral=True)
        for chunk in chunks[1:]:
            await interaction.followup.send(chunk, ephemeral=True)

    async def model_options(self, provider: str, current: str) -> list[app_commands.Choice[str]]:
        """Autocomplete for the model option: the provider's models, filtered by what the member
        typed. A router's list is its live catalog (image-capable first)."""
        if provider in ROUTER_BACKENDS:
            if not ROUTERS[provider].enabled(self.config):
                return []  # no key (no URL for the local server): the provider is not offered
            options = [
                router_choice(provider, m.id, f"{m.name}{'（看圖）' if m.image else ''}")
                for m in await self.catalogs[provider].free_models()
            ]
        elif provider == GROK:
            options = [
                grok_choice(m.id, f"{m.name}（{'／'.join(m.efforts) or '無 effort'}）")
                for m in await grok.models(self.config)
            ]
        else:
            options = [c for c in choices(self.config.codex_model) if c.backend == provider]
        needle = current.strip().lower()
        matched = [
            c
            for c in options
            if not needle or needle in c.value.lower() or needle in c.label.lower()
        ]
        return [app_commands.Choice(name=c.label[:100], value=c.value) for c in matched[:25]]

    async def _provider_default(self, provider: str):
        """The model a provider picked alone stands for: Codex's one model, DEFAULT_MODEL's Grok
        model when the plan still offers it (else the catalog's first), AGY_DEFAULT_FAMILY on
        Antigravity. None for the routers, whose free models come and go (the member picks one),
        and when Grok's catalog cannot be read or the Antigravity default is gone."""
        if provider == CODEX:
            return parse_choice(f"{CODEX}:{self.config.codex_model}", self.config.codex_model)
        if provider == GROK:
            catalog = await grok.models(self.config)
            if not catalog:
                return None
            default = self._choice(self.config.default_model)
            if default.backend == GROK and grok.cached_model(default.family) is not None:
                return default
            return grok_choice(catalog[0].id, catalog[0].name)  # the default left the plan
        if provider == AGY and AGY_DEFAULT_FAMILY in AGY_FAMILIES:
            label = AGY_FAMILIES[AGY_DEFAULT_FAMILY][0]
            return ModelChoice(f"{AGY}:{AGY_DEFAULT_FAMILY}", label, AGY, AGY_DEFAULT_FAMILY)
        return None

    def _chosen_model(self, provider: str, model: str):
        """The ModelChoice for a typed or picked model value; None when it is not offered."""
        value = model.strip()
        prefixes = ("codex:", f"{AGY}:", f"{GROK}:", *(f"{b}:" for b in ROUTER_BACKENDS))
        if not value.startswith(prefixes):
            value = f"{provider}:{value}"  # typed bare id (router ids themselves contain ":")
        chosen = parse_choice(value, self.config.codex_model)
        if chosen.value != value:
            return None  # unknown Codex / Antigravity value fell back to the default
        if chosen.backend == GROK and grok.cached_model(chosen.family) is None:
            return None  # not in this plan's catalog
        if chosen.backend in ROUTER_BACKENDS:
            if self.catalogs[chosen.backend].get(chosen.family) is None:
                return None
        return chosen

    async def _change_model(
        self,
        guild_id: int | None,
        user_id: int,
        provider: str | None,
        model: str | None,
        effort: str | None,
    ) -> tuple[str, str]:
        """Save /model's provider, model and effort for the member: (the stored value, "").
        ("", why) when nothing was saved: a model the menu does not offer, or a provider alone
        with no default model to take; the caller says how to pick one. The operator CLI
        (admin_members) calls this too, so both apply the same checks."""
        stored = self.memory.get_model(guild_id, user_id)
        current = split_stored(stored)[0].split(":")[0]
        source = provider or current
        if model is not None:
            # A typed "<backend>:<model>" is checked against that backend's live list, which
            # need not be the list of the provider the member is on now.
            named = model.strip().split(":", 1)[0]
            listed = named if named == GROK or named in ROUTER_BACKENDS else source
            if listed in ROUTER_BACKENDS:
                await self.catalogs[listed].free_models()
            elif listed == GROK:
                await grok.models(self.config)
            chosen = self._chosen_model(source, model)
            if chosen is None:
                return "", f"沒有這個模型：`{model}`"
        elif provider is not None and current != source:
            # A provider alone used to only show the current setting and save nothing; a
            # member who picked Codex (it has one model) kept getting the default. Picking a
            # provider now means its default model.
            chosen = await self._provider_default(source)
            if chosen is None:
                return "", (
                    f"這個來源的{ROUTERS[source].models_word}會變動" if source in ROUTER_BACKENDS
                    else "Grok 的模型清單暫時讀不到" if source == GROK
                    else "這個來源沒有預設模型"
                )  # fmt: skip
        else:
            chosen = self._choice(stored or self.config.default_model)
        # A new provider without an effort starts at its default effort: the old provider's
        # level saved as-is would read differently from what /status shows.
        switched = chosen.backend != current
        level = effort or ("" if switched else split_stored(stored)[1])
        value = f"{chosen.value}|{level}" if level else chosen.value
        self.memory.set_model(guild_id, user_id, value)
        LOGGER.info("Model set guild=%s user=%s value=%s", guild_id, user_id, value)
        return value, ""

    @app_commands.describe(
        provider="模型來源；只選來源＝用它的預設模型；全部留空＝查看目前設定",
        model="模型（打字篩選；OpenRouter 只列免費模型）",
        effort="這個模型的預設推理強度（/inmu-king 的 effort 可臨時覆蓋）",
        clear="設為 True 清除，回到預設模型",
    )
    @app_commands.choices(provider=PROVIDER_CHOICES, effort=EFFORT_CHOICES)
    @app_commands.autocomplete(model=_model_autocomplete)
    async def model_command(
        self,
        interaction: discord.Interaction,
        provider: app_commands.Choice[str] | None = None,
        model: str | None = None,
        effort: app_commands.Choice[str] | None = None,
        clear: bool = False,
    ) -> None:
        reason = self._access(interaction.guild_id, interaction.channel, interaction.channel_id)
        if reason:
            await interaction.response.send_message(reason, ephemeral=True)
            return
        guild_id, user_id = interaction.guild_id, interaction.user.id
        stored = self.memory.get_model(guild_id, user_id)
        if clear:
            cleared = self.memory.clear_model(guild_id, user_id)
            message = "已清除，回到預設模型。" if cleared else "你沒有設定模型。"
            LOGGER.info("Model cleared guild=%s user=%s had=%s", guild_id, user_id, bool(stored))
        elif model is not None or effort is not None or provider is not None:
            value, why = await self._change_model(
                guild_id,
                user_id,
                provider.value if provider else None,
                model,
                effort.value if effort else None,
            )
            if not value:
                how = (
                    "。請從清單裡選（打字可篩選）。"
                    if model is not None
                    else "，請在 model 欄從清單選一個（打字可篩選）。"
                )
                await interaction.response.send_message(why + how, ephemeral=True)
                return
            message = f"已設定：{self._describe(*split_stored(value))}"
        else:
            current = stored or self.config.default_model
            origin = "" if stored else "（預設）"
            message = f"目前{origin}：{self._describe(*split_stored(current))}"
        await interaction.response.send_message(message, ephemeral=True)

    def _style_status(self, guild_id: int | None, user_id: int) -> str:
        """Style and persona are two independent settings, so both are reported every time: the
        pair is what decides how an answer comes out, and one of them used to move on its own."""
        style = self.memory.get_style(guild_id, user_id)
        persona = "關閉（不帶角色）" if self.memory.get_persona_off(guild_id, user_id) else "保留"
        return f"風格：{style or '無（用預設）'}\n人設：{persona}"

    @app_commands.describe(
        text="你的回覆風格（例如：條列、少於 100 字、用英文）；留空＝查看目前設定",
        clear="設為 True 清除個人風格，回到預設",
        upload=f"設為 True 開啟上傳視窗，用一個 .md 檔當個人風格（上限 {STYLE_MAX_CHARS} 字）",
        persona="要不要保留伺服器人設；不選＝維持現狀",
    )
    @app_commands.choices(persona=PERSONA_CHOICES)
    async def style_command(
        self,
        interaction: discord.Interaction,
        text: str | None = None,
        clear: bool = False,
        upload: bool = False,
        persona: str | None = None,
    ) -> None:
        reason = self._access(interaction.guild_id, interaction.channel, interaction.channel_id)
        if reason:
            await interaction.response.send_message(reason, ephemeral=True)
            return
        guild_id, user_id = interaction.guild_id, interaction.user.id
        if upload and (clear or (text and text.strip())):
            await interaction.response.send_message(
                "上傳檔案時不要同時給 text 或 clear，一次做一件事。", ephemeral=True
            )
            return
        if upload:
            # A modal must be the first response to the interaction, so nothing else happens here.
            await interaction.response.send_modal(
                StyleModal(
                    lambda body: self.memory.set_style(guild_id, user_id, body), STYLE_MAX_CHARS
                )
            )
            return
        changed = False
        if persona is not None:
            self.memory.set_persona_off(guild_id, user_id, persona == "off")
            changed = True
        if clear:
            self.memory.clear_style(guild_id, user_id)
            changed = True
        elif text and text.strip():
            self.memory.set_style(guild_id, user_id, text)
            changed = True
        head = "已更新。" if changed else "目前設定："
        message = f"{head}\n{self._style_status(guild_id, user_id)}"
        await interaction.response.send_message(split_discord_message(message)[0], ephemeral=True)

    async def reset_command(self, interaction: discord.Interaction) -> None:
        reason = self._access(interaction.guild_id, interaction.channel, interaction.channel_id)
        if reason:
            await interaction.response.send_message(reason, ephemeral=True)
            return
        key = ThreadStore.key(interaction.guild_id, interaction.channel_id, interaction.user.id)
        forgot = self.threads.forget(key)
        await interaction.response.send_message(
            "已清除你在此頻道的對話脈絡。" if forgot else "此頻道沒有你的進行中對話。",
            ephemeral=True,
        )

    @app_commands.describe(
        prompt="要交給 Codex 的問題",
        effort="選填：推理強度（預設 Medium）",
        image="選填：一張圖片，或一份檔案（PDF／文字／程式碼）讓 Bot 讀",
        new="選填：忽略之前的對話，從頭開始",
    )
    @app_commands.choices(
        effort=[
            app_commands.Choice(name=label, value=value)
            for value, label in REASONING_EFFORTS.items()
        ]
    )
    async def codex_command(
        self,
        interaction: discord.Interaction,
        prompt: str,
        effort: app_commands.Choice[str] | None = None,
        image: discord.Attachment | None = None,
        new: bool = False,
    ) -> None:
        attachments = [image] if image is not None else []
        prompt = prompt.strip()
        # Unset means the member's own /model effort (or DEFAULT_MODEL's), not Codex's default.
        effort_value = (
            effort.value
            if effort is not None
            else self._member_effort(interaction.guild_id, interaction.user.id)
        )
        reason = self._access(
            interaction.guild_id, interaction.channel, interaction.channel_id
        ) or self._validate(prompt, attachments)
        if reason:
            await interaction.response.send_message(reason, ephemeral=True)
            return

        key = ThreadStore.key(interaction.guild_id, interaction.channel_id, interaction.user.id)
        plain = self.memory.get_persona_off(interaction.guild_id, interaction.user.id)
        model = self._model(interaction.guild_id, interaction.user.id)
        resume = "" if new else self.threads.current(key, plain=plain, model=model)
        await interaction.response.defer(thinking=True)
        # An effort given here is the member's for this message: the conversation stays on its
        # model (no Jev, no move) and only the effort changes.
        routed = await self._route(
            key,
            None,
            interaction.guild_id,
            interaction.user.id,
            prompt,
            attachments,
            plain,
            fresh=new,
            effort=effort.value if effort is not None else "",
        )
        if routed is not None:
            resume = routed.resume

        placeholder = SlashPlaceholder(interaction)
        late = asyncio.create_task(placeholder.retire_when_late())
        try:
            result = await self._run_tracked(
                key,
                interaction.user.id,
                placeholder.show,
                lambda on_video_slow, on_delta: self._answer(
                    prompt,
                    attachments,
                    interaction.guild_id,
                    interaction.user.id,
                    effort_value,
                    resume,
                    on_video_slow=on_video_slow,
                    on_delta=on_delta,
                    channel_id=interaction.channel_id,
                    **self._routed_kw(routed),
                ),
            )
        finally:
            late.cancel()
        if result is None:
            LOGGER.info(
                "Cancelled slash guild=%s user=%s", interaction.guild_id, interaction.user.id
            )
            return
        if routed is not None:
            self._log_route(routed, result)
        # Discord does not echo slash command inputs, so quote the question above the answer.
        # The model named is the one that answered, a fallback spare included.
        tag = self._slash_tag(routed, result, model, effort_value)
        target = result.via
        answer = result.text
        if routed is not None and routed.no_x and target is not None and target.backend != GROK:
            answer += f"\n\n{NO_X_NOTE}"
        reply = format_reply(
            prompt,
            answer,
            has_image=bool(attachments),
            effort=tag,
            resumed=result.resumed,
        )
        chunks = split_discord_message(reply)
        sent_id = None
        view = self._answer_view(interaction.guild_id, interaction.user.id, prompt, result)
        try:
            if interaction_expired(interaction) and interaction.channel is not None:
                await placeholder.retire()
                sent = await interaction.channel.send(
                    chunks[0], files=self._files(result), view=view
                )
                send_rest = interaction.channel.send
            else:
                sent = await interaction.edit_original_response(
                    content=chunks[0], attachments=self._files(result), view=view
                )
                send_rest = interaction.followup.send
            sent_id = sent.id
            for chunk in chunks[1:]:
                await send_rest(chunk)
        except discord.HTTPException:
            LOGGER.exception(
                "Delivery failed for /%s guild=%s", self.config.command_prefix, interaction.guild_id
            )
            try:
                await interaction.followup.send(
                    "回覆送出失敗（Discord 錯誤），請再問一次。", ephemeral=True
                )
            except discord.HTTPException:
                pass
        finally:
            remove_dir(result.generated_dir)
        self._remember(
            key,
            result.thread_id,
            sent_id,
            plain,
            *self._routed_memo(routed, result, model),
        )
        # channel too: a watch is created in whichever channel the member spoke in, and without
        # it a request cannot be traced back to where its side effects landed.
        LOGGER.info(
            "Completed slash guild=%s channel=%s user=%s",
            interaction.guild_id,
            interaction.channel_id,
            interaction.user.id,
        )

    # ----- @mention entry point --------------------------------------------------------------

    async def on_message(self, message: discord.Message) -> None:
        try:
            await self._collect_emoji(message)  # any message: the next one's "previous"
        except Exception:
            LOGGER.exception("Emoji sample failed channel=%s", message.channel.id)
        if message.author.bot or self.user is None:
            return
        # Member-visible link cleaning runs on every message in an allowed channel, not just on
        # @mentions — a link nobody asks about is exactly the one whose params should go.
        try:
            await self._linkclean(message)
        except Exception:
            LOGGER.exception(
                "linkclean failed channel=%s; continuing mention handling", message.channel.id
            )
        if self.user not in message.mentions:
            return
        # A reply to a cleaned-link repost pings the Bot too; only a typed @ is a question.
        typed = mentions_explicitly(message.content, self.user.id)
        if not typed and await self._replies_to_repost(message):
            return
        guild_id = message.guild.id if message.guild else None
        reason = self._access(guild_id, message.channel, message.channel.id)
        if reason:
            await message.reply(reason, mention_author=False)
            return
        prompt = strip_mention(message.content, self.user.id)
        attachments = list(message.attachments)
        # Replying to another member's message (e.g. one that carries a picture) points the Bot
        # at it: its text and images are folded into this request.
        quoted = await self._referenced(message)
        if quoted is not None and (
            quoted.author != self.user or await self._replies_to_repost(message)
        ):
            usable = [
                a
                for a in quoted.attachments
                if validate_attachment(a.content_type, a.filename, a.size, self.config)[0]
            ]
            attachments.extend(usable)
            images = [a for a in usable if (a.content_type or "").startswith("image/")]
            prompt = with_quoted_message(
                prompt, quoted.author.display_name, quoted.content, len(images)
            )
        reason = self._validate(prompt, attachments)
        if reason:
            await message.reply(reason, mention_author=False)
            return
        previews = await self._previews(message, quoted)

        # Replying to one of the Bot's answers continues that exact thread; otherwise the member's
        # most recent thread in this channel (within the TTL) is continued.
        key = ThreadStore.key(guild_id, message.channel.id, message.author.id)
        plain = self.memory.get_persona_off(guild_id, message.author.id)
        model = self._model(guild_id, message.author.id)
        replied_to = message.reference.message_id if message.reference else None
        resume = self.threads.by_message(
            replied_to, plain=plain, model=model
        ) or self.threads.current(key, plain=plain, model=model)
        placeholder: list[discord.Message] = []

        async def show(text: str, view) -> None:
            try:
                if placeholder:
                    await placeholder[0].edit(content=text, view=view)
                else:
                    placeholder.append(await message.reply(text, view=view, mention_author=False))
            except discord.HTTPException:
                pass

        async with message.channel.typing():
            routed = await self._route(
                key, replied_to, guild_id, message.author.id, prompt, attachments, plain
            )
            if routed is not None:
                resume = routed.resume
            result = await self._run_tracked(
                key,
                message.author.id,
                show,
                lambda on_video_slow, on_delta: self._answer(
                    prompt,
                    attachments,
                    guild_id,
                    message.author.id,
                    resume=resume,
                    previews=previews,
                    on_video_slow=on_video_slow,
                    on_delta=on_delta,
                    channel_id=message.channel.id,
                    **self._routed_kw(routed),
                ),
            )
        if result is None:
            LOGGER.info("Cancelled @mention guild=%s user=%s", guild_id, message.author.id)
            return
        text = result.text
        if routed is not None:
            self._log_route(routed, result)
            text = self._routed_text(routed, result)
        chunks = split_discord_message(text)
        sent_id = None
        try:
            files = self._files(result)
            view = self._answer_view(guild_id, message.author.id, prompt, result)
            if placeholder:
                sent = await placeholder[0].edit(content=chunks[0], attachments=files, view=view)
            else:
                sent = await message.reply(chunks[0], files=files, view=view, mention_author=False)
            sent_id = sent.id
            for chunk in chunks[1:]:
                await message.channel.send(chunk)
        except discord.HTTPException:
            LOGGER.exception("Delivery failed for @mention guild=%s", guild_id)
            try:
                await message.channel.send("回覆送出失敗（Discord 錯誤），請再問一次。")
            except discord.HTTPException:
                pass
        finally:
            remove_dir(result.generated_dir)
        self._remember(
            key,
            result.thread_id,
            sent_id,
            plain,
            *self._routed_memo(routed, result, model),
        )
        LOGGER.info(
            "Completed @mention guild=%s channel=%s user=%s",
            message.guild.id,
            message.channel.id,
            message.author.id,
        )


def configure_logging(config: Config) -> None:
    """stderr (so `docker logs` still works) plus, when LOG_DIR is set, a daily-rotating file in a
    bind-mounted host directory. `docker logs` only holds the container that is running now, so a
    rebuild takes the evidence with it; the host file is what an incident is read from afterwards.
    A log dir that cannot be written is a warning, never a reason not to start."""
    formatter = logging.Formatter("%(asctime)s %(levelname)s %(name)s %(message)s")
    root = logging.getLogger()
    root.setLevel(logging.INFO)
    stream = logging.StreamHandler()
    stream.setFormatter(formatter)
    root.addHandler(stream)
    if config.log_dir is None:
        return
    try:
        config.log_dir.mkdir(parents=True, exist_ok=True)
        rotating = logging.handlers.TimedRotatingFileHandler(
            config.log_dir / "bot.log",
            when="midnight",
            backupCount=config.log_keep_days,
            encoding="utf-8",
        )
    except OSError as error:
        root.warning("LOG_DIR %s unusable (%s); logging to stderr only", config.log_dir, error)
        return
    rotating.suffix = "%Y-%m-%d"  # bot.log.2026-09-13 — find an incident by the date it happened
    rotating.setFormatter(formatter)
    root.addHandler(rotating)


def main() -> None:
    config = load_config()
    configure_logging(config)
    # Before the client, which fingerprints these files to decide which threads may resume.
    instructions.compose_workspaces(config)
    client = DiscordCodexClient(config)
    client.run(config.discord_token, log_handler=None)
