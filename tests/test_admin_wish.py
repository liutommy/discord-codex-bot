import io
import types
from dataclasses import asdict, replace

import pytest

from discord_codex_bot import admin, admin_wish
from discord_codex_bot.admin_server import AdminServer
from discord_codex_bot.bot import DiscordCodexClient
from discord_codex_bot.wishes import DEVELOPER_STATUSES, STATUS_LABELS

GUILD, CHANNEL, USER = 111111111111111111, 222222222222222222, 444444444444444444


@pytest.fixture
def bot(config, tmp_path, monkeypatch):
    client = DiscordCodexClient(replace(config, codex_home=tmp_path / "codex"))
    channels = {
        CHANNEL: types.SimpleNamespace(id=CHANNEL, guild=types.SimpleNamespace(id=GUILD)),
        777: types.SimpleNamespace(id=777, guild=types.SimpleNamespace(id=GUILD)),
        888: types.SimpleNamespace(id=888, guild=types.SimpleNamespace(id=999)),
    }

    async def fetch(channel_id):
        return channels[channel_id]

    monkeypatch.setattr(client, "get_channel", lambda _: None)
    monkeypatch.setattr(client, "fetch_channel", fetch)
    return client


@pytest.fixture
def server(bot, tmp_path):
    server = AdminServer(bot, tmp_path / "admin", admin_wish.HANDLERS, admin_wish.SCHEMAS)
    server.prepare()
    return server


async def run(server, command, **args):
    request_id = admin.write_request(server.root, command, args)
    await server.pass_once()
    await server.drain()
    return admin.read_result(server.root, request_id)


def add(bot, **changes):
    args = dict(
        guild_id=GUILD,
        channel_id=CHANNEL,
        message_id=123,
        user_id=USER,
        title="願望",
        original="原話",
        need="需求",
        feasibility="可行",
        alternatives="方案",
        asked="確認",
    )
    return bot.wishes.add(**(args | changes))[0]


def test_all_commands_have_valid_schemas_and_cli_forms(monkeypatch):
    assert (
        set(admin_wish.HANDLERS)
        == set(admin_wish.SCHEMAS)
        == {
            "wish.list",
            "wish.show",
            "wish.status",
        }
    )
    assert admin_wish in admin.handler_modules()
    parser = admin.build_parser(admin.load_schemas())
    for command, schema in admin_wish.SCHEMAS.items():
        admin.check_schema(command, schema)
        for example in schema["examples"]:
            import shlex

            namespace = parser.parse_args(shlex.split(example))
            assert namespace.command == command
            admin._arguments(namespace, schema)
    monkeypatch.setattr("sys.stdin", io.StringIO("進度說明"))
    namespace = parser.parse_args(
        ["wish", "status", "--id", "7", "--status", "done", "--note", "-"]
    )
    assert admin._arguments(namespace, admin_wish.SCHEMAS["wish.status"])["note"] == "進度說明"
    assert admin_wish.SCHEMAS["wish.status"]["args"]["status"]["choices"] == DEVELOPER_STATUSES
    assert set(admin_wish.SCHEMAS["wish.list"]["args"]["status"]["choices"]) == {
        *STATUS_LABELS,
        "open_all",
    }


async def test_list_defaults_to_open_allowed_guilds_and_limit(bot, server):
    wish = add(bot)
    foreign = add(bot, guild_id=999)
    closed = add(bot, title="完成", original="另一需求", message_id=None)
    bot.wishes.set_status(closed.id, "done")
    status, document = await run(server, "wish.list")
    assert status == "done" and document["ok"]
    [row] = document["result"]["wishes"]
    assert row == asdict(wish) | {"link": f"https://discord.com/channels/{GUILD}/{CHANNEL}/123"}
    _, document = await run(server, "wish.list", guild=GUILD, status="done", limit=1)
    assert [row["id"] for row in document["result"]["wishes"]] == [closed.id]
    assert document["result"]["wishes"][0]["link"] == ""
    status, _ = await run(server, "wish.list", guild=foreign.guild_id)
    assert status == "rejected"


async def test_status_queues_without_posting_and_show_includes_notices(bot, server):
    wish = add(bot)
    status, document = await run(
        server, "wish.status", id=wish.id, status="accepted", note="已排入開發"
    )
    assert status == "done" and document["ok"]
    assert "通知已排入佇列" in document["result"]["text"]
    [notice] = bot.wishes.pending_notices()
    assert notice.status == "accepted" and notice.note == "已排入開發"
    bot.wishes.mark_failed(notice.id, "unavailable")
    _, document = await run(server, "wish.show", id=wish.id)
    assert document["ok"] and document["result"]["wish"]["original"] == "原話"
    [row] = document["result"]["notices"]
    assert row["state"] == "pending" and row["attempts"] == 1
    assert row["last_error"] == "unavailable" and "原始需求：原話" in document["result"]["text"]
    _, document = await run(server, "wish.status", id=wish.id, status="accepted", note="補充進度")
    assert document["ok"] and len(bot.wishes.pending_notices()) == 2


@pytest.mark.parametrize("command", ["wish.show", "wish.status"])
async def test_id_operations_reject_disallowed_guild_and_missing_wish(bot, server, command):
    foreign = add(bot, guild_id=999)
    args = {"status": "accepted"} if command == "wish.status" else {}
    status, _ = await run(server, command, id=foreign.id, **args)
    assert status == "rejected"
    assert bot.wishes.get(foreign.id).status == "open" and bot.wishes.pending_notices() == []
    status, document = await run(server, command, id=999, **args)
    assert status == "done" and not document["ok"]


async def test_invalid_status_note_and_limit_are_rejected(bot, server):
    wish = add(bot)
    for args in ({"status": "withdrawn"}, {"status": "open", "note": "x" * 1501}):
        status, _ = await run(server, "wish.status", id=wish.id, **args)
        assert status == "rejected"
    status, _ = await run(server, "wish.list", limit=0)
    assert status == "rejected" and bot.wishes.pending_notices() == []
