from __future__ import annotations

import logging
import re
from collections.abc import Sequence
from pathlib import Path

import discord
from discord import app_commands

from .access import check_access
from .attachments import download_image, remove_request_dir, sweep_forever, validate_image
from .codex import codex_login_status, run_codex
from .config import REASONING_EFFORTS, Config, load_config
from .output import format_reply, split_discord_message, truncate
from .queue import QueueFullError, SerialQueue

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
    ) -> str:
        """Run one validated request through Codex; always returns text to post."""
        images: list[Path] = []
        try:
            for attachment in attachments:
                suffix = validate_image(attachment.content_type, attachment.size, self.config)
                images.append(await download_image(attachment, suffix, self.config))
            answer = await self.queue.run(
                lambda: run_codex(prompt, self.config, images, effort)
            )
            return truncate(answer, self.config.max_response_chars)
        except QueueFullError:
            return QUEUE_FULL_MESSAGE
        except Exception:
            LOGGER.exception("Codex request failed guild=%s", guild_id)
            return FAILURE_MESSAGE
        finally:
            for path in images:
                remove_request_dir(path)

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

    @app_commands.describe(
        prompt="要交給 Codex 的問題",
        effort="選填：推理強度（預設 Medium）",
        image="選填：一張要讓 Codex 看的圖片",
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

        await interaction.response.defer(thinking=True)
        answer = await self._answer(prompt, attachments, interaction.guild_id, effort_value)
        # Discord does not echo slash command inputs, so quote the question above the answer.
        reply = format_reply(
            prompt, answer, has_image=bool(attachments), effort=REASONING_EFFORTS[effort_value]
        )
        chunks = split_discord_message(reply)
        await interaction.edit_original_response(content=chunks[0])
        for chunk in chunks[1:]:
            await interaction.followup.send(chunk)
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

        async with message.channel.typing():
            answer = await self._answer(prompt, message.attachments, message.guild.id)
        chunks = split_discord_message(answer)
        await message.reply(chunks[0], mention_author=False)
        for chunk in chunks[1:]:
            await message.channel.send(chunk)
        LOGGER.info("Completed @mention guild=%s user=%s", message.guild.id, message.author.id)


def main() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )
    config = load_config()
    client = DiscordCodexClient(config)
    client.run(config.discord_token, log_handler=None)
