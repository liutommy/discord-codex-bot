from __future__ import annotations

import asyncio
import re
from types import SimpleNamespace as NS

from discord_codex_bot.ui import (
    NOT_YOURS,
    AnswerButton,
    AnswerView,
    CancelView,
    StyleModal,
    recover_exchange,
)


class Response:
    def __init__(self) -> None:
        self.sent: list[tuple[str, bool]] = []
        self.deferred = False

    async def send_message(self, text, ephemeral=False):
        self.sent.append((text, ephemeral))

    async def defer(self, thinking=False):
        self.deferred = True


def interaction(user_id: int, client=None):
    return NS(user=NS(id=user_id), response=Response(), client=client)


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
    assert task.cancelled() and mine.response.deferred


async def test_answer_buttons_carry_action_and_owner_in_the_custom_id() -> None:
    view = AnswerView(42)
    ids = [item.custom_id for item in view.children]
    assert ids == ["inmu:redo:42", "inmu:remember:42"] and view.timeout is None
    match = re.match(AnswerButton.__discord_ui_compiled_template__, "inmu:remember:42")
    rebuilt = await AnswerButton.from_custom_id(None, None, match)
    assert (rebuilt.action, rebuilt.user_id) == ("remember", 42)


async def test_answer_button_gates_on_owner_and_delegates_to_the_bot() -> None:
    calls = []

    async def handle(interaction, action, user_id):
        calls.append((action, user_id))

    button = AnswerButton("redo", 7)
    stranger = interaction(8)
    assert await button.interaction_check(stranger) is False
    assert stranger.response.sent == [(NOT_YOURS, True)]
    mine = interaction(7, client=NS(handle_answer_button=handle))
    assert await button.interaction_check(mine) is True
    await button.callback(mine)
    assert calls == [("redo", 7)]


def test_recover_exchange_parses_a_slash_answer_and_leaves_others_alone() -> None:
    content = "**問**（Medium、附圖）：\n> 第一行\n> 第二行\n\n這是答案\n第二段"
    assert recover_exchange(content) == ("第一行\n第二行", "這是答案\n第二段")
    assert recover_exchange("純答案") == ("", "純答案")
    assert recover_exchange("") == ("", "")


class _Upload:
    """A submitted attachment. `read` fails loudly so a test can prove it was never called."""

    def __init__(self, filename: str, data: bytes, size: int | None = None) -> None:
        self.filename = filename
        self.size = len(data) if size is None else size
        self._data = data

    async def read(self) -> bytes:
        if self._data is None:
            raise AssertionError("the file was downloaded when it should have been refused")
        return self._data


def _style_modal(*uploads):
    saved: list[str] = []
    modal = StyleModal(saved.append, 4000)
    modal.upload._values = list(uploads)  # what Discord fills in when the modal comes back
    return modal, saved


async def test_style_modal_saves_one_markdown_file() -> None:
    modal, saved = _style_modal(_Upload("me.md", "  條列、少於 50 字  \n".encode()))
    hit = interaction(1)
    await modal.on_submit(hit)
    assert saved == ["條列、少於 50 字"]
    text, ephemeral = hit.response.sent[-1]
    assert "me.md" in text and "10 字" in text and ephemeral is True


async def test_style_modal_refuses_and_saves_nothing() -> None:
    for uploads, reason in (
        ((), "沒有收到檔案"),
        ((_Upload("notes.txt", b"x"),), "只收"),
        ((_Upload("me.md", b"   "),), "空的"),
        ((_Upload("me.md", "字" * 4001),), "4001 字"),
    ):
        payload = tuple(
            _Upload(u.filename, u._data.encode() if isinstance(u._data, str) else u._data)
            for u in uploads
        )
        modal, saved = _style_modal(*payload)
        hit = interaction(1)
        await modal.on_submit(hit)
        assert saved == [] and reason in hit.response.sent[-1][0]


async def test_style_modal_refuses_an_oversized_file_without_downloading_it() -> None:
    modal, saved = _style_modal(_Upload("me.md", None, size=10_000_000))
    hit = interaction(1)
    await modal.on_submit(hit)
    assert saved == [] and "太大" in hit.response.sent[-1][0]
