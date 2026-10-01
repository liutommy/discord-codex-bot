"""Buttons on the Bot's messages. Every button is bound to the member who asked (anyone else
gets a private refusal). The answer buttons are persistent: their custom_id carries the action
and the member, and the question/answer are recovered from the message itself, so they keep
working after the Bot restarts — which it does on every deploy."""

from __future__ import annotations

import asyncio
import re
from collections.abc import Awaitable, Callable
from typing import Any

import discord

from . import instructions
from .memory import STYLE_MAX_UPLOAD_BYTES, parse_style_upload

NOT_YOURS = "這不是你的回答，按鈕只有發問的人能用。"
CANCEL_TIMEOUT_SECONDS = 900
STYLE_MODAL_TIMEOUT = 600
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
                label=label,
                emoji=emoji or default_emoji,
                style=discord.ButtonStyle.secondary,
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


class StyleModal(discord.ui.Modal, title="上傳個人風格"):
    """One Markdown file becomes the member's personal style. It exists because a slash-command
    option is a single line — fine for "條列、少於 100 字", useless for anything longer — so the
    text option stays for one-liners and everything else arrives as a file."""

    def __init__(self, save: Callable[[str], None], limit: int) -> None:
        super().__init__(timeout=STYLE_MODAL_TIMEOUT)
        self.save = save
        self.limit = limit
        self.upload = discord.ui.FileUpload(custom_id="style-file", max_values=1)
        self.add_item(
            discord.ui.Label(
                text=f"風格檔（.md，UTF-8，最多 {limit} 字）",
                description="整份檔案會取代你目前的個人風格。",
                component=self.upload,
            )
        )

    async def on_submit(self, interaction: discord.Interaction) -> None:
        if not self.upload.values:
            await interaction.response.send_message("沒有收到檔案。", ephemeral=True)
            return
        attachment = self.upload.values[0]
        if attachment.size > STYLE_MAX_UPLOAD_BYTES:
            # Checked before downloading: the character limit is the real bound, this only keeps
            # an oversized file from being pulled in to find that out.
            await interaction.response.send_message(
                f"檔案太大（{attachment.size} bytes），個人風格最多 {self.limit} 字。",
                ephemeral=True,
            )
            return
        try:
            body = await attachment.read()
        except discord.HTTPException:
            await interaction.response.send_message("讀不到那個檔案，再試一次。", ephemeral=True)
            return
        try:
            text = parse_style_upload(attachment.filename, body, self.limit)
        except ValueError as bad:
            await interaction.response.send_message(str(bad), ephemeral=True)
            return
        self.save(text)
        await interaction.response.send_message(
            f"已用 {attachment.filename} 設定個人風格（{len(text)} 字）。人設不受影響，"
            "要不帶角色的版本請用 persona:關閉人設。",
            ephemeral=True,
        )


class InstructionsModal(discord.ui.Modal, title="上傳人設／預設輸出風格"):
    """Two optional Markdown files. Both are operator settings shared by every guild this Bot
    serves, so the command that opens this modal is gated; nothing is written unless both files
    pass, because a half-applied pair would leave the Bot in a state nobody chose."""

    def __init__(self, save: Callable[[dict[str, str], int], Awaitable[str]], limit: int) -> None:
        super().__init__(timeout=STYLE_MODAL_TIMEOUT)
        self.save = save
        self.limit = limit
        self.persona = discord.ui.FileUpload(custom_id="persona-file", max_values=1, required=False)
        self.style = discord.ui.FileUpload(custom_id="style-file", max_values=1, required=False)
        self.add_item(
            discord.ui.Label(
                text=f"人設（.md，最多 {limit} 字）",
                description="留空＝不改人設。整份檔案取代現行人設。",
                component=self.persona,
            )
        )
        self.add_item(
            discord.ui.Label(
                text=f"預設輸出風格（.md，最多 {limit} 字）",
                description="留空＝不改風格。",
                component=self.style,
            )
        )

    async def on_submit(self, interaction: discord.Interaction) -> None:
        pairs = (
            (instructions.PERSONA, self.persona),
            (instructions.OUTPUT_STYLE, self.style),
        )
        chosen = [(kind, item.values[0]) for kind, item in pairs if item.values]
        if not chosen:
            await interaction.response.send_message("兩個都留空，沒有東西要改。", ephemeral=True)
            return
        uploads: dict[str, str] = {}
        for kind, attachment in chosen:
            label = instructions.LABELS[kind]
            if attachment.size > instructions.MAX_UPLOAD_BYTES:
                await interaction.response.send_message(
                    f"{label}：檔案太大（{attachment.size} bytes），最多 {self.limit} 字。",
                    ephemeral=True,
                )
                return
            try:
                body = await attachment.read()
            except discord.HTTPException:
                await interaction.response.send_message(
                    f"{label}：讀不到那個檔案，再試一次。", ephemeral=True
                )
                return
            try:
                uploads[kind] = instructions.parse_upload(attachment.filename, body, self.limit)
            except ValueError as bad:
                await interaction.response.send_message(f"{label}：{bad}", ephemeral=True)
                return
        await interaction.response.send_message(
            await self.save(uploads, interaction.user.id), ephemeral=True
        )


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
