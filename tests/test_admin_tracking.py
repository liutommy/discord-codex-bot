import json
import logging
import types
from dataclasses import replace

import discord
import pytest

from discord_codex_bot import admin, admin_tracking
from discord_codex_bot.admin_server import AdminServer
from discord_codex_bot.bot import DiscordCodexClient
from discord_codex_bot.tracking import ContentItem, DecisionInput, FetchResult

GUILD, OTHER_GUILD = 111111111111111111, 333333333333333333
CHANNEL = 222222222222222222  # the conftest allowlisted channel
MEMBER, FRIEND, STRANGER = 444444444444444444, 666666666666666666, 555555555555555555
SOURCE = "https://www.youtube.com/@HoushouMarine"


def _http(kind, status: int):
    return kind(types.SimpleNamespace(status=status, reason="x"), "x")


def _channel(channel_id: int, guild, parent_id: int | None = None):
    """A channel every member can see until a test hides it from one (`hidden`)."""
    channel = types.SimpleNamespace(id=channel_id, guild=guild, parent_id=parent_id, hidden=set())
    channel.permissions_for = lambda member: types.SimpleNamespace(
        view_channel=member.id not in channel.hidden, manage_threads=False
    )
    return channel


class FakeGuild:
    def __init__(self, guild_id: int, members: set[int]) -> None:
        self.id = guild_id
        self.members = members

    def get_member(self, user_id: int):
        return None  # an empty member cache: the API decides

    async def fetch_member(self, user_id: int):
        if user_id not in self.members:
            raise _http(discord.NotFound, 404)
        return types.SimpleNamespace(id=user_id, guild=self)


@pytest.fixture
def bot(config, tmp_path, monkeypatch) -> DiscordCodexClient:
    client = DiscordCodexClient(
        replace(
            config,
            codex_home=tmp_path / "codex",
            tracking_enabled=True,
            tracking_db_path=tmp_path / "tracking.sqlite3",
            tracking_max_per_user=3,
        )
    )
    guild = FakeGuild(GUILD, {MEMBER, FRIEND})
    channels = {
        CHANNEL: _channel(CHANNEL, guild),
        777: _channel(777, guild),  # not allowlisted
    }

    async def fetch_channel(channel_id):
        if channel_id not in channels:
            raise _http(discord.NotFound, 404)
        return channels[channel_id]

    resolved: list[str] = []

    async def resolve(locator):
        resolved.append(locator)
        return f"UC{len(resolved)}", {"title": "Marine"}

    monkeypatch.setattr(client, "get_channel", lambda channel_id: None)
    monkeypatch.setattr(client, "fetch_channel", fetch_channel)
    monkeypatch.setattr(client, "get_guild", lambda guild_id: guild if guild_id == GUILD else None)
    monkeypatch.setattr(client.youtube_tracker, "resolve", resolve)
    client.resolved = resolved
    client.fake_guild = guild
    client.fake_channel = channels[CHANNEL]
    return client


@pytest.fixture
def server(bot, tmp_path) -> AdminServer:
    server = AdminServer(bot, tmp_path / "admin", admin_tracking.HANDLERS, admin_tracking.SCHEMAS)
    server.prepare()
    return server


async def _run(server: AdminServer, command: str, args: dict) -> tuple[str, dict]:
    """One request through the inbox, as the CLI sends it: validated, context-checked, run."""
    request_id = admin.write_request(server.root, command, args)
    await server.pass_once()
    await server.drain()
    return admin.read_result(server.root, request_id)


def _add_args(**changes) -> dict:
    return {"guild": GUILD, "channel": CHANNEL, "user": MEMBER, "source": SOURCE, **changes}


def _judge(store, watch_id: int, external_id: str, *, notify: bool, reason: str = "符合政策"):
    """One new item on the watch's source and the classifier's decision on it."""
    [watch] = [watch for watch in store.watches() if watch.id == watch_id]
    source = store.get_source(watch.source_id)
    if not source.baseline_complete:  # the first read is the baseline and never judged
        store.ingest(source, FetchResult((), "", {"title": "Marine"}))
    item = ContentItem(
        None,
        source.id,
        external_id,
        f"https://example.test/{external_id}",
        f"標題 {external_id}",
        "description",
        "2026-10-01T00:00:00+00:00",
        "video",
    )
    store.ingest(
        store.get_source(source.id), FetchResult((item,), external_id, {"title": "Marine"})
    )
    [(pending, items)] = store.pending_by_watch(watch.id)
    store.save_decisions(
        pending,
        items,
        [DecisionInput(external_id, notify, 0.9, "重大公告", reason, ())],
    )


def test_every_tracking_command_has_a_handler_and_a_cli_form() -> None:
    expected = {
        "track.add",
        "track.list",
        "track.cancel",
        "track.every",
        "track.at",
        "track.log",
        "tracking.outbox",
    }
    assert set(admin_tracking.SCHEMAS) == set(admin_tracking.HANDLERS) == expected
    for command, schema in admin_tracking.SCHEMAS.items():
        admin.check_schema(command, schema)
    parser = admin.build_parser(admin_tracking.SCHEMAS)
    argv = ["track", "add", "--guild", str(GUILD), "--channel", str(CHANNEL), "--user"]
    argv += [str(MEMBER), "--source", SOURCE, "--who", str(FRIEND), "--at", "12:01,20:01"]
    namespace = parser.parse_args(argv)
    assert admin._arguments(namespace, admin_tracking.SCHEMAS["track.add"]) == _add_args(
        who=[FRIEND], at="12:01,20:01"
    )
    assert parser.parse_args(["tracking", "outbox", "--failed"]).command == "tracking.outbox"


async def test_track_add_creates_the_watch_as_the_member(bot, server) -> None:
    status, document = await _run(server, "track.add", _add_args(who=[FRIEND], every=180))
    assert status == "done" and document["ok"] is True
    assert (
        "已新增追蹤 #1" in document["result"]["text"]
        and f"<@{FRIEND}>" in document["result"]["text"]
    )
    [watch] = bot.tracker.watches(user_id=MEMBER)
    assert (watch.guild_id, watch.channel_id, watch.user_id) == (GUILD, CHANNEL, MEMBER)
    assert watch.mention_ids == (FRIEND,) and watch.interval_minutes == 180
    assert document["result"]["id"] == watch.id

    _status, timed = await _run(server, "track.add", _add_args(source=SOURCE + "2", at="1201 2001"))
    assert timed["ok"] is True and "每天 12:01、20:01 判斷" in timed["result"]["text"]
    assert bot.tracker.watches(user_id=MEMBER)[-1].times == ("12:01", "20:01")


@pytest.mark.parametrize(
    ("changes", "reason"),
    [
        ({"at": "中午"}, "--at 看不懂"),
        ({"who": [FRIEND, STRANGER]}, "不是伺服器"),
        ({"user": STRANGER}, "不是伺服器"),
        ({"channel": 777}, "此頻道目前未開放"),
        ({"guild": OTHER_GUILD}, "不在允許清單"),
        ({"every": 0}, "--every 不可小於 1"),
    ],
)
async def test_track_add_refuses_before_resolving_anything(bot, server, changes, reason) -> None:
    status, document = await _run(server, "track.add", _add_args(**changes))
    assert status == "rejected" and reason in document["reason"]
    assert bot.resolved == [] and bot.tracker.watches() == []


async def test_track_add_is_refused_in_a_channel_the_member_cannot_see(bot, server) -> None:
    bot.fake_channel.hidden.add(MEMBER)
    status, document = await _run(server, "track.add", _add_args())
    assert status == "rejected" and "看不到頻道" in document["reason"]
    assert bot.tracker.watches() == [] and bot.resolved == []


async def test_track_add_keeps_the_members_limit_and_source_checks(bot, server) -> None:
    for number in range(3):
        _status, document = await _run(server, "track.add", _add_args(source=f"{SOURCE}{number}"))
        assert document["ok"] is True
    status, document = await _run(server, "track.add", _add_args(source=SOURCE + "x"))
    assert status == "done" and document["ok"] is False
    assert document["error"] == "每人最多 3 個追蹤。"
    _status, unknown = await _run(server, "track.add", _add_args(user=FRIEND, source="not a url"))
    assert unknown["ok"] is False and "追蹤需要" in unknown["error"]
    assert len(bot.tracker.watches()) == 3 and len(bot.resolved) == 3


async def test_track_list_filters_by_member_and_guild(bot, server) -> None:
    await _run(server, "track.add", _add_args())
    await _run(server, "track.add", _add_args(user=FRIEND, source=SOURCE + "2", at="09:30"))
    elsewhere = bot.tracker.add_source("youtube", "UC9", "@elsewhere")
    bot.tracker.add_watch(elsewhere.id, OTHER_GUILD, 999, MEMBER)

    _status, mine = await _run(server, "track.list", {"guild": GUILD, "user": MEMBER})
    assert [row["guild"] for row in mine["result"]["watches"]] == [GUILD]  # never another guild's
    assert f"#1 youtube · {SOURCE}" in mine["result"]["text"]
    _status, here = await _run(server, "track.list", {"guild": GUILD})
    assert [row["user"] for row in here["result"]["watches"]] == [MEMBER, FRIEND]
    assert "每天 09:30 判斷" in here["result"]["text"]
    _status, both = await _run(server, "track.list", {"guild": GUILD, "user": FRIEND})
    assert [row["id"] for row in both["result"]["watches"]] == [2]
    status, everything = await _run(server, "track.list", {})
    assert status == "rejected"  # a guild is always named
    # the same member's watch in another guild cannot be reached from this one
    [there] = [w for w in bot.tracker.watches(user_id=MEMBER) if w.guild_id == OTHER_GUILD]
    _status, far = await _run(
        server, "track.cancel", {"guild": GUILD, "user": MEMBER, "id": there.id}
    )
    assert far["ok"] is False and len(bot.tracker.watches(user_id=MEMBER)) == 2
    # --user only names whose watches to show; it need not be a member (they may have left)
    _status, nobody = await _run(server, "track.list", {"guild": GUILD, "user": STRANGER})
    assert nobody["result"]["watches"] == []
    status, refused = await _run(server, "track.list", {"guild": OTHER_GUILD, "user": MEMBER})
    assert status == "rejected" and "不在允許清單" in refused["reason"]


async def test_a_departed_members_watches_can_still_be_managed(bot, server) -> None:
    await _run(server, "track.add", _add_args())
    [watch] = bot.tracker.watches()
    bot.tracker.add_watch(watch.source_id, OTHER_GUILD, 999, MEMBER)
    bot.fake_guild.members.discard(MEMBER)  # left the guild; the watch still notifies
    owner = {"guild": GUILD, "user": MEMBER, "id": watch.id}

    _status, listed = await _run(server, "track.list", {"guild": GUILD, "user": MEMBER})
    assert [row["id"] for row in listed["result"]["watches"]] == [watch.id]
    for command, extra in (
        ("track.every", {"minutes": 30}),
        ("track.at", {"times": "08:00"}),
        ("track.log", {}),
    ):
        status, document = await _run(server, command, {**owner, **extra})
        assert status == "done" and document["ok"] is True, (command, document)
    assert bot.tracker.watches(user_id=MEMBER)[0].times == ("08:00",)
    # still only their own watch in the guild named, and that guild must be allowlisted
    status, theirs = await _run(server, "track.cancel", {**owner, "user": FRIEND})
    assert status == "done" and theirs["ok"] is False
    status, far = await _run(server, "track.cancel", {**owner, "guild": OTHER_GUILD})
    assert status == "rejected" and "不在允許清單" in far["reason"]
    status, cancelled = await _run(server, "track.cancel", owner)
    assert cancelled["ok"] is True
    assert [w.guild_id for w in bot.tracker.watches(user_id=MEMBER)] == [OTHER_GUILD]
    # a new watch needs them to be there now
    status, added = await _run(server, "track.add", _add_args(source=SOURCE + "2"))
    assert status == "rejected" and "不是伺服器" in added["reason"]


async def test_changes_only_reach_the_members_own_watch(bot, server, caplog) -> None:
    caplog.set_level(logging.INFO, logger="discord_codex_bot.admin_tracking")
    await _run(server, "track.add", _add_args())
    [watch] = bot.tracker.watches()

    for command, extra in (
        ("track.every", {"minutes": 5}),
        ("track.at", {"times": "10:00"}),
        ("track.cancel", {}),
    ):
        status, document = await _run(
            server, command, {"guild": GUILD, "user": FRIEND, "id": watch.id, **extra}
        )
        assert status == "done" and document["ok"] is False
        assert document["error"] == f"使用者 {FRIEND} 沒有追蹤 #{watch.id}。"
    assert bot.tracker.watches() == [watch]

    _status, timed = await _run(
        server, "track.at", {"guild": GUILD, "user": MEMBER, "id": watch.id, "times": "0930"}
    )
    assert timed["result"]["times"] == ["09:30"]
    assert bot.tracker.watches()[0].times == ("09:30",)
    status, unreadable = await _run(
        server, "track.at", {"guild": GUILD, "user": MEMBER, "id": watch.id, "times": "中午"}
    )
    assert status == "rejected" and "--times 看不懂" in unreadable["reason"]
    _status, every = await _run(
        server, "track.every", {"guild": GUILD, "user": MEMBER, "id": watch.id, "minutes": 120}
    )
    assert every["ok"] is True and "每 120 分鐘" in every["result"]["text"]
    assert (bot.tracker.watches()[0].interval_minutes, bot.tracker.watches()[0].times) == (120, ())

    _status, cancelled = await _run(
        server, "track.cancel", {"guild": GUILD, "user": MEMBER, "id": watch.id}
    )
    assert cancelled["result"]["text"] == f"已取消追蹤 #{watch.id}。"
    assert bot.tracker.watches() == []
    assert f"Tracking cancel (admin) user={MEMBER} watch={watch.id} -> deleted" in caplog.text


async def test_track_log_shows_the_members_judgements(bot, server) -> None:
    await _run(server, "track.add", _add_args())
    await _run(server, "track.add", _add_args(source=SOURCE + "2"))
    await _run(server, "track.add", _add_args(user=FRIEND, source=SOURCE + "3"))
    _judge(bot.tracker, 1, "a", notify=True, reason="宣布新曲")
    _judge(bot.tracker, 2, "b", notify=False, reason="一般直播")

    _status, one = await _run(server, "track.log", {"guild": GUILD, "user": MEMBER, "id": 1})
    text = one["result"]["text"]
    assert (
        "🔔 提醒了" in text and "宣布新曲" in text and "標題 a" in text and "一般直播" not in text
    )
    _status, two = await _run(server, "track.log", {"guild": GUILD, "user": MEMBER, "id": 2})
    assert [row["reason"] for row in two["result"]["decisions"]] == ["一般直播"]
    _status, empty = await _run(server, "track.log", {"guild": GUILD, "user": FRIEND, "id": 3})
    assert "還沒有判斷紀錄" in empty["result"]["text"]
    _status, theirs = await _run(server, "track.log", {"guild": GUILD, "user": FRIEND, "id": 1})
    assert theirs["ok"] is False and theirs["error"] == f"使用者 {FRIEND} 沒有追蹤 #1。"


async def test_tracking_outbox_lists_what_has_not_gone_out(bot, server) -> None:
    await _run(server, "track.add", _add_args())
    await _run(server, "track.add", _add_args(source=SOURCE + "2"))
    _status, empty = await _run(server, "tracking.outbox", {})
    assert empty["result"]["pending"] == 0 and empty["result"]["messages"] == []
    _judge(bot.tracker, 1, "a", notify=True)
    _judge(bot.tracker, 1, "b", notify=True)
    _judge(bot.tracker, 2, "c", notify=True)
    _judge(bot.tracker, 2, "d", notify=False)  # judged, not worth a notification: no outbox
    first, second, third = bot.tracker.pending_outbox()
    bot.tracker.mark_delivered(first.id)
    bot.tracker.mark_delivery_failed(second.id, "403 Forbidden")
    bot.tracker.mark_delivery_failed(second.id, "403 Forbidden")

    _status, document = await _run(server, "tracking.outbox", {})
    result = document["result"]
    assert (result["pending"], result["failed"]) == (2, 1)
    assert [(row["id"], row["attempts"]) for row in result["messages"]] == [
        (second.id, 2),
        (third.id, 0),
    ]
    assert f"#{second.id} 送出失敗 2 次 · 追蹤 #1" in result["text"] and "標題 b" in result["text"]
    assert f"#{third.id} 待送 · 追蹤 #2" in result["text"] and "標題 a" not in result["text"]
    _status, failed = await _run(server, "tracking.outbox", {"failed": True})
    assert [row["id"] for row in failed["result"]["messages"]] == [second.id]
    _status, capped = await _run(server, "tracking.outbox", {"limit": 1})
    assert (
        len(capped["result"]["messages"]) == 1
        and "只列出前 1 則，共 2 則" in capped["result"]["text"]
    )
    json.dumps(document)  # what done/ holds is plain JSON


async def test_tracking_outbox_lists_only_allowlisted_guilds_unless_asked(bot, server) -> None:
    await _run(server, "track.add", _add_args())
    [watch] = bot.tracker.watches()
    elsewhere = bot.tracker.add_source("youtube", "UC9", "@elsewhere")
    far = bot.tracker.add_watch(elsewhere.id, OTHER_GUILD, 999, STRANGER)
    _judge(bot.tracker, watch.id, "a", notify=True)
    _judge(bot.tracker, far.id, "z", notify=True)  # a guild the Bot no longer serves
    assert {m.watch.guild_id for m in bot.tracker.pending_outbox()} == {GUILD, OTHER_GUILD}

    def guilds(document) -> list[int]:
        return [row["guild"] for row in document["result"]["messages"]]

    _status, default = await _run(server, "tracking.outbox", {})
    assert guilds(default) == [GUILD] and default["result"]["pending"] == 1
    assert str(STRANGER) not in default["result"]["text"]
    _status, one = await _run(server, "tracking.outbox", {"guild": GUILD})
    assert guilds(one) == [GUILD]
    _status, everything = await _run(server, "tracking.outbox", {"all_guilds": True})
    assert sorted(guilds(everything)) == [GUILD, OTHER_GUILD]
    assert [
        row["watch"] for row in everything["result"]["messages"] if row["guild"] == OTHER_GUILD
    ] == [far.id]
    status, refused = await _run(server, "tracking.outbox", {"guild": OTHER_GUILD})
    assert status == "rejected" and "不在允許清單" in refused["reason"]
    status, both = await _run(server, "tracking.outbox", {"guild": GUILD, "all_guilds": True})
    assert status == "rejected" and "只能擇一" in both["reason"]


async def test_tracking_commands_say_so_when_tracking_is_off(bot, server) -> None:
    bot.tracker = None
    for command, args in (
        ("track.add", _add_args()),
        ("track.list", {"guild": GUILD}),
        ("track.cancel", {"guild": GUILD, "user": MEMBER, "id": 1}),
        ("track.every", {"guild": GUILD, "user": MEMBER, "id": 1, "minutes": 60}),
        ("track.at", {"guild": GUILD, "user": MEMBER, "id": 1, "times": "12:00"}),
        ("track.log", {"guild": GUILD, "user": MEMBER, "id": 1}),
        ("tracking.outbox", {}),
    ):
        status, document = await _run(server, command, args)
        assert status == "done" and document["ok"] is False, command
        assert "社群追蹤尚未啟用" in document["error"]
    assert bot.resolved == []
