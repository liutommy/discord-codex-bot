from __future__ import annotations

import logging
import re
from collections.abc import Sequence
from pathlib import Path

import discord
from discord import app_commands

from .access import check_access
from .attachments import (
    download_image,
    remove_dir,
    remove_request_dir,
    sweep_forever,
    validate_image,
)
from .codex import CodexResult, codex_login_status, run_codex
from .config import REASONING_EFFORTS, Config, load_config
from .output import format_reply, split_discord_message, truncate
from .queue import QueueFullError, SerialQueue
from .threads import ThreadStore

LOGGER = logging.getLogger(__name__)
QUEUE_FULL_MESSAGE = "目前排隊已滿，請稍後再試。"
FAILURE_MESSAGE = (
    "Codex 執行失敗。請用 /codex-status 檢查登入狀態，並通知 Bot 管理者查看 container log。"
)


def strip_mention(content: str, bot_id: int) -> str:
    """Remove every <@id> / <@!id> mention of the bot so only the question remains."""
    return re.sub(rf"<@!?{bot_id}>", "", content).strip()


class DiscordCodexClient(discord.Client):
    def __init__(self, config: Config) -> None:
        intents = discord.Intents.none()
        intents.guilds = True
        # @mention entry point: message_content is needed to read the question and its images.
        intents.guild_messages = True
        intents.message_content = True
        super().__init__(intents=intents)
        self.config = config
        self.tree = app_commands.CommandTree(self)
        self.queue = SerialQueue(config.max_queued_jobs)
        self.threads = ThreadStore(
            config.codex_home / "discord_threads.json", config.thread_ttl_minutes * 60
        )
        self.tree.add_command(
            app_commands.Command(
                name="codex",
                description="詢問此伺服器的 Codex agent",
                callback=self.codex_command,
            )
        )
        self.tree.add_command(
            app_commands.Command(
                name="codex-status",
                description="檢查 Bot 的 Codex 模型與訂閱登入狀態",
                callback=self.status_command,
            )
        )
        self.tree.add_command(
            app_commands.Command(
                name="codex-reset",
                description="忘掉你在此頻道的對話脈絡，下一題從頭開始",
                callback=self.reset_command,
            )
        )

    async def setup_hook(self) -> None:
        self._sweeper = self.loop.create_task(sweep_forever(self.config))
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
        effort: str = "",
        resume: str = "",
    ) -> CodexResult:
        """Run one validated request through Codex; always returns something to post."""
        images: list[Path] = []
        try:
            for attachment in attachments:
                suffix = validate_image(attachment.content_type, attachment.size, self.config)
                images.append(await download_image(attachment, suffix, self.config))
            result = await self.queue.run(
                lambda: run_codex(prompt, self.config, images, effort, resume)
            )
            return CodexResult(
                truncate(result.text, self.config.max_response_chars),
                result.images,
                result.generated_dir,
                result.thread_id,
                result.resumed,
            )
        except QueueFullError:
            return CodexResult(QUEUE_FULL_MESSAGE)
        except Exception:
            LOGGER.exception("Codex request failed guild=%s", guild_id)
            return CodexResult(FAILURE_MESSAGE)
        finally:
            for path in images:
                remove_request_dir(path)

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
            f"預設推理強度：{default_label}（/codex 可選 {'、'.join(REASONING_EFFORTS.values())}）",
            ephemeral=True,
        )

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
        resume = "" if new else self.threads.current(key)
        await interaction.response.defer(thinking=True)
        result = await self._answer(
            prompt, attachments, interaction.guild_id, effort_value, resume
        )
        # Discord does not echo slash command inputs, so quote the question above the answer.
        reply = format_reply(
            prompt,
            result.text,
            has_image=bool(attachments),
            effort=REASONING_EFFORTS[effort_value],
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
        self.threads.remember(key, result.thread_id, sent.id)
        LOGGER.info("Completed /codex guild=%s user=%s", interaction.guild_id, interaction.user.id)

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
        reason = self._validate(prompt, message.attachments)
        if reason:
            await message.reply(reason, mention_author=False)
            return

        # Replying to one of the Bot's answers continues that exact thread; otherwise the member's
        # most recent thread in this channel (within the TTL) is continued.
        key = ThreadStore.key(guild_id, message.channel.id, message.author.id)
        replied_to = message.reference.message_id if message.reference else None
        resume = self.threads.by_message(replied_to) or self.threads.current(key)
        async with message.channel.typing():
            result = await self._answer(prompt, message.attachments, guild_id, resume=resume)
        chunks = split_discord_message(result.text)
        try:
            sent = await message.reply(chunks[0], files=self._files(result), mention_author=False)
            for chunk in chunks[1:]:
                await message.channel.send(chunk)
        finally:
            remove_dir(result.generated_dir)
        self.threads.remember(key, result.thread_id, sent.id)
        LOGGER.info("Completed @mention guild=%s user=%s", message.guild.id, message.author.id)


def main() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )
    config = load_config()
    client = DiscordCodexClient(config)
    client.run(config.discord_token, log_handler=None)
