from dataclasses import replace
from pathlib import Path

from discord_codex_bot.announce import announce_once, pending_announcement
from discord_codex_bot.config import Config


class FakeChannel:
    def __init__(self, cid, guild_id):
        self.id, self.guild, self.sent = cid, type("G", (), {"id": guild_id})(), []

    async def send(self, text):
        self.sent.append(text)


class FakeClient:
    def __init__(self, channels):
        self._channels = {c.id: c for c in channels}

    def get_channel(self, cid):
        return self._channels.get(cid)


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
