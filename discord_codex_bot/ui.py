"""Buttons on the Bot's messages. Every view is bound to the member who asked: anyone else
pressing gets a private refusal, so a button never acts on someone else's request."""

from __future__ import annotations

import asyncio
from typing import Any

import discord

NOT_YOURS = "這不是你的回答，按鈕只有發問的人能用。"
BUTTON_TIMEOUT_SECONDS = 900


class CancelView(discord.ui.View):
    """One ❌ on the "thinking" placeholder; cancels the in-flight request's task. The caller
    edits the placeholder once the task reports cancellation, so the button only acknowledges."""

    def __init__(self, owner_id: int) -> None:
        super().__init__(timeout=BUTTON_TIMEOUT_SECONDS)
        self.owner_id = owner_id
        self.task: asyncio.Task[Any] | None = None

    @discord.ui.button(label="取消", emoji="❌", style=discord.ButtonStyle.secondary)
    async def cancel(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        if interaction.user.id != self.owner_id:
            await interaction.response.send_message(NOT_YOURS, ephemeral=True)
            return
        if self.task is not None and not self.task.done():
            self.task.cancel()
        button.disabled = True
        self.stop()
        await interaction.response.defer()


class AnswerView(discord.ui.View):
    """🔁 asks the same question again as a fresh conversation on the member's current model;
    👍 files the exchange into the member's personal memory."""

    def __init__(self, bot: Any, guild_id: int | None, user_id: int, prompt: str, answer: str):
        super().__init__(timeout=BUTTON_TIMEOUT_SECONDS)
        self.bot, self.guild_id, self.user_id = bot, guild_id, user_id
        self.prompt, self.answer = prompt, answer

    async def _mine(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id == self.user_id:
            return True
        await interaction.response.send_message(NOT_YOURS, ephemeral=True)
        return False

    @discord.ui.button(label="重答", emoji="🔁", style=discord.ButtonStyle.secondary)
    async def redo(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        if not await self._mine(interaction):
            return
        await interaction.response.defer(thinking=True)
        result = await self.bot._answer(self.prompt, [], self.guild_id, self.user_id, resume="")
        await self.bot.send_answer(
            interaction.followup, self.prompt, result, self.guild_id, self.user_id
        )

    @discord.ui.button(label="記住", emoji="👍", style=discord.ButtonStyle.secondary)
    async def remember(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        if not await self._mine(interaction):
            return
        name = self.prompt.strip().splitlines()[0][:30] or "對話"
        text = f"問：{self.prompt.strip()[:200]}\n答：{self.answer.strip()[:600]}"
        line = self.bot.memory.add("user", self.guild_id, self.user_id, name, text)
        button.disabled = True
        await interaction.response.edit_message(view=self)
        await interaction.followup.send(f"已記進你的個人記憶：{line}", ephemeral=True)
