"""The announcement queue (owner 2026-10-07): sent on purpose, at most once, never on start-up."""

import json
import types
from dataclasses import replace
from pathlib import Path

import discord
import pytest

from discord_codex_bot import announce
from discord_codex_bot.announce import (
    announce_pass,
    make_item,
    parse_time,
    queue_dir,
    write_item,
)
from discord_codex_bot.config import Config

NOW = parse_time("2026-10-07 12:00")


class Alerts:
    def __init__(self) -> None:
        self.sent: list[tuple[str, str]] = []

    async def alert_now(self, backend, kind, text, outage=False):
        self.sent.append((kind, text))


class Channel:
    def __init__(self, cid: int, guild_id: int, error: Exception | None = None) -> None:
        self.id, self.guild = cid, types.SimpleNamespace(id=guild_id)
        self.error = error
        self.sent: list[tuple[str, dict]] = []

    async def send(self, text, **kw):
        if self.error is not None:
            raise self.error
        self.sent.append((text, kw))


class Client:
    def __init__(self, *channels: Channel) -> None:
        self._channels = {c.id: c for c in channels}

    def get_channel(self, cid):
        return self._channels.get(cid)


@pytest.fixture
def cfg(config: Config, tmp_path: Path) -> Config:
    return replace(config, codex_home=tmp_path)


def guild(cfg: Config) -> int:
    return next(iter(cfg.allowed_guild_ids))


def http_error(status: int) -> discord.HTTPException:
    return discord.HTTPException(types.SimpleNamespace(status=status, reason="x"), "nope")


def files(cfg: Config) -> list[str]:
    return sorted(p.name for p in queue_dir(cfg).iterdir() if not p.name.startswith("."))


async def run(cfg, client, alerts=None, now=NOW, started_at=NOW - 60, first=False) -> int:
    return await announce_pass(client, cfg, alerts or Alerts(), started_at, now, first)


async def test_a_due_item_is_posted_once_and_removed(cfg) -> None:
    channel = Channel(10, guild(cfg))
    write_item(queue_dir(cfg), make_item(10, "📢 公告", now=NOW - 10), "a.json")
    assert await run(cfg, Client(channel)) == 1
    assert [text for text, _ in channel.sent] == ["📢 公告"]
    mentions = channel.sent[0][1]["allowed_mentions"]
    assert not (mentions.everyone or mentions.roles or mentions.users)  # pings no one
    assert files(cfg) == []
    assert await run(cfg, Client(channel)) == 0 and len(channel.sent) == 1


async def test_nothing_is_posted_just_because_the_bot_started(cfg) -> None:
    # The 2026-10-02 repost came from a start-up post; an empty queue posts nothing on start.
    channel = Channel(10, guild(cfg))
    assert await run(cfg, Client(channel), first=True) == 0 and channel.sent == []


async def test_a_timed_item_waits_and_a_late_one_is_set_aside(cfg) -> None:
    channel = Channel(10, guild(cfg))
    at = parse_time("2026-10-07 20:00")
    write_item(queue_dir(cfg), make_item(10, "晚上見", at=at, now=NOW), "t.json")
    assert await run(cfg, Client(channel), now=at - 60) == 0
    assert await run(cfg, Client(channel), now=at + 60) == 1
    write_item(queue_dir(cfg), make_item(10, "晚上見", at=at, now=NOW), "late.json")
    alerts = Alerts()
    # The host was paused past the 2-hour window: not posted late, the owner is told.
    assert await run(cfg, Client(channel), alerts, now=at + 3 * 3600) == 0
    assert files(cfg) == ["late.expired"] and alerts.sent[0][0] == "expired"


async def test_an_after_deploy_item_waits_for_a_later_start(cfg) -> None:
    channel = Channel(10, guild(cfg))
    write_item(queue_dir(cfg), make_item(10, "更新好了", after_deploy=True, now=NOW), "d.json")
    assert await run(cfg, Client(channel), now=NOW + 60, started_at=NOW - 3600) == 0
    assert await run(cfg, Client(channel), now=NOW + 600, started_at=NOW + 300) == 1


async def test_a_crash_while_sending_never_posts_again(cfg) -> None:
    # Owner: rather miss one than post twice. A `.sending` left by a crash is reported, not sent.
    channel = Channel(10, guild(cfg))
    write_item(queue_dir(cfg), make_item(10, "公告", now=NOW - 10), "c.sending")
    alerts = Alerts()
    assert await run(cfg, Client(channel), alerts, first=True) == 0
    assert channel.sent == [] and files(cfg) == ["c.sending"]
    assert alerts.sent[0][0] == "stuck"


async def test_an_unclear_failure_leaves_it_claimed(cfg) -> None:
    for error in (TimeoutError(), http_error(503)):
        channel = Channel(10, guild(cfg), error=error)
        write_item(queue_dir(cfg), make_item(10, "公告", now=NOW - 10), "u.json")
        alerts = Alerts()
        assert await run(cfg, Client(channel), alerts) == 0
        assert files(cfg) == ["u.sending"] and alerts.sent[-1][0] == "stuck"
        assert await run(cfg, Client(Channel(10, guild(cfg)))) == 0  # never retried
        (queue_dir(cfg) / "u.sending").unlink()


async def test_a_refused_send_goes_back_and_stops_after_three(cfg) -> None:
    # Discord refusing (4xx) means nothing was posted: safe to try again, a few times.
    channel = Channel(10, guild(cfg), error=http_error(403))
    write_item(queue_dir(cfg), make_item(10, "公告", now=NOW - 10), "r.json")
    alerts = Alerts()
    for _ in range(2):
        assert await run(cfg, Client(channel), alerts) == 0
        assert files(cfg) == ["r.json"]
    assert await run(cfg, Client(channel), alerts) == 0
    assert files(cfg) == ["r.failed"] and alerts.sent[-1][0] == "failed"
    assert json.loads((queue_dir(cfg) / "r.failed").read_text("utf-8"))["failures"] == 3


async def test_a_channel_outside_the_allowed_guilds_is_refused(cfg) -> None:
    stranger = Channel(10, 999)
    write_item(queue_dir(cfg), make_item(10, "公告", now=NOW - 10), "x.json")
    alerts = Alerts()
    assert await run(cfg, Client(stranger), alerts) == 0
    assert stranger.sent == [] and files(cfg) == ["x.bad"]


async def test_a_malformed_item_is_set_aside(cfg) -> None:
    queue_dir(cfg).mkdir(parents=True)
    (queue_dir(cfg) / "m.json").write_text('{"channel": 10}', "utf-8")
    alerts = Alerts()
    assert await run(cfg, Client(Channel(10, guild(cfg))), alerts) == 0
    assert files(cfg) == ["m.bad"] and alerts.sent[0][0] == "bad"


def test_items_are_checked_when_queued() -> None:
    with pytest.raises(ValueError, match="1-2000"):
        make_item(10, "x" * 2001, now=NOW)
    with pytest.raises(ValueError, match="1-2000"):
        make_item(10, "   ", now=NOW)
    with pytest.raises(ValueError, match="not both"):
        make_item(10, "a", at=NOW + 60, after_deploy=True, now=NOW)
    with pytest.raises(ValueError, match="not_after"):
        make_item(10, "a", at=NOW + 600, not_after=NOW + 60, now=NOW)
    assert make_item(10, "a", at=NOW + 60, now=NOW)["not_after"] == NOW + 60 + 2 * 3600


def test_the_queue_is_outside_the_image_and_the_backup(cfg) -> None:
    from discord_codex_bot import backup

    assert queue_dir(cfg) == cfg.codex_home / "announce-queue"
    assert not any("announce" in name for name in backup.BACKUP_MEMBERS)
    dockerfile = (Path(__file__).resolve().parent.parent / "Dockerfile").read_text("utf-8")
    assert "announce" not in dockerfile


async def test_send_now_checks_the_guild_and_pings_no_one(cfg, monkeypatch) -> None:
    calls: list[tuple[str, str, dict | None]] = []
    owner = {"guild": guild(cfg)}

    class Response:
        def __init__(self, status, body):
            self.status, self._body = status, body

        async def json(self):
            return self._body

        async def __aenter__(self):
            return self

        async def __aexit__(self, *exc):
            return False

    class Session:
        def __init__(self, **kw):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *exc):
            return False

        def get(self, url, headers):
            calls.append(("GET", url, None))
            return Response(200, {"guild_id": str(owner["guild"])})

        def post(self, url, headers, json):
            calls.append(("POST", url, json))
            return Response(200, {"id": "777"})

    monkeypatch.setattr(announce.aiohttp, "ClientSession", Session)
    assert await announce.send_now(cfg, 10, " 公告 ") == "777"
    assert calls[1] == (
        "POST",
        f"{announce.API}/channels/10/messages",
        {"content": "公告", "allowed_mentions": {"parse": []}},
    )
    owner["guild"] = 999
    with pytest.raises(RuntimeError, match="allowed guild"):
        await announce.send_now(cfg, 10, "公告")
    assert [c[0] for c in calls].count("POST") == 1
