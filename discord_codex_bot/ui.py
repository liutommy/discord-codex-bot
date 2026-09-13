"""Buttons on the Bot's messages. Every button is bound to the member who asked (anyone else
gets a private refusal). The answer buttons are persistent: their custom_id carries the action
and the member, and the question/answer are recovered from the message itself, so they keep
working after the Bot restarts — which it does on every deploy."""

from __future__ import annotations

import asyncio
import re
from typing import Any

import discord

NOT_YOURS = "這不是你的回答，按鈕只有發問的人能用。"
CANCEL_TIMEOUT_SECONDS = 900
_ACTIONS = {"redo": ("重答", "🔁"), "remember": ("記住", "👍")}


class CancelView(discord.ui.View):
    """One ❌ on the "thinking" placeholder; cancels the in-flight request's task. The caller
    edits the placeholder once the task reports cancellation, so the button only acknowledges.
    Not persistent on purpose: a restart kills the request anyway."""

    def __init__(self, owner_id: int) -> None:
        super().__init__(timeout=CANCEL_TIMEOUT_SECONDS)
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


class AnswerButton(
    discord.ui.DynamicItem[discord.ui.Button],
    template=r"inmu:(?P<action>redo|remember):(?P<user>[0-9]+)",
):
    """🔁 asks the same question again, continuing the thread that answer came from; 👍 files
    the exchange into the member's personal memory. The Bot does the work
    (`handle_answer_button`); this item only identifies the action and the owner, from the
    custom_id, and gates on the owner."""

    def __init__(self, action: str, user_id: int, emoji=None) -> None:
        label, default_emoji = _ACTIONS[action]
        super().__init__(
            discord.ui.Button(
                label=label, emoji=emoji or default_emoji, style=discord.ButtonStyle.secondary,
                custom_id=f"inmu:{action}:{user_id}",
            )
        )
        self.action = action
        self.user_id = user_id

    @classmethod
    async def from_custom_id(
        cls, interaction: discord.Interaction, item: discord.ui.Button, match: re.Match[str], /
    ) -> AnswerButton:
        return cls(match["action"], int(match["user"]))

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id == self.user_id:
            return True
        await interaction.response.send_message(NOT_YOURS, ephemeral=True)
        return False

    async def callback(self, interaction: discord.Interaction) -> None:
        await interaction.client.handle_answer_button(interaction, self.action, self.user_id)


class AnswerView(discord.ui.View):
    """The two persistent answer buttons for one member (timeout=None: persistent views must
    never expire)."""

    def __init__(self, user_id: int, remember_emoji=None) -> None:
        super().__init__(timeout=None)
        self.add_item(AnswerButton("redo", user_id))
        self.add_item(AnswerButton("remember", user_id, remember_emoji))


def recover_exchange(content: str) -> tuple[str, str]:
    """(question, answer) from a slash-command answer, whose first lines quote the question
    ("**問**…：" then "> " lines, blank line, answer). Anything else is (\"\", content)."""
    lines = content.splitlines()
    if not lines or not lines[0].startswith("**問**"):
        return "", content
    quoted: list[str] = []
    index = 1
    while index < len(lines) and lines[index].startswith("> "):
        quoted.append(lines[index][2:])
        index += 1
    if index < len(lines) and lines[index] == "":
        index += 1
    return "\n".join(quoted).strip(), "\n".join(lines[index:]).strip()
