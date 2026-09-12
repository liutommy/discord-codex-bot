from __future__ import annotations

import asyncio
import hashlib
import logging
import re
from collections.abc import Sequence
from pathlib import Path

import discord
from discord import app_commands

from .access import check_access
from .agy import run_agy
from .announce import announce_once
from .attachments import (
    download_image,
    remove_dir,
    remove_request_dir,
    sweep_forever,
    validate_image,
)
from .backends import AGY, choices, parse_choice, resolve
from .codex import CodexResult, codex_login_status, run_codex
from .config import REASONING_EFFORTS, Config, load_config
from .consolidate import consolidate_forever
from .harvest import harvest_forever
from .memory import (
    SCOPES,
    MemoryLimits,
    MemoryStore,
    PermanentMemory,
    extract_memory_tags,
    extract_read_requests,
)
from .output import format_reply, split_discord_message, truncate
from .queue import QueueFullError, SerialQueue
from .threads import ThreadStore

LOGGER = logging.getLogger(__name__)
QUEUE_FULL_MESSAGE = "目前排隊已滿，請稍後再試。"
FAILURE_MESSAGE = (
    "Codex 執行失敗。請用 /{prefix}-status 檢查登入狀態，並通知 Bot 管理者查看 container log。"
)


SCOPE_CHOICES = [app_commands.Choice(name=label, value=value) for value, label in SCOPES.items()]
# Discord allows 25 choices per option; Codex + 14 agy slugs = 15. Built with the default
# Codex model name, which is also what load_config() falls back to.
MODEL_CHOICES = [
    app_commands.Choice(name=c.label[:100], value=c.value) for c in choices("gpt-5.6-luna")
]


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
                description="查看／選擇／清除你要用的模型（Codex 或 Antigravity 的模型）",
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
                name=f"{prefix}-status",
                description="檢查 Bot 的 Codex 模型與訂閱登入狀態",
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

    async def setup_hook(self) -> None:
        self._sweeper = self.loop.create_task(sweep_forever(self.config))
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
            return f"一次最多 {self.config.max_attachments} 張圖片。"
        for attachment in attachments:
            suffix = validate_image(attachment.content_type, attachment.size, self.config)
            if not suffix.startswith("."):
                return suffix
        return ""

    async def _answer(
        self,
        prompt: str,
        attachments: Sequence[discord.Attachment],
        guild_id: int | None,
        user_id: int,
        effort: str = "",
        resume: str = "",
    ) -> CodexResult:
        """Run one validated request through the member's backend; always returns text."""
        choice = parse_choice(self.memory.get_model(guild_id, user_id), self.config.codex_model)
        target = resolve(choice, effort or self.config.codex_reasoning_effort)

        async def turn(text: str, **kw) -> CodexResult:
            if target.backend == AGY:
                kw.pop("effort", None)
                return await run_agy(text, self.config, target.model, **kw)
            return await run_codex(text, self.config, effort=target.effort, **kw)

        images: list[Path] = []
        try:
            for attachment in attachments:
                suffix = validate_image(attachment.content_type, attachment.size, self.config)
                images.append(await download_image(attachment, suffix, self.config))
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
            result = await self.queue.run(
                lambda: turn(
                    prompt, images=images, resume=resume, memory=memory, personal_style=style
                )
            )
            # On-demand reads (search snippets / paged recall): the Bot executes the request and
            # feeds the result back into the same thread. Bounded by MEMORY_RECALL_ROUNDS.
            for _ in range(self.config.memory_recall_rounds):
                wanted = extract_read_requests(result.text)
                if not wanted:
                    break
                recalled = "\n\n".join(
                    f'<RESULT kind="{kind}" scope="{scope}" target="{target}">\n'
                    + self._read(kind, scope, guild_id, user_id, target, offset, lines)
                    + "\n</RESULT>"
                    for kind, scope, target, offset, lines in wanted
                )
                result = await self.queue.run(
                    lambda text=recalled, thread=result.thread_id: turn(
                        text + "\n\nNow answer the member's question.",
                        resume=thread,
                        raw=True,
                        personal_style=style,
                    )
                )
            text, facts = extract_memory_tags(result.text)
            for scope, name, fact in facts:
                self.memory.add(scope, guild_id, user_id, name, fact)
            return CodexResult(
                truncate(text, self.config.max_response_chars),
                result.images,
                result.generated_dir,
                result.thread_id,
                result.resumed,
            )
        except QueueFullError:
            return CodexResult(QUEUE_FULL_MESSAGE)
        except Exception:
            LOGGER.exception("Codex request failed guild=%s", guild_id)
            return CodexResult(FAILURE_MESSAGE.format(prefix=self.config.command_prefix))
        finally:
            for path in images:
                remove_request_dir(path)

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

    def _model(self, guild_id: int | None, user_id: int) -> str:
        return parse_choice(self.memory.get_model(guild_id, user_id), self.config.codex_model).value

    def _remember(
        self, key: str, thread_id: str, message_id: int, plain: bool, model: str
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

    async def status_command(self, interaction: discord.Interaction) -> None:
        reason = self._access(interaction.guild_id, interaction.channel, interaction.channel_id)
        if reason:
            await interaction.response.send_message(reason, ephemeral=True)
            return
        status = await codex_login_status(self.config)
        default_label = REASONING_EFFORTS[self.config.codex_reasoning_effort]
        await interaction.response.send_message(
            f"{status}\n模型：{self.config.codex_model}\n"
            f"預設推理強度：{default_label}"
            f"（/{self.config.command_prefix} 可選 {'、'.join(REASONING_EFFORTS.values())}）",
            ephemeral=True,
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

    async def memory_command(self, interaction: discord.Interaction) -> None:
        reason = self._access(interaction.guild_id, interaction.channel, interaction.channel_id)
        if reason:
            await interaction.response.send_message(reason, ephemeral=True)
            return
        text = self.memory.render(interaction.guild_id, interaction.user.id) or "目前沒有記憶。"
        await interaction.response.send_message(
            split_discord_message(text)[0], ephemeral=True
        )

    @app_commands.describe(
        model="要使用的模型；留空＝查看目前設定",
        clear="設為 True 清除，回到預設（Codex）",
    )
    @app_commands.choices(model=MODEL_CHOICES)
    async def model_command(
        self,
        interaction: discord.Interaction,
        model: app_commands.Choice[str] | None = None,
        clear: bool = False,
    ) -> None:
        reason = self._access(interaction.guild_id, interaction.channel, interaction.channel_id)
        if reason:
            await interaction.response.send_message(reason, ephemeral=True)
            return
        guild_id, user_id = interaction.guild_id, interaction.user.id
        if clear:
            cleared = self.memory.clear_model(guild_id, user_id)
            message = "已清除，回到預設模型。" if cleared else "你沒有設定模型。"
        elif model is not None:
            chosen = parse_choice(model.value, self.config.codex_model)
            self.memory.set_model(guild_id, user_id, chosen.value)
            message = f"已設定模型：{chosen.label}"
        else:
            stored = self.memory.get_model(guild_id, user_id)
            message = f"目前模型：{parse_choice(stored, self.config.codex_model).label}"
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
        image="選填：一張要讓 Codex 看的圖片",
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
        result = await self._answer(
            prompt, attachments, interaction.guild_id, interaction.user.id, effort_value, resume
        )
        # Discord does not echo slash command inputs, so quote the question above the answer.
        target = resolve(parse_choice(model, self.config.codex_model), effort_value)
        shown = REASONING_EFFORTS.get(target.effort, target.effort) if target.effort else "固定"
        reply = format_reply(
            prompt,
            result.text,
            has_image=bool(attachments),
            effort=f"{target.model} · {shown}" if target.backend == AGY else shown,
            resumed=result.resumed,
        )
        chunks = split_discord_message(reply)
        try:
            sent = await interaction.edit_original_response(
                content=chunks[0], attachments=self._files(result)
            )
            for chunk in chunks[1:]:
                await interaction.followup.send(chunk)
        finally:
            remove_dir(result.generated_dir)
        self._remember(key, result.thread_id, sent.id, plain, model)
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
            images = [a for a in quoted.attachments if (a.content_type or "").startswith("image/")]
            attachments.extend(images)
            prompt = with_quoted_message(
                prompt, quoted.author.display_name, quoted.content, len(images)
            )
        reason = self._validate(prompt, attachments)
        if reason:
            await message.reply(reason, mention_author=False)
            return

        # Replying to one of the Bot's answers continues that exact thread; otherwise the member's
        # most recent thread in this channel (within the TTL) is continued.
        key = ThreadStore.key(guild_id, message.channel.id, message.author.id)
        plain = bool(self.memory.get_style(guild_id, message.author.id))
        replied_to = message.reference.message_id if message.reference else None
        model = self._model(guild_id, message.author.id)
        resume = self.threads.by_message(
            replied_to, plain=plain, model=model
        ) or self.threads.current(key, plain=plain, model=model)
        async with message.channel.typing():
            result = await self._answer(
                prompt, attachments, guild_id, message.author.id, resume=resume
            )
        chunks = split_discord_message(result.text)
        try:
            sent = await message.reply(chunks[0], files=self._files(result), mention_author=False)
            for chunk in chunks[1:]:
                await message.channel.send(chunk)
        finally:
            remove_dir(result.generated_dir)
        self._remember(key, result.thread_id, sent.id, plain, model)
        LOGGER.info("Completed @mention guild=%s user=%s", message.guild.id, message.author.id)


def main() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )
    config = load_config()
    client = DiscordCodexClient(config)
    client.run(config.discord_token, log_handler=None)
