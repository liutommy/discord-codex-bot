from __future__ import annotations

import asyncio
import hashlib
import io
import logging
import logging.handlers
import re
import tempfile
import time
from collections.abc import Awaitable, Callable, Sequence
from datetime import datetime as _dt
from pathlib import Path

import aiohttp
import discord
from discord import app_commands

from . import apis, embedfix, gemini, sandbox, search
from .access import check_access
from .agy import run_agy
from .alerts import Alerter, login_watch
from .announce import announce_once
from .attachments import (
    download_attachment,
    extract_text,
    remove_dir,
    remove_request_dir,
    sweep_forever,
    validate_attachment,
)
from .backends import (
    AGY,
    OPENROUTER,
    ORCAROUTER,
    ROUTER_BACKENDS,
    choices,
    fallback_target,
    parse_choice,
    resolve,
    router_choice,
    run_batch,
    split_stored,
)
from .backup import backup_forever, export_memory_zip
from .codex import (
    CodexFallbackError,
    CodexResult,
    CodexServerOverloaded,
    CodexUnauthorized,
    CodexUsageLimit,
    codex_login_status,
    run_codex,
)
from .config import REASONING_EFFORTS, Config, load_config
from .consolidate import consolidate_forever
from .harvest import harvest_forever
from .help import render_guide, render_sheet
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
)
from .memory import (
    RECALL_TAG,
    SCOPES,
    SEARCH_TAG,
    MemoryLimits,
    MemoryStore,
    PermanentMemory,
    extract_memory_tags,
    extract_read_requests,
)
from .openrouter import ROUTERS, Catalog, run_router
from .output import format_reply, split_discord_message, truncate
from .queue import QueueFullError, SerialQueue
from .reminders import (
    ReminderStore,
    describe,
    extract_reminder_tags,
    parse_when,
    reminder_loop,
    render_pending,
)
from .summary import DEFAULT_MESSAGES, MAX_MESSAGES, render_transcript, since, summary_prompt
from .threads import ThreadStore
from .tracking import (
    INTEREST_POLICY,
    OutboxMessage,
    ProviderError,
    Source,
    TrackerStore,
    TwitchFetcher,
    WebFetcher,
    YouTubeFetcher,
    extract_track_tags,
    parse_twitch_locator,
    parse_web_locator,
    parse_youtube_locator,
    render_watches,
    tracking_loop,
)
from .ui import AnswerButton, AnswerView, CancelView, recover_exchange
from .usage import probe_rate_limits

LOGGER = logging.getLogger(__name__)
QUEUE_FULL_MESSAGE = "目前排隊已滿，請稍後再試。"
FAILURE_MESSAGE = (
    "Codex 執行失敗。請用 /{prefix}-status 檢查登入狀態，並通知 Bot 管理者查看 container log。"
)


SCOPE_CHOICES = [app_commands.Choice(name=label, value=value) for value, label in SCOPES.items()]
LINKCLEAN_CHOICES = [app_commands.Choice(name="查詢", value="status")] + [
    app_commands.Choice(name=label, value=value) for value, label in MODES.items()
]
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
# Why the last request fell back, for /status only. type() match, not isinstance: these are
# sibling subclasses, and anything not listed (CodexServiceError, future kinds) reads as generic.
_FALLBACK_LABEL = {
    CodexUsageLimit: "額度用完",
    CodexServerOverloaded: "模型滿載",
    CodexUnauthorized: "登入失效",
}
STREAM_SHOW_CHARS = 1900
CANCELLED = "⛔ 已取消。"
PROVIDER_CHOICES = [
    app_commands.Choice(name="Codex", value="codex"),
    app_commands.Choice(name="Antigravity（Gemini／Claude）", value=AGY),
    app_commands.Choice(name="OpenRouter（免費模型）", value=OPENROUTER),
    app_commands.Choice(name="OrcaRouter（免費模型）", value=ORCAROUTER),
]
FREE_MODEL_NOTE = "免費模型可能隨時不穩或下架，失敗時請換一個。"


def tracking_provider(locator: str) -> str:
    """Return the provider for a supported locator without doing network I/O. The specific
    providers are tried first; anything else that is a public page is tracked as a page."""
    for provider, parser in (
        ("youtube", parse_youtube_locator),
        ("twitch", parse_twitch_locator),
        ("web", parse_web_locator),
    ):
        try:
            parser(locator)
        except ValueError:
            continue
        return provider
    raise ValueError("追蹤需要 YouTube 頻道、Twitch 頻道，或任何 http(s) 網頁網址。")


def tracking_message(message: OutboxMessage, source_label: str = "") -> str:
    """One notification in the Bot's own voice: who, what, and the link. Category, confidence
    and reasoning are log material — read on request, never pushed at the member. Social text
    is escaped before Discord sees it."""
    item, decision, watch = message.item, message.decision, message.watch
    title = discord.utils.escape_mentions(item.title.strip())[:300] or "（無標題）"
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
        config.output_style_path,
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
    """Fold a replied-to member message into the prompt so the model sees what was pointed at."""
    quoted = " ".join(content.split())
    parts = []
    if quoted:
        parts.append(f"（後輩回覆了 {author} 的訊息：「{quoted}」）")
    if image_count:
        parts.append(f"（那則訊息附了 {image_count} 張圖，已一併附上）")
    if not parts:
        return prompt
    return "\n".join(parts + [prompt or "請看這則訊息。"])


def request_only(answer: str) -> bool:
    """True when the reply is nothing but read/fetch tags — the protocol for asking the Bot to
    read something. A tag embedded in prose (quoted from a fetched page, say) is just text."""
    rest = answer
    for tag in (SEARCH_TAG, RECALL_TAG, FETCH_TAG, search.WEB_TAG, sandbox.RUN_TAG, apis.API_TAG):
        rest = tag.sub("", rest)
    return bool(answer.strip()) and not rest.strip()


def _for_other(item: dict) -> bool:
    """A reminder set for someone other than the member who set it."""
    return item.get("target_id", item["user_id"]) != item["user_id"]


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


class DiscordCodexClient(discord.Client):
    def __init__(self, config: Config) -> None:
        intents = discord.Intents.none()
        intents.guilds = True
        # @mention entry point: message_content is needed to read the question and its images.
        intents.guild_messages = True
        intents.message_content = True
        intents.emojis_and_stickers = True  # keeps guild.emojis populated for the 記住 button
        super().__init__(intents=intents)
        self.config = config
        prefix = config.command_prefix
        self.tree = app_commands.CommandTree(self)
        self.queue = SerialQueue(config.max_queued_jobs)
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
        self.permanent = PermanentMemory(config.permanent_memory_dir, limits)
        self.active: dict[str, asyncio.Task] = {}  # in-flight request per member+channel
        self.alerts = Alerter(self, config)
        # The most recent request answered on the spare backend: (when, why, which model).
        # Shown by /status only; the answer itself carries no notice.
        self._last_fallback: tuple[float, CodexFallbackError, str] | None = None
        self._emoji_cache: dict[int, dict[str, discord.PartialEmoji]] = {}
        self.reminders = ReminderStore(config.codex_home / "reminders.json")
        self.apis = apis.load_registry(config.apis_path)
        self.openrouter = Catalog(config)
        self.orcarouter = Catalog(config, ROUTERS[ORCAROUTER])
        self.catalogs = {OPENROUTER: self.openrouter, ORCAROUTER: self.orcarouter}
        self.tracker = TrackerStore(config.tracking_db_path) if config.tracking_enabled else None
        self.linkclean = SwitchStore(config.codex_home / "linkclean.sqlite3")
        self.youtube_tracker = YouTubeFetcher(config.youtube_api_key)
        self.twitch_tracker = TwitchFetcher(config.twitch_client_id, config.twitch_client_secret)
        self.web_tracker = WebFetcher(self._read_page)
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
                description="查看／選擇／清除你要用的模型（Codex、Antigravity、OpenRouter、OrcaRouter）",
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
                description="追蹤 YouTube／Twitch；留空列出，或用編號取消／切換正式提醒",
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
                name=f"{prefix}-embedfix",
                description="本伺服器的預覽修正開關：社群貼文連結換成 Discord 能預覽的版本",
                callback=self.embedfix_command,
            )
        )

    async def setup_hook(self) -> None:
        self.add_dynamic_items(AnswerButton)  # answer buttons keep working across restarts
        self._sweeper = self.loop.create_task(sweep_forever(self.config))
        self._backup_loop = self.loop.create_task(backup_forever(self.config))
        self._reminder_loop = self.loop.create_task(
            reminder_loop(self.reminders, self._fire_reminder, 30)
        )
        self._consolidator = self.loop.create_task(
            consolidate_forever(self.memory, self.config, self.queue.run)
        )
        self._harvest_wakeup = asyncio.Event()
        self._harvester = self.loop.create_task(
            harvest_forever(
                self.threads, self.memory, self.config, self.queue.run, self._harvest_wakeup
            )
        )
        if self.tracker is not None:
            self._tracking_loop = self.loop.create_task(self._tracking_forever())
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

    async def on_ready(self) -> None:
        LOGGER.info("Discord bot ready as %s", self.user)
        await self.warm_emojis()
        LOGGER.info("%s", await codex_login_status(self.config))
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
        try:
            await announce_once(self, self.config)
        except Exception:
            LOGGER.exception("Announcement pass failed")

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

    async def _read_page(self, url: str) -> str:
        """One page as text, with its links kept inline. No out_dir: a background poll has
        nobody to show a screenshot to, and nothing to clean up afterwards."""
        text, _shots = await fetch_or_render(url, self.config, None, False)
        return text

    async def _fetch_tracking_source(self, source: Source):
        fetcher = {
            "youtube": self.youtube_tracker,
            "twitch": self.twitch_tracker,
            "web": self.web_tracker,
        }.get(source.provider)
        if fetcher is None:
            raise ProviderError(f"unsupported provider: {source.provider}")
        return await fetcher.fetch(source)

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
            (c.name, c.description, [p.name for p in c.parameters])
            for c in sorted(self.tree.get_commands(), key=lambda c: c.name)
        ]

    def help_sheet(self) -> str:
        """What this Bot can do, generated from the registered commands so it never drifts from
        the code; injected as HELP so the model can explain itself truthfully."""
        sheet = render_sheet(self.config.command_prefix, self._command_rows())
        doc = apis.render_doc(self.apis)
        return f"{sheet}\n{doc}" if doc else sheet

    def help_guide(self) -> str:
        """The detailed member guide behind /<prefix>-help (same source as the model's sheet)."""
        return render_guide(self.config.command_prefix, [row[0] for row in self._command_rows()])

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
            blocks = await asyncio.wait_for(
                asyncio.shield(task), self.config.video_interim_after_seconds
            )
        except TimeoutError:
            if on_video_slow is not None:
                await on_video_slow()
            blocks = await task
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
    ) -> CodexResult:
        """Run one validated request through the member's backend; always returns text.
        `on_delta` receives the accumulated answer while a backend streams it (agy, routers)."""
        stored = self.memory.get_model(guild_id, user_id)
        choice = parse_choice(stored, self.config.codex_model)
        target = resolve(
            choice, effort or split_stored(stored)[1] or self.config.codex_reasoning_effort
        )

        spare = fallback_target(
            self.config.codex_fallback_model,
            self.config.codex_model,
            self.config.codex_reasoning_effort,
        )
        fell_back = False

        async def run_on(via, text: str, **kw) -> CodexResult:
            if via.backend == AGY:
                kw.pop("effort", None)
                return await run_agy(text, self.config, via.model, **kw)
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
            nonlocal target, fell_back
            kw.setdefault("on_delta", on_delta)
            try:
                return await run_on(target, text, **kw)
            except CodexFallbackError as unavailable:
                if spare is None:
                    raise
                # Answer on the spare backend for the rest of this request. Switching mid-request
                # keeps the recall loop's resume ids on one backend, since a Codex thread id means
                # nothing to agy or a router.
                LOGGER.warning(
                    "Codex unavailable (%s: %s); answering with %s",
                    type(unavailable).__name__,
                    unavailable,
                    spare.model,
                )
                target, fell_back = spare, True
                self._last_fallback = (time.time(), unavailable, spare.model)
                if isinstance(unavailable, CodexUnauthorized):
                    # Quota comes back by itself; a lost login does not. Tell the operator now
                    # rather than let the spare hide it until the next periodic login check.
                    await self.alerts.login_lost("Codex", str(unavailable))
                kw.pop("resume", None)
                return await run_on(target, text, **kw)

        images: list[Path] = []
        self.config.attachment_dir.mkdir(parents=True, exist_ok=True)
        link_dir = Path(tempfile.mkdtemp(prefix="req-", dir=self.config.attachment_dir))
        deliver_dir = Path(tempfile.mkdtemp(prefix="req-", dir=self.config.attachment_dir))
        delivered: list[Path] = []  # sandbox-made files handed to the member with the answer
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
                    text = await asyncio.to_thread(extract_text, saved, self.config.link_max_chars)
                    documents.append(f'<FILE name="{attachment.filename}">\n{text}\n</FILE>')
                    remove_request_dir(saved)
            files = "\n\n".join(documents)
            permanent = self.permanent.index_text()
            pending = render_pending(self.reminders.for_user(user_id))
            tracked = self._tracked_lines(user_id)
            memory = "\n\n".join(
                section
                for section in (
                    f"[永久記憶索引]\n{permanent}" if permanent else "",
                    self.memory.render(guild_id, user_id),
                    f"[待辦提醒]\n{pending}" if pending else "",
                    f"[社群追蹤]\n{tracked}" if tracked else "",
                )
                if section
            )
            style = self.memory.get_style(guild_id, user_id)
            found = find_urls(prompt, self.config.link_max_urls)
            links, shots = await link_blocks(found, self.config, link_dir, previews)
            images.extend(shots)
            video = await self._understand_videos(found, link_dir, on_video_slow)
            links = "\n\n".join(block for block in (links, video) if block)
            result = await self.queue.run(
                lambda: turn(
                    prompt,
                    images=images,
                    resume=resume,
                    memory=memory,
                    personal_style=style,
                    links=links,
                    help=self.help_sheet(),
                    files=files,
                )
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
                queries = search.extract_web_queries(result.text)[:2]
                runs = sandbox.extract_runs(result.text) if sandbox.available(self.config) else []
                api_calls = apis.extract_api_calls(result.text) if self.apis else []
                if not wanted and not urls and not queries and not runs and not api_calls:
                    break
                blocks = [
                    f'<RESULT kind="{kind}" scope="{scope}" target="{target}">\n'
                    + self._read(kind, scope, guild_id, user_id, target, offset, lines)
                    + "\n</RESULT>"
                    for kind, scope, target, offset, lines in wanted
                ]
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
                result = await self.queue.run(
                    lambda text=recalled, thread=result.thread_id, imgs=tuple(extra): turn(
                        text + "\n\nNow answer the member's question.",
                        images=imgs,
                        resume=thread,
                        raw=True,
                        personal_style=style,
                    )
                )
            text, facts = extract_memory_tags(result.text)
            for scope, name, fact in facts:
                self.memory.add(scope, guild_id, user_id, name, fact)
            text = self._apply_reminder_tags(text, guild_id, channel_id, user_id)
            text = await self._apply_tracking_tags(text, guild_id, channel_id, user_id)
            await self.alerts.record_success(target.backend)
            generated_dir = result.generated_dir
            outgoing = tuple(result.images)  # not `images`: that list is cleaned up in finally
            if delivered:
                if generated_dir is None:
                    generated_dir = deliver_dir  # the caller removes it after sending
                else:
                    delivered = [self._keep_for_member(path, generated_dir) for path in delivered]
                outgoing += tuple(delivered)
            # A fallback is not announced in the answer (owner ruling 2026-09-14: the member
            # gains nothing from it; /status shows the last one). The thread id is dropped so
            # nothing tries to resume it back on Codex.
            return CodexResult(
                truncate(text, self.config.max_response_chars),
                outgoing,
                generated_dir,
                "" if fell_back else result.thread_id,
                result.resumed and not fell_back,
            )
        except QueueFullError:
            return CodexResult(QUEUE_FULL_MESSAGE)
        except Exception as error:
            LOGGER.exception("Codex request failed guild=%s", guild_id)
            await self.alerts.record_failure(target.backend, str(error))
            return CodexResult(FAILURE_MESSAGE.format(prefix=self.config.command_prefix))
        finally:
            for path in images:
                remove_request_dir(path)
            remove_dir(link_dir)
            if not delivered:
                remove_dir(deliver_dir)

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
        can_replace = (
            links_only
            and member is not None
            and message.channel.permissions_for(member).manage_messages
            and not message.attachments
            and not message.stickers
            and message.reference is None
            and message.thread is None
            and len(replacement) <= 2000
        )
        if can_replace:
            # Never remove the only copy: delivery must succeed before deletion.
            posted = await message.channel.send(
                replacement,
                allowed_mentions=discord.AllowedMentions(
                    users=[message.author], everyone=False, roles=False, replied_user=False
                ),
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
        chosen = parse_choice(value, self.config.codex_model)
        target = resolve(chosen, level or self.config.codex_reasoning_effort)
        text = f"{chosen.label} · {self._effort_label(target)} → `{target.model}`"
        if chosen.backend in ROUTER_BACKENDS:
            info = self.catalogs[chosen.backend].get(chosen.family)
            sees = "看得到" if info is None or info.image else "看不到"
            text += f"（免費，{sees}圖片）\n{FREE_MODEL_NOTE}"
        return text

    def _model(self, guild_id: int | None, user_id: int) -> str:
        return parse_choice(self.memory.get_model(guild_id, user_id), self.config.codex_model).value

    def _remember(
        self, key: str, thread_id: str, message_id: int | None, plain: bool, model: str
    ) -> None:
        """Record the thread; a switch retires the old one, so harvest it without waiting."""
        switched = self.threads.switched(key, thread_id)
        self.threads.remember(key, thread_id, message_id, plain=plain, model=model)
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
        view = CancelView(owner_id)
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
            if text.lstrip().startswith("<"):
                return
            now = time.monotonic()
            if now - state["at"] < STREAM_EDIT_SECONDS:
                return
            state["at"] = now
            await show(text[-STREAM_SHOW_CHARS:] + " ▌", view)

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
        plain = bool(self.memory.get_style(interaction.guild_id, user_id))
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
        if asked is not None:
            previews = await self._previews(asked, pointed)
        LOGGER.info(
            "Button redo guild=%s user=%s resume=%s quoted=%s",
            interaction.guild_id,
            user_id,
            bool(resume),
            pointed is not None,
        )
        result = await self._answer(
            prompt,
            [],
            interaction.guild_id,
            user_id,
            resume=resume,
            channel_id=interaction.channel_id,
            previews=previews,
        )
        sent = await self.send_answer(
            interaction.followup, question, result, interaction.guild_id, user_id
        )
        # Link the redo answer too, so replying to *it* continues the same thread.
        self._remember(key, result.thread_id, getattr(sent, "id", None), plain, model)

    async def send_answer(self, destination, prompt: str, result, guild_id, user_id):
        """Post an answer (with its buttons) through any `.send`-able destination — used by the
        🔁 button, which lands the new answer as a follow-up. Returns the message the destination
        handed back (when it does), so the caller can link it to the thread."""
        chunks = split_discord_message(result.text)
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
            if hours is not None:
                fetched = [
                    m
                    async for m in interaction.channel.history(
                        limit=MAX_MESSAGES, after=since(hours), oldest_first=True
                    )
                ]
            else:
                fetched = [m async for m in interaction.channel.history(limit=count)]
                fetched.reverse()
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
        chunks = split_discord_message(result.text)
        try:
            label = f"（最近 {hours} 小時）" if hours is not None else f"（最近 {len(fetched)} 則）"
            await interaction.edit_original_response(
                content=f"**頻道摘要{label}**\n{chunks[0]}", view=None
            )
            for chunk in chunks[1:]:
                await interaction.followup.send(chunk)
        except discord.HTTPException:
            LOGGER.exception("Summary delivery failed guild=%s", interaction.guild_id)
        finally:
            remove_dir(result.generated_dir)

    @staticmethod
    def _keep_for_member(path: Path, into: Path) -> Path:
        """Copy a sandbox-made file out of the per-request scratch dir so it survives cleanup
        until the answer (and the file) has been sent."""
        into.mkdir(parents=True, exist_ok=True)
        target = into / path.name
        target.write_bytes(path.read_bytes())
        return target

    def _apply_reminder_tags(
        self, text: str, guild_id: int | None, channel_id: int | None, user_id: int
    ) -> str:
        """Create / cancel the reminders the model asked for and append a confirmation."""
        clean, creates, cancels = extract_reminder_tags(text)
        if not creates and not cancels:
            return text
        notes: list[str] = []
        for when_text, what, target in creates:
            due = parse_when(when_text)
            if due is None:
                notes.append(f"（時間「{when_text}」看不懂，提醒沒有設）")
                continue
            if channel_id is None:
                notes.append("（這裡沒辦法設提醒）")
                continue
            item = self.reminders.add(guild_id, channel_id, user_id, due, what, target)
            if isinstance(item, str):
                notes.append(f"（提醒沒有設：{item}）")
            else:
                whom = f"提醒 <@{target}>" if target and target != user_id else "提醒你"
                notes.append(f"⏰ 已設定 #{item['id']}：{describe(due)} {whom}：{item['text']}")
        for reminder_id in cancels:
            done = self.reminders.cancel(user_id, reminder_id)
            notes.append(
                f"⛔ 已取消提醒 #{reminder_id}" if done else f"（找不到你的提醒 #{reminder_id}）"
            )
        return f"{clean}\n\n" + "\n".join(notes)

    async def _add_watch(
        self,
        locator: str,
        interest: str,
        mention_ids: Sequence[int],
        guild_id: int | None,
        channel_id: int | None,
        user_id: int,
        interval_minutes: int = 0,
    ) -> tuple[str, object | None]:
        """Resolve one source and start watching it. Returns (message, watch or None);
        shared by the slash command and the <track> tag so both enforce the same limits."""
        store = self.tracker
        if store is None:
            return "社群追蹤尚未啟用；管理者需設定 TRACKING_ENABLED=true 與 provider 憑證。", None
        if guild_id is None or channel_id is None:
            return "這裡沒辦法建立追蹤。", None
        policy = interest.strip() or INTEREST_POLICY
        if len(locator) > 500 or len(policy) > 4000:
            return "網址或追蹤條件太長。", None
        if len(store.watches(user_id=user_id, active_only=True)) >= (
            self.config.tracking_max_per_user
        ):
            return f"每人最多 {self.config.tracking_max_per_user} 個追蹤。", None
        try:
            provider = tracking_provider(locator)
        except ValueError as error:
            return str(error), None
        if provider == "youtube" and not self.config.youtube_api_key:
            return "管理者尚未設定 YOUTUBE_API_KEY。", None
        if provider == "twitch" and not (
            self.config.twitch_client_id and self.config.twitch_client_secret
        ):
            return "管理者尚未設定 TWITCH_CLIENT_ID／TWITCH_CLIENT_SECRET。", None
        resolver = {
            "youtube": self.youtube_tracker,
            "twitch": self.twitch_tracker,
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
            )
        except (ProviderError, aiohttp.ClientError, TimeoutError, ValueError) as error:
            LOGGER.warning("Tracking source resolution failed (%s)", type(error).__name__)
            return "無法讀取這個來源；請確認網址與 provider 憑證後再試。", None
        label = str(state.get("title") or state.get("login") or external_id)
        also = (
            "，也會 @ " + "、".join(f"<@{uid}>" for uid in watch.mention_ids)
            if watch.mention_ids
            else ""
        )
        return (
            f"已新增追蹤 #{watch.id}：{provider} · {label}{also}\n"
            f"從現在起有符合的新內容就會通知（每 {watch.interval_minutes} 分鐘判斷一次，"
            "建立之前的舊內容不算）。判斷過程用 "
            f"/{self.config.command_prefix}-track log:{watch.id} 查，只有你看得到。",
            watch,
        )

    async def _apply_tracking_tags(
        self, text: str, guild_id: int | None, channel_id: int | None, user_id: int
    ) -> str:
        """Create / promote / demote the watches the model asked for and append a confirmation.
        Deleting is deliberately not a tag: it discards the watch's baseline, and rebuilding one
        costs a whole classification pass, so it stays an explicit slash command."""
        clean, adds, intervals = extract_track_tags(text)
        if not adds and not intervals:
            return text
        store = self.tracker
        if store is None:
            return f"{clean}\n\n（社群追蹤尚未啟用。）"
        notes: list[str] = []
        for locator, interest, who, every in adds:
            note, watch = await self._add_watch(
                locator, interest, who, guild_id, channel_id, user_id, every
            )
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
        LOGGER.info(
            "Tracking tags guild=%s channel=%s user=%s adds=%d every=%d",
            guild_id,
            channel_id,
            user_id,
            len(adds),
            len(intervals),
        )
        return f"{clean}\n\n" + "\n".join(notes)

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
        target = item.get("target_id") or item["user_id"]
        by = f"（{'<@' + str(item['user_id']) + '>'} 設的）" if target != item["user_id"] else ""
        await channel.send(
            f"⏰ <@{target}> 提醒：{item['text']}{by}",
            allowed_mentions=discord.AllowedMentions(users=True, everyone=False, roles=False),
        )

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
                    + (f" → <@{i['target_id']}>" if _for_other(i) else "")
                    + f" — {i['text']}"
                    for i in mine
                )
            )
        await interaction.response.send_message(message, ephemeral=True)

    @app_commands.describe(
        source="YouTube 頻道或 Twitch 頻道網址；留空列出你的追蹤",
        interest="選填：你特別想知道的內容；留空使用預設重大事件政策",
        cancel="取消你的追蹤編號",
        log="看判斷紀錄：這個追蹤最近判斷了什麼、為什麼提醒或不提醒（只有你看得到）",
    )
    async def track_command(
        self,
        interaction: discord.Interaction,
        source: str | None = None,
        interest: str | None = None,
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
        if actions > 1 or (interest and not source):
            await interaction.response.send_message(
                "新增、取消、看判斷紀錄一次只能做一件；interest 必須和 source 一起使用。",
                ephemeral=True,
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
                        f"#{watch.id} {tracked.provider} · {tracked.locator}"
                        f"（每 {watch.interval_minutes} 分鐘判斷一次）"
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
        if not self._can_linkclean_admin(interaction):
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
        if not self._can_linkclean_admin(interaction):
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

    def _can_linkclean_admin(self, interaction: discord.Interaction) -> bool:
        """Who may flip the guild switch: the server owner, a guild admin, or an id the
        operator listed in LINKCLEAN_ADMIN_IDS (covers a delegated owner who is not the
        Discord account that owns the server)."""
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
        stored = self.memory.get_model(guild_id, user_id)
        chosen = parse_choice(stored, self.config.codex_model)
        level = split_stored(stored)[1]
        target = resolve(chosen, level or self.config.codex_reasoning_effort)
        origin = "你設定" if stored else "預設"
        model_line = f"模型：{chosen.label} · 強度 {self._effort_label(target)}（{origin}）"
        if chosen.backend in ROUTER_BACKENDS:
            info = self.catalogs[chosen.backend].get(chosen.family)
            if info is not None and not info.image:
                model_line += " · 看不到圖"
        style = self.memory.get_style(guild_id, user_id)
        style_line = f"風格：{style[:60]}" if style else "風格：無（用預設）"

        key = ThreadStore.key(guild_id, channel_id, user_id)
        entry = self.threads.live_entry(key)
        if entry is None:
            thread_line = "續接：無，下一句會新開對話"
        else:
            minutes = max(0, int((time.time() - float(entry["at"])) // 60))
            previous = parse_choice(str(entry.get("model", "")), self.config.codex_model).label
            if self.threads.current(key, plain=bool(style), model=chosen.value):
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
        spare = fallback_target(
            self.config.codex_fallback_model,
            self.config.codex_model,
            self.config.codex_reasoning_effort,
        )
        if limits is not None:
            codex += (
                f" · 額度 5h {limits.primary_used_percent:.0f}%"
                f" / 7d {limits.secondary_used_percent:.0f}%"
            )
            # "Falling back right now" needs no memory: a spent window means the next request
            # goes to the spare. Derived from the live probe, so it survives a restart and covers
            # requests this process has not seen (the batch jobs fall back the same way).
            if max(limits.primary_used_percent, limits.secondary_used_percent) >= 100:
                codex += (
                    f"\n　└ 額度已達上限，現在的請求改用 {spare.model} 回答"
                    if spare is not None
                    else "\n　└ 額度已達上限，且沒有設定備援模型"
                )
        else:
            codex += " · 額度讀不到"
        if self._last_fallback is not None:
            when, why, model = self._last_fallback
            mins = max(0, int((time.time() - when) // 60))
            label = _FALLBACK_LABEL.get(type(why), "服務異常")
            codex += f"\n　└ 最近一次備援：{mins} 分鐘前{label}，改用 {model} 回答"
        routers = []
        for backend, catalog in self.catalogs.items():
            if ROUTERS[backend].api_key(self.config):
                count = len(await catalog.free_models())
                routers.append(f"{ROUTERS[backend].label} {count} 個免費模型")
        system = [
            f"Codex：{codex}",
            "Antigravity：可選（Gemini／Claude）",
            " · ".join(routers) if routers else "OpenRouter／OrcaRouter：未設定",
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
        typed. OpenRouter's list is the live free-model catalog (image-capable first)."""
        if provider in ROUTER_BACKENDS:
            if not ROUTERS[provider].api_key(self.config):
                return []  # no key: the provider is not offered
            options = [
                router_choice(provider, m.id, f"{m.name}{'（看圖）' if m.image else ''}")
                for m in await self.catalogs[provider].free_models()
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

    def _chosen_model(self, provider: str, model: str):
        """The ModelChoice for a typed or picked model value; None when it is not offered."""
        value = model.strip()
        if not value.startswith(("codex:", f"{AGY}:", *(f"{b}:" for b in ROUTER_BACKENDS))):
            value = f"{provider}:{value}"  # typed bare id (router ids themselves contain ":")
        chosen = parse_choice(value, self.config.codex_model)
        if chosen.value != value:
            return None  # unknown Codex / Antigravity value fell back to the default
        if chosen.backend in ROUTER_BACKENDS:
            if self.catalogs[chosen.backend].get(chosen.family) is None:
                return None
        return chosen

    @app_commands.describe(
        provider="模型來源；留空＝查看目前設定",
        model="模型（打字篩選；OpenRouter 只列免費模型）",
        effort="這個模型的預設推理強度（/inmu-king 的 effort 可臨時覆蓋）",
        clear="設為 True 清除，回到預設（Codex）",
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
        elif model is not None or effort is not None:
            source = provider.value if provider else split_stored(stored)[0].split(":")[0]
            if model is not None:
                if source in ROUTER_BACKENDS:
                    await self.catalogs[source].free_models()
                chosen = self._chosen_model(source, model)
                if chosen is None:
                    await interaction.response.send_message(
                        f"沒有這個模型：`{model}`。請從清單裡選（打字可篩選）。", ephemeral=True
                    )
                    return
            else:
                chosen = parse_choice(stored, self.config.codex_model)
            level = effort.value if effort else split_stored(stored)[1]
            value = f"{chosen.value}|{level}" if level else chosen.value
            self.memory.set_model(guild_id, user_id, value)
            message = f"已設定：{self._describe(chosen.value, level)}"
        else:
            message = f"目前：{self._describe(*split_stored(stored))}"
        await interaction.response.send_message(message, ephemeral=True)

    @app_commands.describe(
        text="你的回覆風格（例如：條列、少於 100 字、用英文）；留空＝查看目前設定",
        clear="設為 True 清除個人風格，回到預設",
    )
    async def style_command(
        self,
        interaction: discord.Interaction,
        text: str | None = None,
        clear: bool = False,
    ) -> None:
        reason = self._access(interaction.guild_id, interaction.channel, interaction.channel_id)
        if reason:
            await interaction.response.send_message(reason, ephemeral=True)
            return
        guild_id, user_id = interaction.guild_id, interaction.user.id
        if clear:
            cleared = self.memory.clear_style(guild_id, user_id)
            message = "已清除個人風格，回到預設。" if cleared else "你沒有設定個人風格。"
        elif text and text.strip():
            self.memory.set_style(guild_id, user_id, text)
            message = f"已設定個人風格：\n{text.strip()}"
        else:
            current = self.memory.get_style(guild_id, user_id)
            message = f"目前個人風格：\n{current}" if current else "目前使用預設風格。"
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
        effort_value = effort.value if effort is not None else self.config.codex_reasoning_effort
        reason = self._access(
            interaction.guild_id, interaction.channel, interaction.channel_id
        ) or self._validate(prompt, attachments)
        if reason:
            await interaction.response.send_message(reason, ephemeral=True)
            return

        key = ThreadStore.key(interaction.guild_id, interaction.channel_id, interaction.user.id)
        plain = bool(self.memory.get_style(interaction.guild_id, interaction.user.id))
        model = self._model(interaction.guild_id, interaction.user.id)
        resume = "" if new else self.threads.current(key, plain=plain, model=model)
        await interaction.response.defer(thinking=True)

        async def show(text: str, view) -> None:
            try:
                await interaction.edit_original_response(content=text, view=view)
            except discord.HTTPException:
                pass

        result = await self._run_tracked(
            key,
            interaction.user.id,
            show,
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
            ),
        )
        if result is None:
            LOGGER.info(
                "Cancelled slash guild=%s user=%s", interaction.guild_id, interaction.user.id
            )
            return
        # Discord does not echo slash command inputs, so quote the question above the answer.
        target = resolve(parse_choice(model, self.config.codex_model), effort_value)
        shown = self._effort_label(target)
        reply = format_reply(
            prompt,
            result.text,
            has_image=bool(attachments),
            effort=shown if target.backend == "codex" else f"{target.model} · {shown}",
            resumed=result.resumed,
        )
        chunks = split_discord_message(reply)
        sent_id = None
        try:
            sent = await interaction.edit_original_response(
                content=chunks[0],
                attachments=self._files(result),
                view=self._answer_view(interaction.guild_id, interaction.user.id, prompt, result),
            )
            sent_id = sent.id
            for chunk in chunks[1:]:
                await interaction.followup.send(chunk)
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
        self._remember(key, result.thread_id, sent_id, plain, model)
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
        if not mentions_explicitly(message.content, self.user.id) and await self._replies_to_repost(
            message
        ):
            return  # a reply to a cleaned-link repost pings the Bot; only a typed @ is a question
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
        plain = bool(self.memory.get_style(guild_id, message.author.id))
        model = self._model(guild_id, message.author.id)
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
                ),
            )
        if result is None:
            LOGGER.info("Cancelled @mention guild=%s user=%s", guild_id, message.author.id)
            return
        chunks = split_discord_message(result.text)
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
        self._remember(key, result.thread_id, sent_id, plain, model)
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
    client = DiscordCodexClient(config)
    client.run(config.discord_token, log_handler=None)
