from __future__ import annotations

import asyncio
from types import SimpleNamespace as NS

from discord_codex_bot.codex import CodexResult
from discord_codex_bot.ui import NOT_YOURS, AnswerView, CancelView


class Response:
    def __init__(self) -> None:
        self.sent: list[tuple[str, bool]] = []
        self.deferred = False
        self.edited = None

    async def send_message(self, text, ephemeral=False):
        self.sent.append((text, ephemeral))

    async def send(self, text, ephemeral=False, **kwargs):
        self.sent.append((text, ephemeral))

    async def defer(self, thinking=False):
        self.deferred = True

    async def edit_message(self, view=None):
        self.edited = view


def interaction(user_id: int):
    return NS(user=NS(id=user_id), response=Response(), followup=Response())


def button_of(view, label):
    return next(item for item in view.children if item.label == label)


async def test_cancel_view_only_the_owner_can_cancel() -> None:
    view = CancelView(owner_id=1)
    task = asyncio.get_running_loop().create_task(asyncio.sleep(5))
    view.task = task
    other = interaction(2)
    await view.cancel.callback(other)
    assert other.response.sent == [(NOT_YOURS, True)] and not task.cancelled()
    mine = interaction(1)
    await view.cancel.callback(mine)
    await asyncio.sleep(0)
    assert task.cancelled() and mine.response.deferred and button_of(view, "取消").disabled


async def test_answer_view_remember_files_the_exchange_into_personal_memory() -> None:
    added = []
    def add(scope, g, u, name, text):
        added.append((scope, g, u, name, text))
        return "- [x](x.md) — y"

    bot = NS(memory=NS(add=add))
    view = AnswerView(bot, 7, 1, "拉麵推薦？\n第二行", "去吃一蘭")
    stranger = interaction(2)
    await view.remember.callback(stranger)
    assert stranger.response.sent == [(NOT_YOURS, True)] and added == []
    mine = interaction(1)
    await view.remember.callback(mine)
    assert added == [("user", 7, 1, "拉麵推薦？", "問：拉麵推薦？\n第二行\n答：去吃一蘭")]
    assert mine.response.edited is view and button_of(view, "記住").disabled
    assert mine.followup.sent[0][0].startswith("已記進你的個人記憶：")


async def test_answer_view_redo_asks_again_as_a_fresh_conversation() -> None:
    calls = []

    async def answer(prompt, attachments, guild_id, user_id, resume=""):
        calls.append((prompt, resume))
        return CodexResult("再答", (), None, "t2", False)

    async def send_answer(destination, prompt, result, guild_id, user_id):
        calls.append(("sent", result.text, guild_id, user_id))

    bot = NS(_answer=answer, send_answer=send_answer)
    view = AnswerView(bot, 7, 1, "問題", "答案")
    mine = interaction(1)
    await view.redo.callback(mine)
    assert mine.response.deferred
    assert calls == [("問題", ""), ("sent", "再答", 7, 1)]
