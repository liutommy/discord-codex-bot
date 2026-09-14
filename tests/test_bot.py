import asyncio
import logging
import logging.handlers
from dataclasses import dataclass
from pathlib import Path

from discord_codex_bot.bot import (
    DiscordCodexClient,
    strip_mention,
    tracking_message,
    tracking_provider,
    with_quoted_message,
)
from discord_codex_bot.config import Config


@dataclass
class FakeAttachment:
    content_type: str | None
    size: int
    filename: str = "blob.bin"


def test_registers_only_expected_slash_commands(config: Config) -> None:
    from dataclasses import replace

    client = DiscordCodexClient(replace(config, command_prefix="inmu-king"))
    assert {command.name for command in client.tree.get_commands()} == {
        "inmu-king",
        "inmu-king-status",
        "inmu-king-help",
        "inmu-king-reset",
        "inmu-king-stop",
        "inmu-king-summary",
        "inmu-king-remind",
        "inmu-king-export",
        "inmu-king-remember",
        "inmu-king-forget",
        "inmu-king-memory",
        "inmu-king-style",
        "inmu-king-model",
        "inmu-king-track",
    }
    assert client.intents.guilds
    assert client.intents.message_content


def test_tracking_provider_supports_only_youtube_and_twitch() -> None:
    assert tracking_provider("https://www.youtube.com/@HoushouMarine") == "youtube"
    assert tracking_provider("https://www.twitch.tv/chibidoki") == "twitch"
    with pytest.raises(ValueError, match="只支援"):
        tracking_provider("https://example.com/person")


def test_strip_mention_removes_every_bot_mention_form() -> None:
    assert strip_mention("<@123> 你好 <@!123>  嗎", 123) == "你好   嗎"
    assert strip_mention("<@999> 不是我", 123) == "<@999> 不是我"


def test_validate_rejects_too_many_or_non_image_attachments(config: Config) -> None:
    client = DiscordCodexClient(config)
    images = [FakeAttachment("image/png", 10)] * config.max_attachments
    assert client._validate("q", images) == ""
    assert "最多" in client._validate("q", images + [FakeAttachment("image/png", 10)])
    assert client._validate("q", [FakeAttachment("application/octet-stream", 10)])
    assert client._validate("q", [FakeAttachment("text/plain", 10, "notes.txt")]) == ""  # document
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
from discord_codex_bot.codex import CodexResult, CodexUsageLimit  # noqa: E402
from discord_codex_bot.links import Preview  # noqa: E402
from discord_codex_bot.queue import SerialQueue  # noqa: E402
from discord_codex_bot.tracking import (  # noqa: E402
    ContentItem,
    Decision,
    OutboxMessage,
    TrackerStore,
    Watch,
)
from discord_codex_bot.usage import RateLimits  # noqa: E402

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


async def test_tracking_classifier_is_isolated_and_bounded(
    client, tmp_path, monkeypatch
) -> None:
    client.tracker = TrackerStore(tmp_path / "tracking.sqlite3")
    client.config = replace(
        client.config,
        tracking_schema_path=tmp_path / "tracking-schema.json",
        tracking_reasoning_effort="high",
        tracking_min_remaining_percent=50,
    )
    calls = []

    async def limits(_config):
        return RateLimits(10, 20, "test")

    async def batch(prompt, config, **kwargs):
        calls.append((prompt, config, kwargs))
        return '{"decisions":[]}'

    monkeypatch.setattr(bot_module, "probe_rate_limits", limits)
    monkeypatch.setattr(bot_module, "run_batch", batch)
    assert await client._classify_tracking("classify") == '{"decisions":[]}'
    kwargs = calls[0][2]
    assert kwargs["isolated"] and kwargs["effort"] == "high"
    assert kwargs["schema"] == client.config.tracking_schema_path
    await client._classify_tracking("again")
    assert len(calls) == 2  # the window gate is the only limit; there is no daily cap


async def test_tracking_tags_add_a_watch_and_switch_its_mode(
    client, tmp_path, monkeypatch
) -> None:
    client.tracker = TrackerStore(tmp_path / "tracking.sqlite3")
    client.config = replace(client.config, youtube_api_key="key")

    async def resolve(_locator):
        return "UC1", {"title": "Marine"}

    monkeypatch.setattr(client.youtube_tracker, "resolve", resolve)
    # A real Discord id: <track who=…> only accepts 17-20 digit snowflakes, so that a stray
    # number in the attribute cannot turn into a mention.
    friend = 222222222222222222
    added = await client._apply_tracking_tags(
        "好，幫你追。\n"
        f'<track source="https://www.youtube.com/@HoushouMarine" who="<@{friend}>"/>',
        GUILD, 555, USER,
    )
    assert "已新增追蹤 #1" in added and f"<@{friend}>" in added and "<track" not in added
    watch = client.tracker.watches(user_id=USER)[0]
    assert watch.mention_ids == (friend,) and watch.interval_minutes == 60
    # The member's own watches are listed in the prompt, so ids never have to be invented.
    assert f"#{watch.id} youtube" in client._tracked_lines(USER)
    slower = await client._apply_tracking_tags(
        f'<track_every id="{watch.id}" minutes="180"/>', GUILD, 555, USER
    )
    assert "每 180 分鐘" in slower
    assert client.tracker.watches(user_id=USER)[0].interval_minutes == 180
    # Someone else's watch is not theirs to retune: the store checks the owner.
    assert "找不到你的追蹤" in await client._apply_tracking_tags(
        f'<track_every id="{watch.id}" minutes="5"/>', GUILD, 555, 999
    )


def test_notification_uses_the_wording_the_model_wrote() -> None:
    watch = Watch(1, 1, GUILD, 555, USER, "policy", mention_ids=(7, 8))
    item = ContentItem(1, 1, "v1", "https://example.com/v1", "新曲發表", "", "now", "video")
    said = "前輩發現星街彗星發新曲了，而且是久違的原創曲"
    decision = Decision(1, 1, 1, True, 0.9, "新曲", "符合政策", (), "decided", said)
    text = tracking_message(OutboxMessage(1, decision, watch, item, 0), "星街彗星")
    assert text.startswith(f"<@{USER}> <@7> <@8> {said}")
    assert "https://example.com/v1" in text
    # Category, confidence and reasoning are log material, not notification material.
    assert "符合政策" not in text and "0.9" not in text


def test_notification_falls_back_when_the_model_wrote_no_wording() -> None:
    watch = Watch(1, 1, GUILD, 555, USER, "policy")
    item = ContentItem(1, 1, "v1", "https://example.com/v1", "新曲發表", "", "now", "video")
    decision = Decision(1, 1, 1, True, 0.9, "新曲", "符合政策", (), "decided")
    text = tracking_message(OutboxMessage(1, decision, watch, item, 0), "星街彗星")
    assert "前輩發現星街彗星有新曲了" in text


def test_a_notification_never_carries_a_mention_from_the_social_text() -> None:
    watch = Watch(1, 1, GUILD, 555, USER, "policy")
    item = ContentItem(
        1, 1, "v1", "https://example.com/v1", "@everyone 新衣裝公開", "", "now", "video"
    )
    # Even the model's own wording is derived from untrusted text, so it is escaped too.
    decision = Decision(
        1, 1, 1, True, 0.9, "新衣裝", "@everyone 符合", (), "decided", "@everyone 新衣裝公開了"
    )
    text = tracking_message(OutboxMessage(1, decision, watch, item, 0), "星街彗星")
    assert "@everyone" not in text and text.startswith(f"<@{USER}> ")


async def test_tracking_classifier_defers_before_calling_the_model(
    client, tmp_path, monkeypatch
) -> None:
    client.tracker = TrackerStore(tmp_path / "tracking.sqlite3")
    client.config = replace(client.config, tracking_min_remaining_percent=50)
    calls = []

    async def limits(_config):
        return RateLimits(55, 10, "test")

    async def batch(prompt, config, **kwargs):
        calls.append(prompt)
        return "{}"

    monkeypatch.setattr(bot_module, "probe_rate_limits", limits)
    monkeypatch.setattr(bot_module, "run_batch", batch)
    with pytest.raises(RuntimeError, match="below the tracking gate"):
        await client._classify_tracking("classify")
    assert calls == []


async def test_answer_falls_back_to_the_spare_backend_when_codex_quota_is_spent(
    client, monkeypatch
) -> None:
    agy = FakeBackend("備援答案")

    async def spent(*_args, **_kw):
        raise CodexUsageLimit("You've hit your usage limit. Try again at 2:33 PM.")

    monkeypatch.setattr(bot_module, "run_codex", spent)
    monkeypatch.setattr(bot_module, "run_agy", agy)
    result = await client._answer("q", [], GUILD, USER, resume="t-old")
    assert "備援答案" in result.text and "Codex 額度用完了" in result.text
    assert agy.calls[0][1] == ("gemini-3.8-flash-medium",)  # CODEX_FALLBACK_MODEL default
    assert "resume" not in agy.calls[0][2]  # a Codex thread id means nothing to agy
    assert result.thread_id == "" and not result.resumed  # nothing to resume back on Codex


async def test_answer_without_a_spare_backend_reports_the_failure(client, monkeypatch) -> None:
    async def spent(*_args, **_kw):
        raise CodexUsageLimit("quota spent")

    client.config = replace(client.config, codex_fallback_model="")
    monkeypatch.setattr(bot_module, "run_codex", spent)
    result = await client._answer("q", [], GUILD, USER)
    assert "額度用完了" not in result.text  # the generic failure message, not a silent answer


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


async def test_answer_feeds_a_standalone_fetch_tag_screenshot_back_as_an_image(
    client, backends, monkeypatch, tmp_path
) -> None:
    codex, _ = backends
    codex.replies = ['<fetch url="https://x.example/p" render="1"/>', "看到了"]
    shot = tmp_path / "shot.jpg"
    shot.write_bytes(b"x")
    calls: list[tuple[str, bool]] = []

    async def fake_fetch_or_render(url, config, out_dir, render=False):
        calls.append((url, render))
        return "整頁內容", [shot]

    monkeypatch.setattr(bot_module, "fetch_or_render", fake_fetch_or_render)
    result = await client._answer("看這個網頁", [], GUILD, USER)
    assert result.text == "看到了"
    assert calls == [("https://x.example/p", True)]
    prompt, _args, kw = codex.calls[1]
    assert prompt.startswith('<LINK url="https://x.example/p">\n整頁內容\n</LINK>')
    assert tuple(kw["images"]) == (shot,) and kw["resume"] == "t1" and kw["raw"] is True


async def test_fetch_tag_embedded_in_prose_is_not_treated_as_a_standalone_request(
    client, backends, monkeypatch
) -> None:
    codex, _ = backends
    embedded = '先說明一下。<fetch url="https://x.example/p"/>後面還有內容。'
    codex.replies = [embedded]

    async def fail_if_called(url, config, out_dir, render=False):
        raise AssertionError("fetch_or_render must not run for an embedded fetch tag")

    monkeypatch.setattr(bot_module, "fetch_or_render", fail_if_called)
    result = await client._answer("問題", [], GUILD, USER)
    assert result.text == embedded
    assert len(codex.calls) == 1


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


def test_request_only_accepts_tag_only_replies_and_rejects_prose() -> None:
    from discord_codex_bot.bot import request_only

    assert request_only('<fetch url="https://a.example/p" render="1"/>')
    assert request_only('<search scope="user" query="a|b"/>\n<recall scope="guild" name="n"/>')
    assert not request_only('先說明。<fetch url="https://a.example/p"/>')
    assert not request_only('<fetch url="https://a.example/p"/> 然後回答')
    assert not request_only("")


async def test_answer_dispatches_to_openrouter_with_the_catalog(
    client, backends, monkeypatch
) -> None:
    from discord_codex_bot.openrouter import Model

    calls = []

    async def fake_openrouter(router, text, config, model, **kw):
        calls.append((text, model, kw))
        assert router.key == "openrouter"
        return CodexResult("OR", (), None, "or-1", False)

    async def no_refresh():
        return client.openrouter.models

    monkeypatch.setattr(bot_module, "run_router", fake_openrouter)
    monkeypatch.setattr(client.openrouter, "free_models", no_refresh)
    client.openrouter.models = [Model("g/free", "G", True, False, 1000)]
    client.memory.set_model(GUILD, USER, "openrouter:g/free|high")
    result = await client._answer("q", [], GUILD, USER)
    assert result.text == "OR" and backends[0].calls == [] and backends[1].calls == []
    text, model, kw = calls[0]
    assert text == "q" and model == "g/free" and kw["effort"] == "high"
    assert kw["catalog"] is client.openrouter and kw["resume"] == ""


async def test_model_command_autocomplete_and_openrouter_reminder(client, monkeypatch) -> None:
    from discord_codex_bot.openrouter import Model

    async def no_refresh():
        return client.openrouter.models

    monkeypatch.setattr(client.openrouter, "free_models", no_refresh)
    client.openrouter.models = [Model("g/vision:free", "Vision", True, False, 1),
                                Model("t/text:free", "Text", False, True, 1)]
    names = [c.name for c in await client.model_options("openrouter", "")]
    assert names == ["OpenRouter · Vision（看圖）", "OpenRouter · Text"]
    assert [c.value for c in await client.model_options("openrouter", "TEXT")] == [
        "openrouter:t/text:free"
    ]
    assert [c.value for c in await client.model_options("agy", "opus")] == ["agy:claude-opus-4-6"]
    assert [c.value for c in await client.model_options("codex", "")] == ["codex:gpt-5.6-luna"]
    assert client._chosen_model("openrouter", "g/vision:free").value == "openrouter:g/vision:free"
    assert client._chosen_model("openrouter", "openrouter:t/text:free").family == "t/text:free"
    assert client._chosen_model("openrouter", "gone/model") is None
    assert client._chosen_model("agy", "gemini-3.8-flash").value == "agy:gemini-3.8-flash"
    assert client._chosen_model("agy", "nope") is None
    described = client._describe("openrouter:g/vision:free", "high")
    assert "OpenRouter · g/vision:free · 無" in described and "看得到圖片" in described
    assert "免費模型可能隨時不穩或下架" in described
    assert "· High" in client._describe("openrouter:t/text:free", "high")
    assert "看不到圖片" in client._describe("openrouter:t/text:free", "")


def test_previews_from_reads_discord_embeds_preferring_the_proxied_picture() -> None:
    from types import SimpleNamespace as NS

    from discord_codex_bot.bot import previews_from

    thumb = NS(url="https://cdn.dcard/1.jpg", proxy_url="https://images.discordapp.net/1.jpg")
    article = NS(type="article", url="https://www.dcard.tw/f/x/p/1", title="T", description="D",
                 thumbnail=thumb, image=None)
    bare = NS(type="link", url="https://a.example", title=None, description=None,
              thumbnail=NS(url=None, proxy_url=None), image=NS(url="https://a.example/i.png",
              proxy_url=None))
    gif = NS(type="gifv", url="https://tenor.com/x", title="", description="", thumbnail=None,
             image=None)
    previews = previews_from([NS(embeds=[article, gif]), NS(embeds=[bare, article])])
    assert list(previews) == ["https://www.dcard.tw/f/x/p/1", "https://a.example"]
    assert previews["https://www.dcard.tw/f/x/p/1"] == Preview(
        "https://www.dcard.tw/f/x/p/1", "T", "D", "https://images.discordapp.net/1.jpg"
    )
    assert previews["https://a.example"].image_url == "https://a.example/i.png"


async def test_answer_hands_previews_to_link_blocks(client, backends, monkeypatch) -> None:
    seen = {}

    async def fake_link_blocks(urls, config, out_dir, previews=None):
        seen["urls"], seen["previews"] = urls, previews
        return "", []

    monkeypatch.setattr(bot_module, "link_blocks", fake_link_blocks)
    previews = {"https://x.example": Preview("https://x.example", "t")}
    await client._answer("看 https://x.example", [], GUILD, USER, previews=previews)
    assert seen == {"urls": ["https://x.example"], "previews": previews}


async def test_understand_videos_races_the_timer_and_fires_the_interim(
    client, monkeypatch
) -> None:
    import asyncio

    from discord_codex_bot import bot as bm

    monkeypatch.setattr(bm.gemini, "available", lambda config: True)
    client.config = replace(client.config, video_interim_after_seconds=0.05)

    async def slow_understand(url, config, out_dir):
        await asyncio.sleep(0.2)
        return "描述"

    notices = []

    async def on_slow():
        notices.append(1)

    monkeypatch.setattr(bm, "understand_video", slow_understand)
    block = await client._understand_videos(
        ["https://youtu.be/dQw4w9WgXcQ"], Path("/tmp"), on_slow
    )
    assert block == '<VIDEO url="https://youtu.be/dQw4w9WgXcQ">\n描述\n</VIDEO>'
    assert notices == [1]  # the slow clip fired the interim exactly once


async def test_understand_videos_stays_silent_when_fast_or_disabled(client, monkeypatch) -> None:
    from discord_codex_bot import bot as bm

    async def fast_understand(url, config, out_dir):
        return "快"

    notices = []

    async def on_slow():
        notices.append(1)

    monkeypatch.setattr(bm.gemini, "available", lambda config: True)
    monkeypatch.setattr(bm, "understand_video", fast_understand)
    block = await client._understand_videos(["https://youtu.be/dQw4w9WgXcQ"], Path("/tmp"), on_slow)
    assert block == '<VIDEO url="https://youtu.be/dQw4w9WgXcQ">\n快\n</VIDEO>' and notices == []
    # no video urls, or Gemini disabled → no work, no notice
    assert await client._understand_videos(["https://example.com"], Path("/tmp"), on_slow) == ""
    monkeypatch.setattr(bm.gemini, "available", lambda config: False)
    assert await client._understand_videos(
        ["https://youtu.be/dQw4w9WgXcQ"], Path("/tmp"), on_slow
    ) == ""


async def test_answer_dispatches_to_orcarouter_with_its_own_catalog(
    client, backends, monkeypatch
) -> None:
    from discord_codex_bot.openrouter import Model

    calls = []

    async def fake_router(router, text, config, model, **kw):
        calls.append((router.key, model, kw["catalog"]))
        return CodexResult("OC", (), None, "oc-1", False)

    async def no_refresh():
        return client.orcarouter.models

    monkeypatch.setattr(bot_module, "run_router", fake_router)
    monkeypatch.setattr(client.orcarouter, "free_models", no_refresh)
    client.orcarouter.models = [Model("tencent/hy3-free", "hy3", False, False, 0)]
    client.memory.set_model(GUILD, USER, "orcarouter:tencent/hy3-free")
    result = await client._answer("q", [], GUILD, USER)
    assert result.text == "OC" and result.thread_id == "oc-1"
    assert calls == [("orcarouter", "tencent/hy3-free", client.orcarouter)]


async def test_model_options_lists_orcarouter_free_models_only_with_a_key(
    client, monkeypatch
) -> None:
    from discord_codex_bot.openrouter import Model

    async def no_refresh():
        return client.orcarouter.models

    monkeypatch.setattr(client.orcarouter, "free_models", no_refresh)
    client.orcarouter.models = [Model("tencent/hy3-free", "hy3", False, False, 0)]
    assert [c.value for c in await client.model_options("orcarouter", "")] == [
        "orcarouter:tencent/hy3-free"
    ]
    assert client._chosen_model("orcarouter", "tencent/hy3-free").backend == "orcarouter"
    assert client._chosen_model("orcarouter", "nope-free") is None
    described = client._describe("orcarouter:tencent/hy3-free", "high")
    assert "OrcaRouter · tencent/hy3-free · 無" in described
    client.config = replace(client.config, orcarouter_api_key="")
    assert await client.model_options("orcarouter", "") == []  # no key → not offered


async def test_status_text_reports_member_settings_and_system(client, monkeypatch) -> None:
    from discord_codex_bot.openrouter import Model
    from discord_codex_bot.threads import ThreadStore

    async def login(config):
        return "ChatGPT 訂閱登入有效"

    async def no_refresh_or():
        return client.openrouter.models

    async def no_refresh_oc():
        return client.orcarouter.models

    monkeypatch.setattr(bot_module, "codex_login_status", login)
    monkeypatch.setattr(client.openrouter, "free_models", no_refresh_or)
    monkeypatch.setattr(client.orcarouter, "free_models", no_refresh_oc)
    client.openrouter.models = [Model("g/free", "G", True, False, 0)]
    client.orcarouter.models = [Model("tencent/hy3-free", "hy3", False, False, 0)]

    # defaults: nothing set, no thread
    text = await client._status_text(GUILD, 555, USER)
    assert "模型：Codex · gpt-5.6-luna · 強度 High（預設）" in text
    assert "風格：無（用預設）" in text and "續接：無，下一句會新開對話" in text
    assert "記憶：個人 0 條 / 0 KB（上限 50 MB） · 伺服器 0 條" in text
    assert "永久 0 主題" in text
    assert "Codex：ChatGPT 訂閱登入有效" in text
    assert "OpenRouter 1 個免費模型 · OrcaRouter 1 個免費模型" in text
    assert "影片理解：開 · 讀連結：開" in text

    # a member with a router model, a style, memories and a resumable thread
    client.memory.set_model(GUILD, USER, "orcarouter:tencent/hy3-free|low")
    client.memory.set_style(GUILD, USER, "條列、少於 50 字")
    client.memory.add("user", GUILD, USER, "拉麵", "小明喜歡拉麵")
    key = ThreadStore.key(GUILD, 555, USER)
    client.threads.remember(key, "oc-abc", None, plain=True, model="orcarouter:tencent/hy3-free")
    text = await client._status_text(GUILD, 555, USER)
    assert "模型：OrcaRouter · tencent/hy3-free · 強度 無（你設定） · 看不到圖" in text
    assert "風格：條列、少於 50 字" in text
    assert "續接：會接續 0 分鐘前的對話（OrcaRouter · tencent/hy3-free）" in text
    assert "個人 1 條" in text

    # switching model makes the old thread non-resumable: status says so
    client.memory.set_model(GUILD, USER, "codex:gpt-5.6-luna")
    text = await client._status_text(GUILD, 555, USER)
    assert "續接：0 分鐘前的對話是 OrcaRouter · tencent/hy3-free／另一種風格，下一句會新開" in text

    # a router without a key is not listed
    client.config = replace(client.config, orcarouter_api_key="", gemini_api_key="")
    text = await client._status_text(GUILD, 555, USER)
    assert "OpenRouter 1 個免費模型" in text and "OrcaRouter" not in text.split("【系統】")[1]
    assert "影片理解：關" in text


def test_help_sheet_is_generated_from_the_registered_commands(client) -> None:
    sheet = client.help_sheet()
    assert sheet.startswith("這個 Bot 的斜線指令（前綴 /codex）：")
    assert "/codex-status — " in sheet and "/codex-model — " in sheet
    assert "/codex-model — " in sheet and "（參數：" in sheet.split("/codex-model — ")[1]
    assert "/codex-memory — " in sheet and "OrcaRouter" in sheet and "/codex-help — " in sheet


async def test_answer_hands_the_help_sheet_to_the_backend(client, backends) -> None:
    codex, _ = backends
    await client._answer("你會什麼", [], GUILD, USER)
    assert codex.calls[0][2]["help"].startswith("這個 Bot 的斜線指令")


def test_memory_text_lists_one_scope_or_both(client) -> None:
    assert client._memory_text(GUILD, USER) == "目前沒有記憶。"
    assert client._memory_text(GUILD, USER, "guild") == "目前沒有伺服器記憶。"
    client.memory.add("user", GUILD, USER, "拉麵", "小明喜歡拉麵")
    client.memory.add("guild", GUILD, None, "開團", "週五開團")
    both = client._memory_text(GUILD, USER)
    assert "[個人記憶索引]" in both and "[伺服器記憶索引]" in both and "開團" in both
    only_guild = client._memory_text(GUILD, USER, "guild")
    assert only_guild.startswith("[伺服器記憶索引]") and "拉麵" not in only_guild


def test_every_registered_command_has_a_guide_entry_and_vice_versa(client) -> None:
    """The maintenance guard: a new command without help text (or stale help for a removed
    command) fails here, so the guide cannot drift from the code."""
    from discord_codex_bot.help import COMMAND_GUIDE

    prefix = client.config.command_prefix
    registered = {c.name.removeprefix(prefix) for c in client.tree.get_commands()}
    assert registered == set(COMMAND_GUIDE), (registered ^ set(COMMAND_GUIDE))


def test_help_guide_and_sheet_come_from_the_same_source(client) -> None:
    guide = client.help_guide()
    assert guide.startswith("**/codex 指令說明**")
    assert "**/codex-model**" in guide and "`/codex-model clear:True`" in guide
    assert "`/codex-remember scope:個人 name:拉麵" in guide
    assert "**不用指令也能做的事**" in guide and "@提及" in guide
    sheet = client.help_sheet()
    assert "/codex-help — 所有指令的說明與範例用法" in sheet
    assert "其他用法：@提及 Bot 也能問" in sheet and "**" not in sheet  # markdown stripped


async def test_run_tracked_reports_cancellation_and_clears_the_active_slot(client) -> None:
    import asyncio

    shown = []

    async def show(text, view):
        shown.append((text, type(view).__name__ if view else None))

    async def start(on_video_slow, on_delta):
        await asyncio.sleep(5)
        return CodexResult("late", (), None, "t", False)

    async def cancel_soon():
        await asyncio.sleep(0.05)
        client.active["k"].cancel()

    asyncio.get_running_loop().create_task(cancel_soon())
    assert await client._run_tracked("k", USER, show, start) is None
    assert shown == [("🤔 思考中…", "CancelView"), ("⛔ 已取消。", None)]
    assert "k" not in client.active

    async def quick(on_video_slow, on_delta):
        return CodexResult("ok", (), None, "t", False)

    result = await client._run_tracked("k", USER, show, quick)
    assert result.text == "ok" and "k" not in client.active


async def test_stop_command_cancels_only_an_in_flight_request(client) -> None:
    import asyncio
    from types import SimpleNamespace as NS

    from discord_codex_bot.threads import ThreadStore

    sent = []

    class Response:
        async def send_message(self, text, ephemeral=False):
            sent.append(text)

    channel_id = 222222222222222222  # the conftest allowlisted channel
    interaction = NS(guild_id=GUILD, channel_id=channel_id, channel=None, user=NS(id=USER),
                     response=Response())
    await client.stop_command(interaction)
    assert sent[-1] == "你在這個頻道沒有進行中的請求。"
    key = ThreadStore.key(GUILD, channel_id, USER)
    task = asyncio.get_running_loop().create_task(asyncio.sleep(5))
    client.active[key] = task
    await client.stop_command(interaction)
    await asyncio.sleep(0)
    assert sent[-1] == "已取消你在這個頻道進行中的請求。" and task.cancelled()


async def test_answer_feeds_the_alerter(client, backends, monkeypatch) -> None:
    events = []

    async def failure(backend, error):
        events.append(("fail", backend, error[:20]))

    async def success(backend):
        events.append(("ok", backend))

    monkeypatch.setattr(client.alerts, "record_failure", failure)
    monkeypatch.setattr(client.alerts, "record_success", success)
    await client._answer("q", [], GUILD, USER)
    assert events == [("ok", "codex")]

    async def boom(*args, **kwargs):
        raise RuntimeError("Codex exited with code 1: x")

    monkeypatch.setattr(bot_module, "run_codex", boom)
    result = await client._answer("q", [], GUILD, USER)
    assert result.text.startswith("Codex 執行失敗")
    assert events[-1] == ("fail", "codex", "Codex exited with co")


async def test_answer_reads_attached_documents_into_a_files_block(
    client, backends, tmp_path
) -> None:
    from dataclasses import dataclass

    @dataclass
    class Doc:
        content_type: str
        size: int
        filename: str
        body: bytes

        async def save(self, path):
            await asyncio.to_thread(Path(path).write_bytes, self.body)

    codex, _ = backends
    client.config = replace(client.config, attachment_dir=tmp_path)
    await client._answer(
        "這兩份在講什麼", [Doc("text/plain", 5, "a.txt", b"hello file"),
                            Doc("application/octet-stream", 3, "b.py", b"print(1)")], GUILD, USER,
    )
    kw = codex.calls[-1][2]
    assert '<FILE name="a.txt">\nhello file\n</FILE>' in kw["files"]
    assert '<FILE name="b.py">\nprint(1)\n</FILE>' in kw["files"]
    assert kw["images"] == []


async def test_fire_reminder_mentions_only_the_member(client) -> None:

    sent = []

    class Channel:
        async def send(self, text, allowed_mentions=None):
            sent.append((text, allowed_mentions.users, allowed_mentions.everyone))

    client.get_channel = lambda cid: Channel() if cid == 555 else None
    await client._fire_reminder({"channel_id": 555, "user_id": USER, "text": "收衣服"})
    assert sent == [(f"⏰ <@{USER}> 提醒：收衣服", True, False)]
    await client._fire_reminder(
        {"channel_id": 555, "user_id": USER, "target_id": 777, "text": "開團"}
    )
    assert sent[-1][0] == f"⏰ <@777> 提醒：開團（<@{USER}> 設的）"


async def test_streamer_throttles_skips_tag_interims_and_clips(client, monkeypatch) -> None:
    import time as time_module

    shown = []

    async def show(text, view):
        shown.append(text)

    clock = {"t": 100.0}
    monkeypatch.setattr(time_module, "monotonic", lambda: clock["t"])
    on_delta = client._streamer(show, None)
    await on_delta("<fetch url=\"https://x\"/>")  # a read request, not an answer
    await on_delta("你好")
    await on_delta("你好，我是")  # within 1.5s: suppressed
    clock["t"] += 2
    await on_delta("A" * 3000)
    assert shown == ["你好 ▌", "A" * 1900 + " ▌"]


async def test_answer_passes_on_delta_to_the_backend(client, backends) -> None:
    codex, _ = backends

    async def on_delta(text):
        pass

    await client._answer("q", [], GUILD, USER, on_delta=on_delta)
    assert codex.calls[-1][2]["on_delta"] is on_delta


async def test_exchange_of_uses_the_replied_message_or_the_quoted_block(
    client, monkeypatch
) -> None:
    from types import SimpleNamespace as NS

    monkeypatch.setattr(type(client), "user", property(lambda self: NS(id=999)))
    original = NS(content="<@999> 拉麵推薦？", channel=None)

    async def fetch_message(message_id):
        return original

    replied = NS(
        content="去吃一蘭", reference=NS(message_id=1, resolved=None),
        channel=NS(fetch_message=fetch_message),
    )
    # a resolved reference is a discord.Message in production; the fallback path is exercised
    # by isinstance failing here, so also cover the quoted-block shape
    slash = NS(content="**問**：\n> 今天吃什麼\n\n吃咖哩", reference=None)
    assert await client._exchange_of(slash) == ("今天吃什麼", "吃咖哩")
    # a SimpleNamespace is not a discord.Message, so the quoted-block fallback runs
    assert await client._exchange_of(replied) == ("", "去吃一蘭")


async def test_handle_answer_button_remember_and_redo(client, backends, monkeypatch) -> None:
    from types import SimpleNamespace as NS

    sent = []

    class Response:
        def __init__(self):
            self.deferred = False

        async def send_message(self, text, ephemeral=False):
            sent.append(("msg", text))

        async def defer(self, thinking=False):
            self.deferred = True

    class Followup:
        async def send(self, text, files=None, view=None):
            sent.append(("followup", text, type(view).__name__ if view else None))
            return NS(id=9999)

    message = NS(content="**問**：\n> 今天吃什麼\n\n吃咖哩", reference=None, id=4242)
    interaction = NS(
        message=message, guild_id=GUILD, channel_id=555, response=Response(), followup=Followup()
    )
    await client.handle_answer_button(interaction, "remember", USER)
    assert sent[-1][0] == "msg" and sent[-1][1].startswith("已記進你的個人記憶：")
    assert [e.name for e in client.memory.entries("user", GUILD, USER)] == ["今天吃什麼"]
    # 🔁 resumes the thread that answer came from: without it a redo of a follow-up question
    # ("那他呢？") is answered as if the conversation before it had never happened.
    from discord_codex_bot.threads import ThreadStore

    plain, model = bool(client.memory.get_style(GUILD, USER)), client._model(GUILD, USER)
    key = ThreadStore.key(GUILD, 555, USER)
    client.threads.remember(key, "thread-abc", 4242, plain=plain, model=model)
    await client.handle_answer_button(interaction, "redo", USER)
    assert interaction.response.deferred
    assert sent[-1] == ("followup", "答案", "AnswerView")
    assert backends[0].calls[-1][0] == "今天吃什麼"
    assert backends[0].calls[-1][2]["resume"] == "thread-abc"
    assert client.threads.by_message(9999, plain=plain, model=model) == "t1"  # redo answer linked
    empty = NS(message=NS(content="沒有引用", reference=None), guild_id=GUILD, channel_id=555,
               response=Response(), followup=Followup())
    await client.handle_answer_button(empty, "redo", USER)
    assert sent[-1][1].startswith("找不到原本的問題")


async def test_recall_loop_runs_web_searches_from_a_tag_only_reply(client, monkeypatch) -> None:
    from discord_codex_bot import search as search_module
    from discord_codex_bot.bot import request_only

    assert request_only('<web query="台北 夜市"/>') and not request_only("先說<web query=\"x\"/>")
    replies = iter([
        CodexResult('<web query="台北 夜市"/>', (), None, "t1", False),
        CodexResult("答案", (), None, "t1", True),
    ])
    calls = []

    async def fake_codex(text, config, **kw):
        calls.append(text)
        return next(replies)

    async def fake_search(query, config):
        return "SearXNG", [search_module.Hit("士林夜市", "https://s.example", "有名")]

    monkeypatch.setattr(bot_module, "run_codex", fake_codex)
    monkeypatch.setattr(search_module, "search_web", fake_search)
    result = await client._answer("推薦夜市", [], GUILD, USER)
    assert result.text == "答案"
    assert '<RESULT kind="web" query="台北 夜市" via="SearXNG">' in calls[1]
    assert "1. 士林夜市 — https://s.example" in calls[1]


def test_remember_button_uses_the_guild_custom_emoji_when_present(client, monkeypatch) -> None:
    from types import SimpleNamespace as NS

    monkeypatch.setattr(client, "get_guild", lambda gid: NS(
        emojis=[NS(name="other", id=1, animated=False), NS(name="114514", id=2, animated=False)]
    ))
    view = client._answer_view(GUILD, USER, "q", CodexResult("a"))
    remember = [b for b in view.children if b.custom_id.startswith("inmu:remember")][0]
    assert remember.item.emoji.id == 2 and remember.item.emoji.name == "114514"
    monkeypatch.setattr(client, "get_guild", lambda gid: NS(emojis=[]))
    view = client._answer_view(GUILD, USER, "q", CodexResult("a"))
    remember = [b for b in view.children if b.custom_id.startswith("inmu:remember")][0]
    assert str(remember.item.emoji) == "👍"


async def test_warm_emojis_fills_the_cache_used_by_the_remember_button(client, monkeypatch) -> None:
    from types import SimpleNamespace as NS

    async def fetch_emojis():
        return [NS(name="114514", id=5, animated=False)]

    guilds = [NS(id=GUILD, fetch_emojis=fetch_emojis)]
    monkeypatch.setattr(type(client), "guilds", property(lambda self: guilds))
    monkeypatch.setattr(client, "get_guild", lambda gid: NS(emojis=[]))  # gateway cache empty
    await client.warm_emojis()
    view = client._answer_view(GUILD, USER, "q", CodexResult("a"))
    remember = [b for b in view.children if b.custom_id.startswith("inmu:remember")][0]
    assert remember.item.emoji.id == 5


async def test_answer_creates_and_cancels_reminders_from_model_tags(
    client, backends, monkeypatch
) -> None:
    codex, _ = backends
    replies = iter([
        CodexResult(
            '好，明天叫你。<remind when="2026-12-01 09:30" text="倒垃圾"/>', (), None, "t", False
        ),
        CodexResult('取消了。<cancel_reminder id="1"/>', (), None, "t", False),
        CodexResult('這個不行。<remind when="等一下" text="x"/>', (), None, "t", False),
    ])

    async def fake_codex(text, config, **kw):
        fake_codex.prompts.append(kw)
        return next(replies)

    fake_codex.prompts = []
    monkeypatch.setattr(bot_module, "run_codex", fake_codex)
    result = await client._answer("明天 9:30 提醒我倒垃圾", [], GUILD, USER, channel_id=555)
    assert result.text.startswith("好，明天叫你。\n\n⏰ 已設定 #1：12/01 09:30 提醒你：倒垃圾")
    assert [i["text"] for i in client.reminders.for_user(USER)] == ["倒垃圾"]
    result = await client._answer("把那個提醒取消", [], GUILD, USER, channel_id=555)
    assert "[待辦提醒]\n#1 12/01 09:30 倒垃圾" in fake_codex.prompts[-1]["memory"]
    assert result.text.endswith("⛔ 已取消提醒 #1") and client.reminders.for_user(USER) == []
    result = await client._answer("等一下提醒我", [], GUILD, USER, channel_id=555)
    assert "看不懂，提醒沒有設" in result.text


async def test_recall_loop_runs_sandbox_snippets_and_delivers_files(
    client, backends, monkeypatch, tmp_path
) -> None:
    from discord_codex_bot import sandbox as sandbox_module
    from discord_codex_bot.bot import request_only

    assert request_only('<run lang="python">print(1)</run>')
    client.config = replace(client.config, attachment_dir=tmp_path)
    replies = iter([
        CodexResult('<run lang="python">\nprint(6*7)\n</run>', (), None, "t1", False),
        CodexResult("答案是 42", (), None, "t1", True),
    ])
    prompts = []

    async def fake_codex(text, config, **kw):
        prompts.append((text, kw.get("images")))
        return next(replies)

    async def fake_run(lang, code, config, out_dir):
        out_dir.mkdir(parents=True, exist_ok=True)
        chart = out_dir / "chart.png"
        chart.write_bytes(b"png")
        return sandbox_module.RunResult(0, False, "42\n", "", [chart], [])

    monkeypatch.setattr(bot_module, "run_codex", fake_codex)
    monkeypatch.setattr(sandbox_module, "run_code", fake_run)
    result = await client._answer("算 6*7 並畫圖", [], GUILD, USER)
    assert result.text == "答案是 42"
    assert '<RESULT kind="run" lang="python" status="exit 0">\n42' in prompts[1][0]
    assert [p.name for p in prompts[1][1]] == ["chart.png"]  # the model sees the picture
    assert [p.name for p in result.images] == ["chart.png"] and result.generated_dir is not None
    assert result.images[0].exists()  # survives _answer's cleanup for the caller to send


async def test_recall_loop_calls_registered_apis_and_help_lists_them(
    client, backends, monkeypatch
) -> None:
    from discord_codex_bot import apis as apis_module
    from discord_codex_bot.bot import request_only

    client.apis = {"lol": apis_module.Api("lol", "https://x/", {}, "先 getLeagues")}
    assert "- lol：先 getLeagues" in client.help_sheet()
    assert request_only('<api name="lol" path="getLeagues"/>')
    replies = iter([
        CodexResult('<api name="lol" path="getLeagues?hl=zh-TW"/>', (), None, "t1", False),
        CodexResult("LCK 有 10 隊", (), None, "t1", True),
    ])
    prompts = []

    async def fake_codex(text, config, **kw):
        prompts.append(text)
        return next(replies)

    async def fake_call(name, path, registry, config):
        return '{"leagues":[{"name":"LCK"}]}'

    monkeypatch.setattr(bot_module, "run_codex", fake_codex)
    monkeypatch.setattr(apis_module, "call_api", fake_call)
    result = await client._answer("LCK 有幾隊", [], GUILD, USER)
    assert result.text == "LCK 有 10 隊"
    assert '<RESULT kind="api" name="lol" path="getLeagues?hl=zh-TW">\n{"leagues"' in prompts[1]


def test_configure_logging_writes_to_the_host_dir_and_survives_an_unusable_one(
    tmp_path: Path, config: Config
) -> None:
    from dataclasses import replace

    from discord_codex_bot.bot import configure_logging

    root = logging.getLogger()
    saved_handlers, saved_level = list(root.handlers), root.level
    root.handlers.clear()
    try:
        configure_logging(replace(config, log_dir=tmp_path / "logs", log_keep_days=3))
        logging.getLogger("probe").info("hello-log")
        for handler in root.handlers:
            handler.flush()
        assert "hello-log" in (tmp_path / "logs" / "bot.log").read_text("utf-8")
        rotating = [
            h for h in root.handlers
            if isinstance(h, logging.handlers.TimedRotatingFileHandler)
        ]
        assert len(rotating) == 1 and rotating[0].backupCount == 3
        for handler in root.handlers:
            handler.close()
        root.handlers.clear()
        # A LOG_DIR that cannot be created is a warning, never a reason not to start.
        blocker = tmp_path / "blocker"
        blocker.write_text("a file, not a directory", "utf-8")
        configure_logging(replace(config, log_dir=blocker / "logs"))
        assert root.handlers and not any(
            isinstance(h, logging.FileHandler) for h in root.handlers
        )
    finally:
        for handler in root.handlers:
            handler.close()
        root.handlers.clear()
        root.handlers.extend(saved_handlers)
        root.setLevel(saved_level)


async def test_redo_puts_back_the_message_the_question_pointed_at(
    client, backends, monkeypatch
) -> None:
    """A member who asks by replying to someone else's link has that link folded into the
    prompt. The redo must fold it back in, or it re-asks the question with its subject gone."""
    from types import SimpleNamespace as NS

    monkeypatch.setattr(type(client), "user", property(lambda self: NS(id=999)))

    class Response:
        async def defer(self, thinking=False):
            pass

        async def send_message(self, text, ephemeral=False):
            pass

    class Followup:
        async def send(self, text, files=None, view=None):
            return NS(id=9999)

    video = "https://www.youtube.com/watch?v=r28Uo9uWGSo"
    pointed = NS(content=video, embeds=[], attachments=[],
                 author=NS(display_name="030", id=5), reference=None, channel=None)

    async def fetch_pointed(message_id):
        return pointed

    asked = NS(content="<@999> 整理一下影片大綱", embeds=[], attachments=[],
               reference=NS(message_id=2, resolved=None),
               channel=NS(fetch_message=fetch_pointed))

    async def fetch_asked(message_id):
        return asked

    answer = NS(content="**問**：\n> 整理一下影片大綱\n\n看不到影片內容", id=4242,
                reference=NS(message_id=1, resolved=None),
                channel=NS(fetch_message=fetch_asked))
    interaction = NS(message=answer, guild_id=GUILD, channel_id=555,
                     response=Response(), followup=Followup())
    await client.handle_answer_button(interaction, "redo", USER)
    prompt = backends[0].calls[-1][0]
    assert video in prompt  # the link lived in the quoted message, not in the member's own words
    assert "整理一下影片大綱" in prompt and "030" in prompt


async def test_recall_loop_does_not_re_send_an_identical_api_query(
    client, backends, monkeypatch
) -> None:
    """One model sent the same Leaguepedia query three times and burned the anonymous quota to
    be told the same thing. A repeat gets the kept answer instead of another call."""
    from discord_codex_bot import apis as apis_module

    client.apis = {"lp": apis_module.Api("lp", "https://x/", {}, "doc")}
    tag = '<api name="lp" path="tables=ScoreboardGames"/>'
    replies = iter([
        CodexResult(tag, (), None, "t1", False),
        CodexResult(tag, (), None, "t1", False),
        CodexResult("來源被限流，稍後再問", (), None, "t1", True),
    ])
    prompts = []

    async def fake_codex(text, config, **kw):
        prompts.append(text)
        return next(replies)

    calls = []

    async def fake_call(name, path, registry, config):
        calls.append((name, path))
        return '{"error":{"code":"ratelimited"}}'

    monkeypatch.setattr(bot_module, "run_codex", fake_codex)
    monkeypatch.setattr(apis_module, "call_api", fake_call)
    result = await client._answer("誰第一個在職業賽用上路凱莎", [], GUILD, USER)
    assert result.text == "來源被限流，稍後再問"
    assert calls == [("lp", "tables=ScoreboardGames")]  # asked once, not once per round
    assert "沿用當時的結果" in prompts[2]
