from dataclasses import dataclass

from discord_codex_bot.bot import DiscordCodexClient, strip_mention, with_quoted_message
from discord_codex_bot.config import Config


@dataclass
class FakeAttachment:
    content_type: str | None
    size: int


def test_registers_only_expected_slash_commands(config: Config) -> None:
    from dataclasses import replace

    client = DiscordCodexClient(replace(config, command_prefix="inmu-king"))
    assert {command.name for command in client.tree.get_commands()} == {
        "inmu-king",
        "inmu-king-status",
        "inmu-king-reset",
        "inmu-king-remember",
        "inmu-king-forget",
        "inmu-king-memory",
        "inmu-king-style",
        "inmu-king-model",
    }
    assert client.intents.guilds
    assert client.intents.message_content


def test_strip_mention_removes_every_bot_mention_form() -> None:
    assert strip_mention("<@123> 你好 <@!123>  嗎", 123) == "你好   嗎"
    assert strip_mention("<@999> 不是我", 123) == "<@999> 不是我"


def test_validate_rejects_too_many_or_non_image_attachments(config: Config) -> None:
    client = DiscordCodexClient(config)
    images = [FakeAttachment("image/png", 10)] * config.max_attachments
    assert client._validate("q", images) == ""
    assert "最多" in client._validate("q", images + [FakeAttachment("image/png", 10)])
    assert client._validate("q", [FakeAttachment("text/plain", 10)])
    assert client._validate("", []) != ""


def test_codex_command_offers_every_verified_effort(config: Config) -> None:
    from discord_codex_bot.config import REASONING_EFFORTS

    client = DiscordCodexClient(config)
    command = next(c for c in client.tree.get_commands() if c.name == "codex")
    effort = next(p for p in command.parameters if p.name == "effort")
    assert {choice.value: choice.name for choice in effort.choices} == REASONING_EFFORTS
    assert not effort.required


def test_with_quoted_message_folds_reply_target_into_prompt() -> None:
    folded = with_quoted_message("這是什麼", "ryanlo", "看看這張\n圖", 1)
    assert folded == (
        "（後輩回覆了 ryanlo 的訊息：「看看這張 圖」）\n"
        "（那則訊息附了 1 張圖，已一併附上）\n這是什麼"
    )
    assert with_quoted_message("", "kimo", "", 2).endswith("請看這則訊息。")
    assert with_quoted_message("q", "kimo", "", 0) == "q"


# ----- _answer: backend dispatch, recall loop, memory tags ---------------------------------------

from dataclasses import replace  # noqa: E402

import pytest  # noqa: E402

from discord_codex_bot import bot as bot_module  # noqa: E402
from discord_codex_bot.bot import FAILURE_MESSAGE, QUEUE_FULL_MESSAGE  # noqa: E402
from discord_codex_bot.codex import CodexResult  # noqa: E402
from discord_codex_bot.queue import SerialQueue  # noqa: E402

GUILD, USER = 111111111111111111, 5


class FakeBackend:
    """Stands in for run_codex / run_agy; each call answers with the next reply (last repeats)
    and a fresh thread id so the recall loop's `resume` chaining is observable."""

    def __init__(self, *replies: str) -> None:
        self.replies = list(replies)
        self.calls: list[tuple[str, tuple, dict]] = []

    async def __call__(self, prompt, config, *args, **kw):
        self.calls.append((prompt, args, kw))
        reply = self.replies.pop(0) if len(self.replies) > 1 else self.replies[0]
        return CodexResult(reply, thread_id=f"t{len(self.calls)}")


@pytest.fixture
def client(config: Config, tmp_path) -> DiscordCodexClient:
    cfg = replace(
        config,
        codex_home=tmp_path / "codex",
        permanent_memory_dir=tmp_path / "perm",
        memory_recall_rounds=2,
    )
    return DiscordCodexClient(cfg)


@pytest.fixture
def backends(monkeypatch):
    codex, agy = FakeBackend("答案"), FakeBackend("OK")
    monkeypatch.setattr(bot_module, "run_codex", codex)
    monkeypatch.setattr(bot_module, "run_agy", agy)
    return codex, agy


async def test_answer_defaults_to_codex_with_stored_or_explicit_effort(client, backends) -> None:
    codex, agy = backends
    result = await client._answer("q", [], GUILD, USER)
    assert result.text == "答案" and result.thread_id == "t1" and agy.calls == []
    prompt, args, kw = codex.calls[0]
    assert prompt == "q" and args == () and kw["effort"] == "high"  # config default
    assert kw["resume"] == "" and kw["images"] == [] and kw["personal_style"] == ""
    client.memory.set_model(GUILD, USER, "codex:gpt-5.6-luna|low")
    await client._answer("q", [], GUILD, USER)
    assert codex.calls[-1][2]["effort"] == "low"  # the member's stored level
    await client._answer("q", [], GUILD, USER, effort="max", resume="t-old")
    assert codex.calls[-1][2]["effort"] == "max" and codex.calls[-1][2]["resume"] == "t-old"


async def test_answer_dispatches_to_agy_from_the_stored_model(client, backends) -> None:
    codex, agy = backends
    client.memory.set_model(GUILD, USER, "agy:gemini-3.8-flash|low")
    result = await client._answer("q", [], GUILD, USER)
    assert result.text == "OK" and codex.calls == []
    prompt, args, kw = agy.calls[0]
    assert args == ("gemini-3.8-flash-low",) and "effort" not in kw
    await client._answer("q", [], GUILD, USER, effort="max")
    assert agy.calls[-1][1] == ("gemini-3.8-flash-high",)  # capped at the family's top level
    client.memory.set_model(GUILD, USER, "agy:claude-opus-4-6")
    await client._answer("q", [], GUILD, USER, effort="low")
    assert agy.calls[-1][1] == ("claude-opus-4-6-thinking",)


async def test_answer_injects_memory_indexes_and_personal_style(client, tmp_path, backends) -> None:
    codex, _ = backends
    (tmp_path / "perm").mkdir()
    (tmp_path / "perm" / "MEMORY.md").write_text("- 永久 A\n", "utf-8")
    client.memory.add("user", GUILD, USER, "綠茶", "最喜歡綠茶")
    client.memory.add("guild", GUILD, None, "開團", "週五開團")
    client.memory.set_style(GUILD, USER, "短句")
    await client._answer("q", [], GUILD, USER)
    kw = codex.calls[0][2]
    assert kw["memory"].startswith("[永久記憶索引]\n- 永久 A\n\n[個人記憶索引]\n- [綠茶]")
    assert "[伺服器記憶索引]\n- [開團]" in kw["memory"] and kw["personal_style"] == "短句"
    await client._answer("q", [], GUILD, 6)  # another member: no personal section
    assert "[個人記憶索引]" not in codex.calls[-1][2]["memory"]


async def test_answer_feeds_recall_results_back_into_the_same_thread(client, backends) -> None:
    codex, _ = backends
    codex.replies = ['<search scope="user" query="綠茶"/>', "綠茶是你最喜歡的。"]
    client.memory.add("user", GUILD, USER, "綠茶", "最喜歡綠茶")
    result = await client._answer("我喜歡什麼", [], GUILD, USER)
    assert result.text == "綠茶是你最喜歡的。" and result.thread_id == "t2"
    assert len(codex.calls) == 2
    prompt, _args, kw = codex.calls[1]
    assert prompt.startswith('<RESULT kind="search" scope="user" target="綠茶">\n')
    assert "## 綠茶 (綠茶.md)" in prompt and "最喜歡綠茶" in prompt
    assert prompt.endswith("</RESULT>\n\nNow answer the member's question.")
    assert kw["resume"] == "t1" and kw["raw"] is True and kw["effort"] == "high"
    assert tuple(kw.get("images", ())) == () and "memory" not in kw  # no re-injection


async def test_answer_stops_recalling_after_the_configured_rounds(client, backends) -> None:
    codex, _ = backends
    tag = '<recall scope="permanent" name="list"/>'
    codex.replies = [tag]
    result = await client._answer("q", [], GUILD, USER)
    assert len(codex.calls) == 1 + client.config.memory_recall_rounds
    assert [c[2]["resume"] for c in codex.calls] == ["", "t1", "t2"]
    assert result.text == tag  # the last round's tag reaches the member unchanged


async def test_answer_stores_and_strips_memory_tags(client, backends) -> None:
    codex, _ = backends
    codex.replies = [
        '好的。<memory scope="user" name="綠茶">喜歡綠茶</memory>'
        '<memory scope="guild" name="開團">週五開團</memory>'
        '<memory scope="guild" name="空">   </memory>'
    ]
    result = await client._answer("q", [], GUILD, USER)
    assert result.text == "好的。"
    assert [e.name for e in client.memory.entries("user", GUILD, USER)] == ["綠茶"]
    assert [e.name for e in client.memory.entries("guild", GUILD, None)] == ["開團"]


async def test_answer_truncates_and_reports_failures(client, backends, monkeypatch) -> None:
    codex, _ = backends
    codex.replies = ["x" * 100]
    client.config = replace(client.config, max_response_chars=30)
    result = await client._answer("q", [], GUILD, USER)
    assert len(result.text) == 30 and result.text.endswith("[輸出已截斷]")

    async def boom(*args, **kw):
        raise RuntimeError("codex down")

    monkeypatch.setattr(bot_module, "run_codex", boom)
    failed = await client._answer("q", [], GUILD, USER)
    assert failed.text == FAILURE_MESSAGE.format(prefix="codex") and failed.thread_id == ""
    client.queue = SerialQueue(0)
    assert (await client._answer("q", [], GUILD, USER)).text == QUEUE_FULL_MESSAGE


def test_read_routes_permanent_and_member_scopes(client, tmp_path) -> None:
    (tmp_path / "perm" / "topics").mkdir(parents=True)
    (tmp_path / "perm" / "topics" / "rules.md").write_text("# 規則\n\n不可以洗頻\n", "utf-8")
    client.memory.add("user", GUILD, USER, "綠茶", "最喜歡綠茶")
    client.memory.add("guild", GUILD, None, "開團", "週五開團")
    found = client._read("search", "permanent", GUILD, USER, "洗頻", 1, None)
    assert "## rules (rules.md) line 3" in found
    assert client._read("recall", "permanent", GUILD, USER, "rules", 3, 1).startswith(
        "[rules.md 第 3–3 行，共 3 行]\n   3: 不可以洗頻"
    )
    assert client._read("recall", "permanent", GUILD, USER, "list", 1, None) == "- rules"
    assert "綠茶.md" in client._read("search", "user", GUILD, USER, "綠茶", 1, None)
    missed = client._read("search", "guild", GUILD, USER, "綠茶", 1, None)
    assert missed.startswith("（「綠茶」沒有命中")
    assert "   1: # 開團" in client._read("recall", "guild", GUILD, USER, "開團", 1, 1)
    assert client._read("recall", "user", GUILD, USER, "開團", 1, None).startswith("（找不到記憶")


def test_remember_wakes_the_harvester_only_on_a_thread_switch(client) -> None:
    import asyncio

    client._harvest_wakeup = asyncio.Event()
    key = "1:2:3"
    client._remember(key, "a", 10, False, "codex:gpt-5.6-luna")
    assert not client._harvest_wakeup.is_set()
    client._remember(key, "a", 11, False, "codex:gpt-5.6-luna")  # same thread continued
    assert not client._harvest_wakeup.is_set()
    client._remember(key, "b", 12, False, "codex:gpt-5.6-luna")
    assert client._harvest_wakeup.is_set()
    assert client.threads.harvest_candidates() == [(key, "a")]
