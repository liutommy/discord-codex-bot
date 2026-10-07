"""Custom emoji understanding (hub spec 2026-10-07): samples, descriptions, the import, the
backfill, and how a message's emoji reach the model."""

from __future__ import annotations

import json
import logging
import types
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path

import discord
import pytest

from discord_codex_bot import emoji as emoji_module
from discord_codex_bot.bot import DiscordCodexClient
from discord_codex_bot.config import Config, load_config
from discord_codex_bot.emoji import (
    INSUFFICIENT,
    KEEP_SAMPLES,
    EmojiStore,
    backfill_channel,
    describe_pending,
    describe_prompt,
    import_descriptions,
)

GUILD, OTHER = 111111111111111111, 999999999999999999
PEPE, CAT, PARTY, FOREIGN = (100000000000000001, 100000000000000002, 100000000000000003,
                             100000000000000009)  # fmt: skip
NOW = datetime(2026, 10, 7, 12, tzinfo=UTC)


@pytest.fixture
def store(tmp_path: Path) -> EmojiStore:
    store = EmojiStore(tmp_path / "emoji.sqlite3")
    store.sync(GUILD, [(PEPE, "pepe", False), (CAT, "cat", False), (PARTY, "party", True)])
    store.sync(OTHER, [(FOREIGN, "theirs", False)])
    return store


def _sample(store: EmojiStore, emoji_id: int, message_id: int, at: float = 1.0, **kw) -> bool:
    return store.add_sample(
        kw.pop("guild", GUILD), emoji_id, kw.pop("kind", "message"), message_id, 3,
        kw.pop("text", "好耶"), kw.pop("previous", "前一則"), at, **kw,
    )  # fmt: skip


# ----------------------------------------------------------------------------- samples


def test_only_the_guilds_own_emoji_still_on_it_are_sampled(store: EmojiStore) -> None:
    assert _sample(store, PEPE, 1)
    assert not _sample(store, FOREIGN, 2)  # another server's emoji (Nitro)
    assert not _sample(store, 123456789012345678, 3)  # unknown
    store.sync(GUILD, [(CAT, "cat", False)])  # pepe was removed from the server
    assert not _sample(store, PEPE, 4)
    assert store.known(GUILD, CAT) and not store.known(GUILD, PEPE)


def test_a_sample_counts_once_and_only_the_newest_hundred_are_kept(store: EmojiStore) -> None:
    for n in range(KEEP_SAMPLES + 20):
        assert _sample(store, PEPE, 1000 + n, at=float(n))
    assert not _sample(store, PEPE, 1000 + KEEP_SAMPLES + 19)  # the same message again
    kept = store.samples(PEPE)
    assert len(kept) == KEEP_SAMPLES and kept[0].at == KEEP_SAMPLES + 19 and kept[-1].at == 20
    assert store.get(PEPE).uses == KEEP_SAMPLES + 20 and store.get(PEPE).added == KEEP_SAMPLES + 20


def test_long_messages_are_clipped(store: EmojiStore) -> None:
    _sample(store, PEPE, 1, text="字" * 2000, previous="前" * 2000)
    sample = store.samples(PEPE)[0]
    assert len(sample.text) == emoji_module.TEXT_MAX + 1 and len(sample.previous) == 501


def test_a_deleted_message_takes_its_samples_with_it(store: EmojiStore) -> None:
    _sample(store, PEPE, 1)
    _sample(store, CAT, 1)
    _sample(store, PEPE, 2)
    assert store.forget_messages([1]) == 2
    assert [s.text for s in store.samples(PEPE)] == ["好耶"] and store.samples(CAT) == []


# ----------------------------------------------------------------------------- prompts


def test_a_message_reads_its_emoji_by_meaning(store: EmojiStore) -> None:
    store.describe(PEPE, "綠色青蛙，用來表示無奈", False, "bot:x")
    store.describe(CAT, "一隻貓", True, "bot:x")
    store.describe(PARTY, "不該出現", False, "bot:x")  # animated: never shown
    text = "<:pepe:100000000000000001> 和 <:cat:100000000000000002> <a:party:100000000000000003>"
    text += " <:theirs:100000000000000009> <:new:123456789012345678>"
    assert store.rewrite(text, GUILD) == (
        f":pepe:（綠色青蛙，用來表示無奈） 和 :cat:（一隻貓；{INSUFFICIENT}） :party:"
        " :theirs: :new:"
    )
    assert store.rewrite("沒有表情", GUILD) == "沒有表情"


def test_another_servers_emoji_never_brings_its_description(store: EmojiStore) -> None:
    # Both servers are ours: a member of one using the other's emoji (Nitro) got the other's
    # summary of how its members talk (hub on PR #40).
    store.describe(FOREIGN, "那邊的用法", False, "bot:x")
    store.describe(PEPE, "青蛙", False, "bot:x")
    text = f"<:theirs:{FOREIGN}> <:pepe:{PEPE}>"
    assert store.rewrite(text, GUILD) == ":theirs: :pepe:（青蛙）"
    assert store.rewrite(text, OTHER) == ":theirs:（那邊的用法） :pepe:"
    store.sync(GUILD, [(CAT, "cat", False)])  # pepe was taken off the server
    assert store.rewrite(text, GUILD) == ":theirs: :pepe:"


def test_the_list_carries_short_descriptions_and_a_message_the_whole(store) -> None:
    # The list goes into every prompt of the server: 50 × 300 characters was up to 15k (hub).
    store.describe(PEPE, "長" * 100, False, "bot:x")
    (listed,) = [line for line in store.block(GUILD).splitlines() if line.startswith(":pepe:")]
    assert listed == ":pepe:（" + "長" * (emoji_module.LISTED_MAX - 1) + "…）"
    assert store.rewrite(f"<:pepe:{PEPE}>", GUILD) == ":pepe:（" + "長" * 100 + "）"


def test_the_bots_descriptions_are_not_the_members_words(store, tmp_path, config) -> None:
    # The description written into a message reached harvest and the digest as something the
    # member said (hub on PR #40); a full-width bracket inside it would end it early.
    from discord_codex_bot import harvest
    from discord_codex_bot.codex import _prompt

    store.describe(PEPE, "綠色青蛙（很有名）\n表示無奈", False, "bot:x")
    assert store.get(PEPE).description == "綠色青蛙(很有名) 表示無奈"
    asked = store.rewrite(f"我覺得 <:pepe:{PEPE}> 這很好笑 <:cat:{CAT}>", GUILD)
    assert asked == "我覺得 :pepe:（綠色青蛙(很有名) 表示無奈） 這很好笑 :cat:"
    day = tmp_path / "sessions" / "2026" / "10" / "07"
    day.mkdir(parents=True)
    payload = {"type": "message", "role": "user",
               "content": [{"type": "input_text", "text": _prompt(asked, speaker=3)}]}  # fmt: skip
    (day / "rollout-2026-10-07T10-00-00-t3.jsonl").write_text(
        json.dumps({"type": "response_item", "payload": payload}, ensure_ascii=False), "utf-8"
    )
    turns = harvest.transcript_turns(replace(config, codex_home=tmp_path), "t3")
    assert [t.text for t in turns if t.role == "user"] == ["我覺得 :pepe: 這很好笑 :cat:"]


def test_memory_lists_the_most_used_static_emoji(store: EmojiStore, monkeypatch) -> None:
    for n in range(3):
        _sample(store, CAT, 10 + n)
    _sample(store, PEPE, 20)
    store.describe(CAT, "一隻貓", False, "bot:x")
    assert store.block(GUILD) == "[伺服器表情]\n:cat:（一隻貓）\n:pepe:"  # party is animated
    assert store.block(None) == "" and store.block(42) == ""
    monkeypatch.setattr(emoji_module, "PROMPT_MAX", 1)
    assert store.listing(GUILD, 1)[0].name == "cat"


# ----------------------------------------------------------------------------- descriptions


async def test_only_emoji_with_new_samples_are_described(store: EmojiStore, tmp_path) -> None:
    for n in range(3):
        _sample(store, PEPE, 10 + n, text=f"<:pepe:{PEPE}> 第{n}則")
    _sample(store, CAT, 20)
    _sample(store, PARTY, 30)  # animated: never described
    asked: list[tuple[str, Path]] = []

    async def describer(prompt: str, image: Path) -> str:
        asked.append((prompt, image))
        _sample(store, PEPE, 99)  # arrives while the description is being written
        return json.dumps({"description": "說明", "insufficient": False})

    async def image(emoji):
        path = tmp_path / f"{emoji.emoji_id}.png"
        path.write_bytes(b"png")
        return path

    stats = await describe_pending(store, describer, image, 10, "bot:gpt")
    assert stats == {"described": 2, "failed": 0, "no_image": 0}
    assert {p.name for _q, p in asked} == {f"{PEPE}.png", f"{CAT}.png"}
    assert not any(p.exists() for _q, p in asked)  # the images are not kept
    assert ":pepe: 第0則" in asked[0][0]  # emoji in samples read by name
    pepe, cat = store.get(PEPE), store.get(CAT)
    assert (pepe.description, pepe.insufficient, pepe.source) == ("說明", False, "bot:gpt")
    assert cat.insufficient  # one sample: looks only, whatever the model said
    assert pepe.added == 1 and cat.added == 0  # pepe's late sample still counts as new
    assert [e.emoji_id for e in store.pending()] == [PEPE]


async def test_a_failed_description_stays_pending(store: EmojiStore, tmp_path) -> None:
    _sample(store, PEPE, 1)

    async def broken(prompt: str, image: Path) -> str:
        return "not json"

    async def image(emoji):
        (tmp_path / "x.png").write_bytes(b"png")
        return tmp_path / "x.png"

    async def no_image(emoji):
        return None

    assert (await describe_pending(store, broken, image, 10, "b"))["failed"] == 1
    assert (await describe_pending(store, broken, no_image, 10, "b"))["no_image"] == 1
    assert store.get(PEPE).description == "" and store.pending()[0].emoji_id == PEPE


def test_the_prompt_carries_the_rules_and_the_samples(store: EmojiStore) -> None:
    _sample(store, PEPE, 1, kind="reaction", text="被按的訊息", previous="")
    prompt = describe_prompt(store.get(PEPE), store.samples(PEPE))
    assert "With fewer than 3 examples, describe only what it looks like" in prompt
    assert '"kind": "reaction", "text": "被按的訊息"' in prompt and '"name": "pepe"' in prompt


# ----------------------------------------------------------------------------- import


def test_descriptions_written_elsewhere_are_checked_and_marked(store: EmojiStore) -> None:
    for n in range(3):
        _sample(store, PEPE, 10 + n)
    _sample(store, CAT, 20)
    lines = [
        json.dumps({"guild_id": GUILD, "emoji_id": PEPE, "description": "青蛙，表示無奈",
                    "insufficient": False, "source": "claude agent", "generated_at": 5.0}),
        json.dumps({"guild_id": GUILD, "emoji_id": CAT, "description": "一隻貓"}),
        json.dumps({"guild_id": GUILD, "emoji_id": FOREIGN, "description": "x"}),
        json.dumps({"guild_id": OTHER, "emoji_id": PEPE, "description": "x"}),
        json.dumps({"guild_id": GUILD, "emoji_id": PARTY, "description": "x"}),
        json.dumps({"guild_id": GUILD, "emoji_id": PEPE, "description": " "}),
        "not json",
        "",
    ]  # fmt: skip
    report = import_descriptions(store, lines)
    assert report.startswith("imported 2; rejected 5")
    assert "not an emoji of that guild ×2" in report and "animated ×1" in report
    pepe, cat = store.get(PEPE), store.get(CAT)
    assert (pepe.description, pepe.insufficient, pepe.source) == (
        "青蛙，表示無奈", False, "import:claude_agent")  # fmt: skip
    assert cat.insufficient and cat.description == "一隻貓"  # one sample: marked anyway
    assert store.pending() == []


def test_an_emoji_gone_from_the_server_is_not_imported(store: EmojiStore) -> None:
    store.sync(GUILD, [(CAT, "cat", False)])
    line = json.dumps({"guild_id": GUILD, "emoji_id": PEPE, "description": "x"})
    assert "no longer on the server ×1" in import_descriptions(store, [line])


# ----------------------------------------------------------------------------- backfill


class _Reaction:
    def __init__(self, emoji_id: int, count: int, animated: bool = False) -> None:
        self.emoji = types.SimpleNamespace(id=emoji_id, animated=animated)
        self.count = count


def _message(n: int, content: str, bot: bool = False, reactions=()) -> types.SimpleNamespace:
    return types.SimpleNamespace(
        id=1_000 + n,
        content=content,
        author=types.SimpleNamespace(bot=bot),
        reactions=list(reactions),
        created_at=NOW - timedelta(days=200 - n),
    )


class _History:
    """history(): newest first, before/after honoured, like discord.py's."""

    def __init__(self, messages, fail_after: int | None = None) -> None:
        self.id, self.name = 3, "閒聊"
        self.messages = messages
        self.fail_after = fail_after
        self.calls: list[tuple[int, datetime]] = []

    def history(self, *, limit, before, after, oldest_first):
        assert limit is None and oldest_first is False
        self.calls.append((before.id, after))
        chosen = [m for m in self.messages if m.id < before.id and m.created_at > after]
        chosen.sort(key=lambda m: m.id, reverse=True)
        fail_after = self.fail_after

        async def walk():
            for i, message in enumerate(chosen):
                if fail_after is not None and i == fail_after:
                    raise discord.HTTPException(types.SimpleNamespace(status=500, reason="x"), "x")
                yield message

        return walk()


async def _no_sleep(_seconds):
    return None


async def test_the_backfill_pairs_each_message_with_the_one_before_it(store) -> None:
    messages = [
        _message(100, "開場"),
        _message(101, f"笑死 <:pepe:{PEPE}> <:pepe:{PEPE}>"),
        _message(102, "機器人說話", bot=True, reactions=[_Reaction(CAT, 4)]),
        _message(103, f"<:cat:{CAT}> <a:party:{PARTY}>", reactions=[_Reaction(PARTY, 2, True)]),
        _message(104, f"<:theirs:{FOREIGN}>"),
    ]
    channel = _History(messages)
    messages_read, samples = await backfill_channel(store, GUILD, channel, NOW, 150, 0, _no_sleep)
    assert (messages_read, samples) == (5, 3)
    assert [(s.kind, s.text, s.previous) for s in store.samples(PEPE)] == [
        ("message", f"笑死 <:pepe:{PEPE}> <:pepe:{PEPE}>", "開場")
    ]
    cat = sorted((s.kind, s.text, s.previous) for s in store.samples(CAT))
    assert cat == [("message", f"<:cat:{CAT}> <a:party:{PARTY}>", "機器人說話"),
                   ("reaction", "機器人說話", "")]  # fmt: skip
    assert store.get(CAT).uses == 5  # four reactions and one message
    assert store.samples(PARTY) == []  # animated: not sampled
    assert channel.calls[0][1] == NOW - timedelta(days=150)


async def test_an_interrupted_backfill_goes_on_from_where_it_stopped(store, monkeypatch) -> None:
    monkeypatch.setattr(emoji_module, "BACKFILL_PAGE", 2)
    messages = [_message(n, f"<:pepe:{PEPE}> {n}") for n in range(10)]
    broken = _History(messages, fail_after=6)
    with pytest.raises(discord.HTTPException):
        await backfill_channel(store, GUILD, broken, NOW, 365, 0, _no_sleep)
    saved = store.progress(GUILD, 3)
    assert saved["days"] == 0 and saved["cursor"] is not None
    channel = _History(messages)
    await backfill_channel(store, GUILD, channel, NOW, 365, 0, _no_sleep)
    assert channel.calls[0][0] == saved["cursor"]  # not from the top again
    assert len(store.samples(PEPE)) == 10 and store.get(PEPE).uses == 10  # no duplicates
    assert store.progress(GUILD, 3)["days"] == 365
    assert await backfill_channel(store, GUILD, channel, NOW, 365, 0, _no_sleep) == (0, 0)


async def test_a_longer_backfill_reads_on_from_the_oldest_message_read(store) -> None:
    messages = [_message(n, f"<:pepe:{PEPE}>") for n in range(0, 200, 20)]  # one per 20 days
    channel = _History(messages)
    await backfill_channel(store, GUILD, channel, NOW, 90, 0, _no_sleep)
    first = len(store.samples(PEPE))
    await backfill_channel(store, GUILD, channel, NOW, 365, 0, _no_sleep)
    assert first == 4  # the four within 90 days
    assert channel.calls[1][0] == 1_120  # on from the oldest of them, not from the top
    assert len(store.samples(PEPE)) == 10
    assert channel.calls[1][1] == NOW - timedelta(days=365)


# ----------------------------------------------------------------------------- the Bot


class _Text(discord.TextChannel):
    def __init__(self, channel_id: int, name: str, public: bool = True, readable: bool = True):
        self.id, self.name, self._public, self._readable = channel_id, name, public, readable
        self.messages: list = []
        self.reads = 0  # history() calls: one REST request each

    def permissions_for(self, who) -> discord.Permissions:
        if who == "everyone":
            return discord.Permissions(view_channel=self._public)
        return discord.Permissions(view_channel=True, read_message_history=self._readable)

    def history(self, *, limit, before=None, after=None, oldest_first=None):
        self.reads += 1
        chosen = [m for m in self.messages if before is None or m.id < before.id]
        chosen.sort(key=lambda m: m.id, reverse=True)

        async def walk():
            for message in chosen[: limit or None]:
                yield message

        return walk()

    async def fetch_message(self, message_id: int):
        return next(m for m in self.messages if m.id == message_id)

    def archived_threads(self, limit=None):
        async def none():
            return
            yield

        return none()


@pytest.fixture
def bot(config: Config, tmp_path, monkeypatch) -> DiscordCodexClient:
    cfg = replace(
        config,
        codex_home=tmp_path / "codex",
        permanent_memory_dir=tmp_path / "perm",
        attachment_dir=tmp_path / "att",
        emoji_enabled=True,
        emoji_db_path=tmp_path / "emoji.sqlite3",
    )
    client = DiscordCodexClient(cfg)
    client.emoji.sync(GUILD, [(PEPE, "pepe", False), (CAT, "cat", False)])
    guild = types.SimpleNamespace(id=GUILD, default_role="everyone", me="bot")
    monkeypatch.setattr(client, "get_guild", lambda guild_id: guild if guild_id == GUILD else None)
    return client


def _incoming(n: int, channel, content: str, bot: bool = False):
    message = _message(n, content, bot=bot)
    message.guild = types.SimpleNamespace(id=GUILD)
    message.channel = channel
    channel.messages.append(message)
    return message


async def test_members_messages_in_public_channels_are_sampled(bot) -> None:
    public, private = _Text(3, "閒聊"), _Text(4, "幹部", public=False)
    await bot._collect_emoji(_incoming(1, public, f"<:pepe:{PEPE}> 第一則"))  # asks history
    await bot._collect_emoji(_incoming(2, public, "Bot 的回答", bot=True))
    await bot._collect_emoji(_incoming(3, public, f"又是 <:pepe:{PEPE}>"))
    await bot._collect_emoji(_incoming(4, private, f"<:cat:{CAT}> 私下講"))
    await bot._collect_emoji(_incoming(5, public, f"<:pepe:{PEPE}>", bot=True))  # a bot's own
    assert [(s.text, s.previous) for s in bot.emoji.samples(PEPE)] == [
        (f"又是 <:pepe:{PEPE}>", "Bot 的回答"),
        (f"<:pepe:{PEPE}> 第一則", ""),
    ]
    assert bot.emoji.samples(CAT) == []  # not public: never sampled
    assert public.reads == 1  # only the first message asked Discord for the one before it


async def test_a_reaction_samples_the_message_it_was_put_on_once(bot, monkeypatch) -> None:
    channel = _Text(3, "閒聊")
    target = _incoming(1, channel, "今天下雨")
    monkeypatch.setattr(bot, "get_channel", lambda channel_id: channel)
    bot._connection.user = types.SimpleNamespace(id=77)

    def payload(emoji_id: int, user_id: int = 5, animated: bool = False):
        return types.SimpleNamespace(
            emoji=types.SimpleNamespace(id=emoji_id, animated=animated),
            guild_id=GUILD, channel_id=3, message_id=target.id, user_id=user_id,
        )  # fmt: skip

    await bot.on_raw_reaction_add(payload(CAT))
    await bot.on_raw_reaction_add(payload(CAT, user_id=6))
    await bot.on_raw_reaction_add(payload(CAT, user_id=77))  # the Bot's own reaction
    await bot.on_raw_reaction_add(payload(FOREIGN))
    assert [(s.kind, s.text) for s in bot.emoji.samples(CAT)] == [("reaction", "今天下雨")]
    assert bot.emoji.get(CAT).uses == 2
    await bot.on_raw_message_delete(types.SimpleNamespace(message_id=target.id))
    assert bot.emoji.samples(CAT) == []


async def test_the_backfill_reports_channels_it_cannot_read(bot, monkeypatch, caplog) -> None:
    readable, closed, private = (
        _Text(3, "閒聊"),
        _Text(4, "公告", readable=False),
        _Text(5, "幹部", public=False),
    )
    _incoming(1, readable, f"<:pepe:{PEPE}>")
    guild = types.SimpleNamespace(
        id=GUILD, default_role="everyone", me="bot", text_channels=[readable, closed, private],
        threads=[], forums=[],
    )  # fmt: skip
    monkeypatch.setattr(bot, "get_guild", lambda guild_id: guild)
    monkeypatch.setattr(DiscordCodexClient, "guilds", property(lambda self: [guild]))
    caplog.set_level(logging.INFO)
    await bot._backfill_emoji()
    assert "no Read Message History in 1 channel(s): #公告 (4)" in caplog.text
    assert "1 channel(s) not public, not read: #幹部 (5)" in caplog.text
    assert len(bot.emoji.samples(PEPE)) == 1 and bot.emoji.meta("backfill_anchor")
    assert "Emoji backfill guild=111111111111111111 done back 90 days" in caplog.text


async def test_answers_read_emoji_by_meaning_and_memory_lists_them(bot, monkeypatch) -> None:
    from discord_codex_bot import bot as bot_module
    from discord_codex_bot.codex import CodexResult

    bot.emoji.describe(PEPE, "綠色青蛙，表示無奈", False, "bot:x")
    seen: list[tuple[str, dict]] = []

    async def codex(prompt, config, **kw):
        seen.append((prompt, kw))
        return CodexResult("好", thread_id="t")

    monkeypatch.setattr(bot_module, "run_codex", codex)
    await bot._answer(f"這是什麼意思 <:pepe:{PEPE}>", [], GUILD, 5)
    prompt, kw = seen[0]
    assert prompt == "這是什麼意思 :pepe:（綠色青蛙，表示無奈）"
    assert "[伺服器表情]\n:cat:\n:pepe:（綠色青蛙，表示無奈）" in kw["memory"]  # by name at 0 uses
    assert kw["images"] == []  # never the emoji's picture


def test_emoji_settings_default_off_and_bounded(tmp_path) -> None:
    base = {"DISCORD_TOKEN": "t", "DISCORD_APPLICATION_ID": "123456789012345678",
            "ALLOWED_GUILD_IDS": str(GUILD), "CODEX_HOME": str(tmp_path)}  # fmt: skip
    config = load_config(base)
    assert not config.emoji_enabled and config.emoji_backfill_days == 90
    assert config.emoji_db_path == tmp_path / "emoji.sqlite3" and config.emoji_describe_hour == 4
    assert load_config({**base, "EMOJI_DESCRIBE_HOUR": "-1"}).emoji_describe_hour == -1
    with pytest.raises(ValueError):
        load_config({**base, "EMOJI_BACKFILL_DAYS": "-5"})


def test_the_emoji_database_is_backed_up(tmp_path: Path, config: Config) -> None:
    import tarfile

    from discord_codex_bot.backup import make_backup

    store = EmojiStore(tmp_path / "home" / "emoji.sqlite3")
    store.sync(GUILD, [(PEPE, "pepe", False)])
    cfg = replace(
        config,
        codex_home=tmp_path / "home",
        backup_dir=tmp_path / "backups",
        tracking_db_path=tmp_path / "home" / "tracking.sqlite3",
        emoji_db_path=tmp_path / "home" / "emoji.sqlite3",
    )
    target = make_backup(cfg, now=1_700_000_000)
    with tarfile.open(target) as tar:
        assert "emoji.sqlite3" in tar.getnames()


async def test_each_ready_syncs_the_emoji_and_runs_one_backfill(bot, monkeypatch) -> None:
    # Codex on PR #40 feared the task attribute shadowed the method: a ready must start exactly
    # one backfill, and a second ready while it runs must not start another.
    import asyncio

    started: list[int] = []
    gate = asyncio.Event()

    async def backfill():
        started.append(1)
        await gate.wait()

    guild = types.SimpleNamespace(id=GUILD, emojis=[types.SimpleNamespace(
        id=CAT, name="cat", animated=False)])  # fmt: skip
    monkeypatch.setattr(DiscordCodexClient, "guilds", property(lambda self: [guild]))
    monkeypatch.setattr(bot, "_backfill_emoji", backfill)
    bot._start_emoji()
    bot._start_emoji()
    await asyncio.sleep(0)
    assert started == [1] and not bot.emoji.known(GUILD, PEPE)  # synced: pepe is gone
    gate.set()
    await bot._emoji_backfill_task
    bot._start_emoji()
    await asyncio.sleep(0)
    assert started == [1, 1]  # the next ready goes on from where it stopped
    await bot._emoji_backfill_task


async def test_archived_threads_discord_will_not_list_are_logged(bot, monkeypatch, caplog) -> None:
    # Codex on PR #40: a failed listing dropped those threads without a word.
    class Closed(_Text):
        def archived_threads(self, limit=None):
            async def refuse():
                raise discord.Forbidden(types.SimpleNamespace(status=403, reason="x"), "x")
                yield

            return refuse()

    closed = Closed(6, "封存")
    guild = types.SimpleNamespace(
        id=GUILD, default_role="everyone", me="bot", text_channels=[closed], threads=[], forums=[]
    )
    monkeypatch.setattr(bot, "get_guild", lambda guild_id: guild)
    monkeypatch.setattr(DiscordCodexClient, "guilds", property(lambda self: [guild]))
    caplog.set_level(logging.INFO)
    await bot._backfill_emoji()
    assert "archived threads not listed in 1 channel(s): #封存 (6): 403" in caplog.text


def test_help_explains_emoji_only_while_it_is_on(bot, config) -> None:
    assert "看得懂伺服器表情" in bot.help_guide() and "看得懂伺服器表情" in bot.help_sheet()
    off = DiscordCodexClient(replace(bot.config, emoji_enabled=False))
    assert "看得懂伺服器表情" not in off.help_guide() + off.help_sheet()
