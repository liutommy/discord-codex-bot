from __future__ import annotations

import logging

import discord
from discord import app_commands

from .access import check_access
from .codex import codex_login_status, run_codex
from .config import Config, load_config
from .output import split_discord_message, truncate
from .queue import QueueFullError, SerialQueue

LOGGER = logging.getLogger(__name__)


class DiscordCodexClient(discord.Client):
    def __init__(self, config: Config) -> None:
        intents = discord.Intents.none()
        intents.guilds = True
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

    def _access(self, interaction: discord.Interaction) -> tuple[bool, str]:
        parent_id = getattr(interaction.channel, "parent_id", None)
        decision = check_access(
            guild_id=interaction.guild_id,
            channel_id=interaction.channel_id,
            parent_channel_id=parent_id,
            config=self.config,
        )
        return decision.allowed, decision.reason

    async def status_command(self, interaction: discord.Interaction) -> None:
        allowed, reason = self._access(interaction)
        if not allowed:
            await interaction.response.send_message(reason, ephemeral=True)
            return
        status = await codex_login_status(self.config)
        await interaction.response.send_message(
            f"{status}\n模型：{self.config.codex_model}\n"
            f"推理強度：{self.config.codex_reasoning_effort}",
            ephemeral=True,
        )

    @app_commands.describe(prompt="要交給 Codex 的問題")
    async def codex_command(self, interaction: discord.Interaction, prompt: str) -> None:
        allowed, reason = self._access(interaction)
        if not allowed:
            await interaction.response.send_message(reason, ephemeral=True)
            return
        prompt = prompt.strip()
        if not prompt or len(prompt) > self.config.max_prompt_chars:
            await interaction.response.send_message(
                f"prompt 必須介於 1 到 {self.config.max_prompt_chars} 個字元。",
                ephemeral=True,
            )
            return

        await interaction.response.defer(thinking=True)
        try:
            answer = await self.queue.run(lambda: run_codex(prompt, self.config))
            chunks = split_discord_message(truncate(answer, self.config.max_response_chars))
            await interaction.edit_original_response(content=chunks[0])
            for chunk in chunks[1:]:
                await interaction.followup.send(chunk)
            LOGGER.info(
                "Completed Codex request guild=%s user=%s",
                interaction.guild_id,
                interaction.user.id,
            )
        except QueueFullError:
            await interaction.edit_original_response(content="目前排隊已滿，請稍後再試。")
        except Exception:
            LOGGER.exception("Codex request failed guild=%s", interaction.guild_id)
            await interaction.edit_original_response(
                content=(
                    "Codex 執行失敗。請用 /codex-status 檢查登入狀態，"
                    "並通知 Bot 管理者查看 container log。"
                )
            )


def main() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )
    config = load_config()
    client = DiscordCodexClient(config)
    client.run(config.discord_token, log_handler=None)
