from __future__ import annotations

import asyncio
import hashlib
import io
import logging
import re
import tempfile
import time
from collections.abc import Awaitable, Callable, Sequence
from datetime import datetime as _dt
from pathlib import Path

import discord
from discord import app_commands

from . import gemini, search
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
    parse_choice,
    resolve,
    router_choice,
    split_stored,
)
from .backup import backup_forever, export_memory_zip
from .codex import CodexResult, codex_login_status, run_codex
from .config import REASONING_EFFORTS, Config, load_config
from .consolidate import consolidate_forever
from .harvest import harvest_forever
from .help import render_guide, render_sheet
from .links import (
    FETCH_TAG,
    Preview,
    extract_fetch_tags,
    fetch_or_render,
    find_urls,
    has_video,
    link_blocks,
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
from .reminders import ReminderStore, describe, parse_when, reminder_loop
from .summary import DEFAULT_MESSAGES, MAX_MESSAGES, render_transcript, since, summary_prompt
from .threads import ThreadStore
from .ui import AnswerButton, AnswerView, CancelView, recover_exchange

LOGGER = logging.getLogger(__name__)
QUEUE_FULL_MESSAGE = "目前排隊已滿，請稍後再試。"
FAILURE_MESSAGE = (
    "Codex 執行失敗。請用 /{prefix}-status 檢查登入狀態，並通知 Bot 管理者查看 container log。"
)


SCOPE_CHOICES = [app_commands.Choice(name=label, value=value) for value, label in SCOPES.items()]
# Discord allows 25 choices per option; Codex + 14 agy slugs = 15. Built with the default
# Codex model name, which is also what load_config() falls back to.
EFFORT_CHOICES = [app_commands.Choice(name=v, value=k) for k, v in REASONING_EFFORTS.items()]
VIDEO_INTERIM = "🎬 影片較長，前輩正在看，稍等…"
THINKING = "🤔 思考中…"
STREAM_EDIT_SECONDS = 1.5  # Discord edits per placeholder while an answer streams in
STREAM_SHOW_CHARS = 1900
CANCELLED = "⛔ 已取消。"
PROVIDER_CHOICES = [
    app_commands.Choice(name="Codex", value="codex"),
    app_commands.Choice(name="Antigravity（Gemini／Claude）", value=AGY),
    app_commands.Choice(name="OpenRouter（免費模型）", value=OPENROUTER),
    app_commands.Choice(name="OrcaRouter（免費模型）", value=ORCAROUTER),
]
FREE_MODEL_NOTE = "免費模型可能隨時不穩或下架，失敗時請換一個。"


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
    for tag in (SEARCH_TAG, RECALL_TAG, FETCH_TAG, search.WEB_TAG):
        rest = tag.sub("", rest)
    return bool(answer.strip()) and not rest.strip()


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


class DiscordCodexClient(discord.Client):
    def __init__(self, config: Config) -> None:
        intents = discord.Intents.none()
        intents.guilds = True
        # @mention entry point: message_content is needed to read the question and its images.
        intents.guild_messages = True
        intents.message_content = True
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
        self.reminders = ReminderStore(config.codex_home / "reminders.json")
        self.openrouter = Catalog(config)
        self.orcarouter = Catalog(config, ROUTERS[ORCAROUTER])
        self.catalogs = {OPENROUTER: self.openrouter, ORCAROUTER: self.orcarouter}
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
                description=f"刪除一則記憶（用 /{prefix}-memory 看名稱）",
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

    async def on_ready(self) -> None:
        LOGGER.info("Discord bot ready as %s", self.user)
        LOGGER.info("%s", await codex_login_status(self.config))
        LOGGER.info("Alerts go to user %s", await self.alerts.resolve_owner() or "(none)")
        if not getattr(self, "_login_watch", None):
            self._login_watch = asyncio.create_task(login_watch(
                self.alerts, self.config, codex_login_status,
                self.config.alert_login_check_minutes * 60,
            ))
        try:
            await announce_once(self, self.config)
        except Exception:
            LOGGER.exception("Announcement pass failed")

    # ----- shared pipeline -------------------------------------------------------------------

    def _access(self, guild_id: int | None, channel: object, channel_id: int | None) -> str:
        """Empty string when allowed, otherwise the user-facing rejection reason."""
        decision = check_access(
            guild_id=guild_id,
            channel_id=channel_id,
            parent_channel_id=getattr(channel, "parent_id", None),
            config=self.config,
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
        return render_sheet(self.config.command_prefix, self._command_rows())

    def help_guide(self) -> str:
        """The detailed member guide behind /<prefix>-help (same source as the model's sheet)."""
        return render_guide(
            self.config.command_prefix, [row[0] for row in self._command_rows()]
        )

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
                *(understand_video(u, self.config, out_dir / f"vid{i}")
                  for i, u in enumerate(targets))
            )
            return [f'<VIDEO url="{u}">\n{d}\n</VIDEO>' for u, d in zip(targets, done, strict=True)
                    if d]

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
    ) -> CodexResult:
        """Run one validated request through the member's backend; always returns text.
        `on_delta` receives the accumulated answer while a backend streams it (agy, routers)."""
        stored = self.memory.get_model(guild_id, user_id)
        choice = parse_choice(stored, self.config.codex_model)
        target = resolve(
            choice, effort or split_stored(stored)[1] or self.config.codex_reasoning_effort
        )

        async def turn(text: str, **kw) -> CodexResult:
            kw.setdefault("on_delta", on_delta)
            if target.backend == AGY:
                kw.pop("effort", None)
                return await run_agy(text, self.config, target.model, **kw)
            if target.backend in ROUTER_BACKENDS:
                catalog = self.catalogs[target.backend]
                await catalog.free_models()  # image / effort capability lookup
                return await run_router(
                    ROUTERS[target.backend], text, self.config, target.model,
                    effort=target.effort, catalog=catalog, **kw,
                )
            return await run_codex(text, self.config, effort=target.effort, **kw)

        images: list[Path] = []
        self.config.attachment_dir.mkdir(parents=True, exist_ok=True)
        link_dir = Path(tempfile.mkdtemp(prefix="req-", dir=self.config.attachment_dir))
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
                    text = await asyncio.to_thread(
                        extract_text, saved, self.config.link_max_chars
                    )
                    documents.append(f'<FILE name="{attachment.filename}">\n{text}\n</FILE>')
                    remove_request_dir(saved)
            files = "\n\n".join(documents)
            permanent = self.permanent.index_text()
            memory = "\n\n".join(
                section
                for section in (
                    f"[永久記憶索引]\n{permanent}" if permanent else "",
                    self.memory.render(guild_id, user_id),
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
            for _ in range(self.config.memory_recall_rounds):
                if not request_only(result.text):
                    break  # an answer that merely quotes a tag (e.g. from a page) is an answer
                wanted = extract_read_requests(result.text)
                urls = extract_fetch_tags(result.text)[: self.config.link_max_urls]
                queries = search.extract_web_queries(result.text)[:2]
                if not wanted and not urls and not queries:
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
                extra: list[Path] = []
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
            await self.alerts.record_success(target.backend)
            return CodexResult(
                truncate(text, self.config.max_response_chars),
                result.images,
                result.generated_dir,
                result.thread_id,
                result.resumed,
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
        guild = self.get_guild(guild_id) if guild_id else None
        wanted = self.config.remember_emoji_name
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
        result = await self._answer(question, [], interaction.guild_id, user_id, resume="")
        await self.send_answer(
            interaction.followup, question, result, interaction.guild_id, user_id
        )

    async def send_answer(self, destination, prompt: str, result, guild_id, user_id) -> None:
        """Post an answer (with its buttons) through any `.send`-able destination — used by the
        🔁 button, which lands the new answer as a follow-up."""
        chunks = split_discord_message(result.text)
        try:
            await destination.send(
                chunks[0], files=self._files(result),
                view=self._answer_view(guild_id, user_id, prompt, result),
            )
            for chunk in chunks[1:]:
                await destination.send(chunk)
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
                fetched = [m async for m in interaction.channel.history(
                    limit=MAX_MESSAGES, after=since(hours), oldest_first=True
                )]
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
            key, interaction.user.id, show,
            lambda on_video_slow, on_delta: self._answer(
                prompt, [], interaction.guild_id, interaction.user.id, resume="",
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

    async def _fire_reminder(self, item: dict) -> None:
        channel = self.get_channel(item["channel_id"]) or await self.fetch_channel(
            item["channel_id"]
        )
        await channel.send(
            f"⏰ <@{item['user_id']}> 提醒：{item['text']}",
            allowed_mentions=discord.AllowedMentions(users=True, everyone=False, roles=False),
        )

    @app_commands.describe(
        when="什麼時候：30分鐘後、2小時後、明天 9:30、後天下午3點、21:00、9/15 14:30",
        text="到時要提醒的內容",
        cancel="要取消的提醒編號（用留空的 /指令 查看）",
    )
    async def remind_command(
        self,
        interaction: discord.Interaction,
        when: str | None = None,
        text: str | None = None,
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
                item = self.reminders.add(
                    interaction.guild_id, interaction.channel_id, user_id, due, text
                )
                message = item if isinstance(item, str) else (
                    f"好，{describe(due)} 在這個頻道提醒你：{item['text']}（#{item['id']}）"
                )
        elif when or text:
            message = "要同時給 when（時間）和 text（內容）。"
        else:
            mine = self.reminders.for_user(user_id)
            message = "你沒有提醒。" if not mine else "你的提醒：\n" + "\n".join(
                f"#{i['id']} {describe(_dt.fromisoformat(i['due']))}"
                f" — {i['text']}" for i in mine
            )
        await interaction.response.send_message(message, ephemeral=True)

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

    async def _status_text(
        self, guild_id: int | None, channel_id: int | None, user_id: int
    ) -> str:
        """Two sections: what this member has set (model, style, whether the next request
        continues a thread, memory sizes) and what the system offers. No usage figures: the
        Codex quota is the operator's, and the routers' free limits are not published."""
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
                thread_line = (
                    f"續接：{minutes} 分鐘前的對話是 {previous}／另一種風格，下一句會新開"
                )

        def scope_line(label: str, scope: str, owner: int | None, limit: int) -> str:
            shown = len(self.memory.entries(scope, guild_id, owner))
            total = len(self.memory.all_entries(scope, guild_id, owner))
            used = self.memory.usage_bytes(scope, guild_id, owner)
            text = f"{label} {total} 條 / {used // 1024} KB（上限 {limit // 1_000_000} MB）"
            return text + (f"，{total - shown} 條已推到 archive" if total > shown else "")

        memory_line = "記憶：" + " · ".join((
            scope_line("個人", "user", user_id, self.config.memory_user_max_bytes),
            scope_line("伺服器", "guild", None, self.config.memory_guild_max_bytes),
            f"永久 {self.permanent.topic_count()} 主題",
        ))

        codex = await codex_login_status(self.config)
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
            f" · 搜尋：{'／'.join(n for n, _ in search.providers(self.config)) or '關'}",
        ]
        return "\n".join((
            "【你的設定】", model_line, style_line, thread_line, memory_line,
            "", "【系統】", *system,
        ))

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

    @app_commands.describe(scope="個人或伺服器", name="記憶名稱（用 -memory 指令查看）")
    @app_commands.choices(scope=SCOPE_CHOICES)
    async def forget_command(
        self, interaction: discord.Interaction, scope: app_commands.Choice[str], name: str
    ) -> None:
        reason = self._access(interaction.guild_id, interaction.channel, interaction.channel_id)
        if reason:
            await interaction.response.send_message(reason, ephemeral=True)
            return
        forgot = self.memory.forget(scope.value, interaction.guild_id, interaction.user.id, name)
        await interaction.response.send_message(
            f"已刪除「{name}」。" if forgot else f"找不到「{name}」。", ephemeral=True
        )

    def _memory_text(self, guild_id: int | None, user_id: int, scope: str = "") -> str:
        """Index listing for one scope, or both; the archive is not listed (search finds it)."""
        if scope:
            owner = user_id if scope == "user" else None
            text = self.memory.index_text(scope, guild_id, owner)
            label = SCOPES[scope]
            return f"[{label}記憶索引]\n{text}" if text else f"目前沒有{label}記憶。"
        return self.memory.render(guild_id, user_id) or "目前沒有記憶。"

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
            key, interaction.user.id, show,
            lambda on_video_slow, on_delta: self._answer(
                prompt, attachments, interaction.guild_id, interaction.user.id, effort_value,
                resume, on_video_slow=on_video_slow, on_delta=on_delta,
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
                content=chunks[0], attachments=self._files(result),
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
        LOGGER.info("Completed slash guild=%s user=%s", interaction.guild_id, interaction.user.id)

    # ----- @mention entry point --------------------------------------------------------------

    async def on_message(self, message: discord.Message) -> None:
        if message.author.bot or self.user is None or self.user not in message.mentions:
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
        if quoted is not None and quoted.author != self.user:
            usable = [
                a for a in quoted.attachments
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
        replied_to = message.reference.message_id if message.reference else None
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
                    placeholder.append(
                        await message.reply(text, view=view, mention_author=False)
                    )
            except discord.HTTPException:
                pass

        async with message.channel.typing():
            result = await self._run_tracked(
                key, message.author.id, show,
                lambda on_video_slow, on_delta: self._answer(
                    prompt, attachments, guild_id, message.author.id, resume=resume,
                    previews=previews, on_video_slow=on_video_slow, on_delta=on_delta,
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
        LOGGER.info("Completed @mention guild=%s user=%s", message.guild.id, message.author.id)


def main() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )
    config = load_config()
    client = DiscordCodexClient(config)
    client.run(config.discord_token, log_handler=None)
