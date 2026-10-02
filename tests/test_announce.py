from dataclasses import replace
from pathlib import Path

import discord

from discord_codex_bot.announce import announce_once, pending_announcement
from discord_codex_bot.config import Config

BOT_ID = 4242


class FakeMessage:
    def __init__(self, author_id, content):
        self.author, self.content = type("A", (), {"id": author_id})(), content


class FakeChannel:
    def __init__(self, cid, guild_id, readable=True):
        self.id, self.guild, self.sent = cid, type("G", (), {"id": guild_id})(), []
        self.messages: list[FakeMessage] = []  # oldest first
        self.readable = readable

    async def send(self, text):
        self.sent.append(text)
        self.messages.append(FakeMessage(BOT_ID, text.strip()))  # Discord trims the edges

    async def history(self, limit):
        if not self.readable:
            raise discord.Forbidden(type("R", (), {"status": 403, "reason": "Forbidden"})(), "no")
        for message in list(reversed(self.messages))[:limit]:
            yield message


class FakeClient:
    def __init__(self, channels):
        self._channels = {c.id: c for c in channels}
        self.user = type("U", (), {"id": BOT_ID})()

    def get_channel(self, cid):
        return self._channels.get(cid)


def _approved(tmp_path: Path, config: Config, text: str, channels: set[int]) -> Config:
    (tmp_path / "announce").mkdir(exist_ok=True)
    (tmp_path / "announce" / "latest.md").write_text(text, "utf-8")
    cfg = replace(
        config,
        announce_dir=tmp_path / "announce",
        codex_home=tmp_path,
        announce_channel_ids=frozenset(channels),
    )
    return replace(cfg, announce_approved=pending_announcement(cfg)[0])


async def test_announce_not_repeated_when_record_is_lost(tmp_path: Path, config: Config) -> None:
    # 2026-10-02: a start on an empty CODEX_HOME re-posted an approved announcement.
    good = FakeChannel(10, next(iter(config.allowed_guild_ids)))
    client = FakeClient([good])
    cfg = _approved(tmp_path, config, "新功能上線！", {10})
    assert await announce_once(client, cfg) == 1
    (tmp_path / "announced.json").unlink()
    good.messages += [FakeMessage(1, f"閒聊 {n}") for n in range(30)]
    assert await announce_once(client, cfg) == 0
    assert good.sent == ["新功能上線！"]
    assert (tmp_path / "announced.json").exists()  # the record is written back


async def test_announce_someone_elses_copy_does_not_count(tmp_path: Path, config: Config) -> None:
    good = FakeChannel(10, next(iter(config.allowed_guild_ids)))
    good.messages.append(FakeMessage(1, "新功能上線！"))
    cfg = _approved(tmp_path, config, "新功能上線！", {10})
    assert await announce_once(FakeClient([good]), cfg) == 1


async def test_announce_skips_channel_with_unreadable_history(
    tmp_path: Path, config: Config
) -> None:
    blind = FakeChannel(10, next(iter(config.allowed_guild_ids)), readable=False)
    cfg = _approved(tmp_path, config, "新功能上線！", {10})
    assert await announce_once(FakeClient([blind]), cfg) == 0
    assert blind.sent == []


async def test_announce_only_to_configured_channels_once(tmp_path: Path, config: Config) -> None:
    (tmp_path / "announce").mkdir()
    (tmp_path / "announce" / "latest.md").write_text("新功能上線！", "utf-8")
    base = replace(config, announce_dir=tmp_path / "announce", codex_home=tmp_path)
    assert pending_announcement(base)[1] == "新功能上線！"
    allowed = next(iter(config.allowed_guild_ids))
    good, foreign = FakeChannel(10, allowed), FakeChannel(11, 999)
    client = FakeClient([good, foreign])

    assert await announce_once(client, base) == 0  # nothing configured -> nothing posted
    cfg = replace(base, announce_channel_ids=frozenset({10, 11}))
    assert await announce_once(client, cfg) == 0  # configured but not approved -> nothing
    cfg = replace(cfg, announce_approved=pending_announcement(cfg)[0])
    assert await announce_once(client, cfg) == 1
    assert good.sent == ["新功能上線！"] and foreign.sent == []
    assert await announce_once(client, cfg) == 0  # same version -> silent
    (tmp_path / "announce" / "latest.md").write_text("第二版", "utf-8")
    assert await announce_once(client, cfg) == 0  # new content, old approval -> nothing
    cfg = replace(cfg, announce_approved=pending_announcement(cfg)[0])
    assert await announce_once(client, cfg) == 1 and good.sent[-1] == "第二版"


async def test_announce_cut_on_whitespace_is_still_recognised(
    tmp_path: Path, config: Config
) -> None:
    good = FakeChannel(10, next(iter(config.allowed_guild_ids)))
    client = FakeClient([good])
    cfg = _approved(tmp_path, config, "字" * 1999 + " 之後被截掉", {10})
    assert await announce_once(client, cfg) == 1
    (tmp_path / "announced.json").unlink()
    assert await announce_once(client, cfg) == 0
    assert len(good.sent) == 1
