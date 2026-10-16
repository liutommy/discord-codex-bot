import asyncio
import io
import stat
import types
import zipfile
from dataclasses import replace
from pathlib import Path

import discord
import pytest

from discord_codex_bot import admin, admin_members
from discord_codex_bot import bot as bot_module
from discord_codex_bot.admin_members import HANDLERS, NO_CHANNEL_THREAD, SCHEMAS
from discord_codex_bot.admin_server import AdminError, AdminServer
from discord_codex_bot.bot import DiscordCodexClient
from discord_codex_bot.openrouter import Model
from discord_codex_bot.reminders import PAST
from discord_codex_bot.threads import ThreadStore
from discord_codex_bot.usage import RateLimits

GUILD, OTHER_GUILD = 111111111111111111, 333333333333333333
CHANNEL = 222222222222222222  # the conftest allowlisted channel
CLOSED = 777  # in the guild, not allowlisted
MEMBER, FRIEND, STRANGER = 444444444444444444, 666666666666666666, 555555555555555555


def _not_found():
    return discord.NotFound(types.SimpleNamespace(status=404, reason="x"), "x")


class FakeGuild:
    def __init__(self, guild_id: int, members: set[int]) -> None:
        self.id = guild_id
        self.members = members

    def get_member(self, user_id: int):
        return None  # an empty member cache: the API decides

    async def fetch_member(self, user_id: int):
        if user_id not in self.members:
            raise _not_found()
        return types.SimpleNamespace(id=user_id, guild=self, display_name=f"m{user_id}")


@pytest.fixture
def bot(config, tmp_path, monkeypatch) -> DiscordCodexClient:
    client = DiscordCodexClient(
        replace(config, codex_home=tmp_path / "codex", permanent_memory_dir=tmp_path / "perm")
    )
    guild = FakeGuild(GUILD, {MEMBER, FRIEND})
    # No send(): a command that tried to post would fail the test.
    channels = {
        CHANNEL: types.SimpleNamespace(id=CHANNEL, guild=guild, parent_id=None),
        CLOSED: types.SimpleNamespace(id=CLOSED, guild=guild, parent_id=None),
    }

    async def fetch_channel(channel_id):
        if channel_id not in channels:
            raise _not_found()
        return channels[channel_id]

    monkeypatch.setattr(client, "get_channel", lambda channel_id: None)
    monkeypatch.setattr(client, "fetch_channel", fetch_channel)
    monkeypatch.setattr(client, "get_guild", lambda guild_id: guild if guild_id == GUILD else None)
    return client


@pytest.fixture
def root(tmp_path) -> Path:
    return tmp_path / "admin"


@pytest.fixture
def server(bot, root) -> AdminServer:
    server = AdminServer(bot, root, HANDLERS, SCHEMAS)
    server.prepare()
    return server


async def _run(server: AdminServer, command: str, **args) -> tuple[str, dict]:
    """One request through the inbox, the way the CLI sends it: (status, document)."""
    request_id = admin.write_request(server.root, command, args)
    await server.pass_once()
    await server.drain()
    return admin.read_result(server.root, request_id)


async def _ok(server: AdminServer, command: str, **args) -> dict:
    status, document = await _run(server, command, **args)
    assert (status, document["ok"], document["error"]) == ("done", True, None)
    return document["result"]


async def _failed(server: AdminServer, command: str, **args) -> str:
    status, document = await _run(server, command, **args)
    assert (status, document["ok"]) == ("done", False)
    return document["error"]


async def _refused(server: AdminServer, command: str, **args) -> str:
    status, document = await _run(server, command, **args)
    assert status == "rejected"
    return document["reason"]


def test_every_member_command_has_a_handler_and_a_valid_schema() -> None:
    expected = {
        *("remind.add", "remind.list", "remind.cancel"),
        *("memory.add", "memory.forget", "memory.list"),
        *("model.get", "model.set", "model.clear"),
        *("style.get", "style.set", "style.clear", "style.persona"),
        *("reset", "stop", "export", "status"),
    }
    assert set(HANDLERS) == set(SCHEMAS) == expected
    for command, schema in SCHEMAS.items():
        admin.check_schema(command, schema)
    parsed = admin.build_parser(SCHEMAS).parse_args(
        ["remind", "add", "--guild", "1", "--channel", "2", "--user", "3"]
        + ["--when", "明天 9:30", "--text", "-", "--who", "4", "5"]
    )
    assert parsed.command == "remind.add" and parsed.arg_who == [4, 5]
    assert admin.build_parser(SCHEMAS).parse_args(["status", "--guild", "1", "--user", "2"])


async def test_a_reminder_is_set_listed_and_cancelled_as_the_member(bot, server) -> None:
    result = await _ok(
        server,
        "remind.add",
        guild=GUILD,
        channel=CHANNEL,
        user=MEMBER,
        when="30分鐘後",
        text="倒垃圾",
        who=[FRIEND, MEMBER],
    )
    (item,) = bot.reminders.for_user(MEMBER)
    assert result["reminder"] == item and result["text"].startswith("⏰ 已設定 #1")
    assert (item["guild_id"], item["channel_id"], item["text"]) == (GUILD, CHANNEL, "倒垃圾")
    assert item["target_ids"] == [FRIEND, MEMBER]

    listed = await _ok(server, "remind.list", user=MEMBER, guild=GUILD)
    assert listed["reminders"] == [item]
    assert "#1" in listed["text"] and "倒垃圾" in listed["text"] and str(FRIEND) in listed["text"]
    assert (await _ok(server, "remind.list", user=FRIEND, guild=GUILD))[
        "text"
    ] == "這位成員沒有提醒。"

    # The store's own scoping: a reminder is cancelled only as the member who set it.
    assert "找不到這位成員在這個伺服器的提醒 #1" in await _failed(
        server, "remind.cancel", user=FRIEND, guild=GUILD, id=1
    )
    assert bot.reminders.for_user(MEMBER) == [item]
    # a reminder the same person set in another guild is not reachable from this one
    with pytest.raises(AdminError, match="在這個伺服器"):
        await admin_members.remind_cancel(bot, {"guild": GUILD + 1, "user": MEMBER, "id": 1})
    assert (await _ok(server, "remind.cancel", user=MEMBER, guild=GUILD, id=1))["id"] == 1
    assert bot.reminders.for_user(MEMBER) == []


async def test_a_reminder_that_cannot_be_set_stores_nothing(bot, server) -> None:
    base = {"guild": GUILD, "channel": CHANNEL, "user": MEMBER, "text": "開會"}
    assert "看不懂 --when" in await _refused(server, "remind.add", **base, when="找個時間")
    assert "不是伺服器" in await _refused(
        server, "remind.add", **base, when="1小時後", who=[STRANGER]
    )
    assert "不是伺服器" in await _refused(
        server, "remind.add", **{**base, "user": STRANGER}, when="1小時後"
    )
    assert "此頻道目前未開放" in await _refused(
        server, "remind.add", **{**base, "channel": CLOSED}, when="1小時後"
    )
    assert await _failed(server, "remind.add", **base, when="2020-01-01 08:00") == PAST
    assert bot.reminders.for_user(MEMBER) == []


async def test_memory_commands_act_in_the_members_own_scope(bot, server) -> None:
    def names(scope: str, user: int | None) -> list[str]:
        return [entry.name for entry in bot.memory.all_entries(scope, GUILD, user)]

    assert "--user" in await _refused(
        server, "memory.add", scope="user", guild=GUILD, name="生日", text="3/14"
    )
    added = await _ok(
        server, "memory.add", scope="user", guild=GUILD, user=MEMBER, name="生日", text="3/14"
    )
    assert added["text"].startswith("已記住：- [生日]")
    await _ok(server, "memory.add", scope="guild", guild=GUILD, name="規則", text="不洗版")
    assert names("user", MEMBER) == ["生日"] and names("user", FRIEND) == []
    assert names("guild", None) == ["規則"]

    both = await _ok(server, "memory.list", guild=GUILD, user=MEMBER)
    assert "生日" in both["text"] and "規則" in both["text"]
    assert [e["name"] for e in both["entries"]["user"]] == ["生日"]
    server_only = await _ok(server, "memory.list", guild=GUILD)
    assert set(server_only["entries"]) == {"guild"} and "生日" not in server_only["text"]
    assert "--user" in await _refused(server, "memory.list", guild=GUILD, scope="user")

    # Two notes with one name: nothing is deleted until the operator names the file.
    await _ok(
        server, "memory.add", scope="user", guild=GUILD, user=MEMBER, name="生日", text="另一個"
    )
    forget = {"scope": "user", "guild": GUILD, "user": MEMBER}
    assert "多條同名" in await _failed(server, "memory.forget", **forget, name="生日")
    assert names("user", MEMBER) == ["生日", "生日"]
    first = bot.memory.all_entries("user", GUILD, MEMBER)[0].file
    assert (await _ok(server, "memory.forget", **forget, name=first))["file"] == first
    assert [e.file for e in bot.memory.all_entries("user", GUILD, MEMBER)] != [first]
    assert names("user", MEMBER) == ["生日"]
    # Another member's notes are not reachable through this member.
    theirs = {**forget, "user": FRIEND}
    assert "找不到" in await _failed(server, "memory.forget", **theirs, name="生日")
    assert names("user", MEMBER) == ["生日"]
    await _ok(server, "memory.forget", scope="guild", guild=GUILD, name="規則")
    assert names("guild", None) == []


async def test_model_set_checks_values_like_the_slash_command(bot, server, monkeypatch) -> None:
    member = {"guild": GUILD, "user": MEMBER}
    shown = await _ok(server, "model.get", **member)
    assert shown["text"].startswith("目前（預設）：") and shown["stored"] is None

    assert (await _ok(server, "model.set", **member, provider="codex"))["stored"] == (
        "codex:gpt-5.6-luna"
    )
    await _ok(server, "model.set", **member, effort="high")
    assert bot.memory.get_model(GUILD, MEMBER) == "codex:gpt-5.6-luna|high"
    # Another provider without an effort starts at its default effort.
    await _ok(server, "model.set", **member, value="agy:gemini-3.8-flash")
    assert bot.memory.get_model(GUILD, MEMBER) == "agy:gemini-3.8-flash"

    assert "沒有這個模型" in await _refused(server, "model.set", **member, value="agy:nope")
    assert "至少一個" in await _refused(server, "model.set", **member)
    assert "免費模型會變動" in await _refused(server, "model.set", **member, provider="openrouter")
    assert bot.memory.get_model(GUILD, MEMBER) == "agy:gemini-3.8-flash"

    # A router model is checked against that router's live list, loaded on the spot even
    # though the member is on Antigravity now.
    loads = []

    async def free_models():
        loads.append(1)
        bot.openrouter.models = [Model("t/text:free", "Text", False, True, 1)]
        return bot.openrouter.models

    monkeypatch.setattr(bot.openrouter, "free_models", free_models)
    await _ok(server, "model.set", **member, value="openrouter:t/text:free", effort="low")
    assert loads and bot.memory.get_model(GUILD, MEMBER) == "openrouter:t/text:free|low"
    assert (await _ok(server, "model.get", **member))["text"].startswith("目前：OpenRouter")

    assert (await _ok(server, "model.clear", **member))["cleared"] is True
    assert bot.memory.get_model(GUILD, MEMBER) == ""
    assert (await _ok(server, "model.clear", **member))["cleared"] is False
    assert "不是伺服器" in await _refused(server, "model.get", guild=GUILD, user=STRANGER)


async def test_style_and_persona_are_set_separately(bot, server) -> None:
    member = {"guild": GUILD, "user": MEMBER}
    result = await _ok(server, "style.set", **member, text="條列、少於 50 字")
    assert result["text"].startswith("已更新。") and "風格：條列、少於 50 字" in result["text"]
    assert bot.memory.get_style(GUILD, MEMBER) == "條列、少於 50 字"
    assert (await _ok(server, "style.persona", **member, mode="off"))["persona_off"] is True
    assert bot.memory.get_persona_off(GUILD, MEMBER)
    shown = await _ok(server, "style.get", **member)
    assert shown["text"].startswith("目前設定：") and "人設：關閉" in shown["text"]
    cleared = await _ok(server, "style.clear", **member)
    assert cleared["style"] == "" and cleared["persona_off"] is True
    await _ok(server, "style.persona", **member, mode="keep")
    assert not bot.memory.get_persona_off(GUILD, MEMBER)
    assert bot.memory.get_style(GUILD, FRIEND) == ""


async def test_reset_forgets_only_that_members_thread_in_that_channel(bot, server) -> None:
    mine = ThreadStore.key(GUILD, CHANNEL, MEMBER)
    elsewhere = ThreadStore.key(GUILD, 999, MEMBER)
    theirs = ThreadStore.key(GUILD, CHANNEL, FRIEND)
    for number, key in enumerate((mine, elsewhere, theirs)):
        bot.threads.remember(key, f"t{number}", model="codex:gpt-5.6-luna")
    args = {"guild": GUILD, "channel": CHANNEL, "user": MEMBER}
    assert (await _ok(server, "reset", **args))["reset"] is True
    assert bot.threads.live_entry(mine) is None
    assert bot.threads.live_entry(elsewhere) and bot.threads.live_entry(theirs)
    assert (await _ok(server, "reset", **args))["reset"] is False


async def test_stop_cancels_only_the_members_requests(bot, server) -> None:
    keys = {
        "here": ThreadStore.key(GUILD, CHANNEL, MEMBER),
        "there": ThreadStore.key(GUILD, 999, MEMBER),
        "friend": ThreadStore.key(GUILD, CHANNEL, FRIEND),
        "other guild": ThreadStore.key(OTHER_GUILD, CHANNEL, MEMBER),
    }
    tasks = {name: asyncio.create_task(asyncio.sleep(60)) for name in keys}
    bot.active.update({keys[name]: task for name, task in tasks.items()})
    try:
        args = {"guild": GUILD, "user": MEMBER}
        assert (await _ok(server, "stop", **args, channel=CHANNEL))["stopped"] == 1
        await asyncio.sleep(0)
        assert tasks["here"].cancelled() and not tasks["there"].done()
        assert (await _ok(server, "stop", **args))["stopped"] == 1  # every channel of the guild
        await asyncio.sleep(0)
        assert tasks["there"].cancelled()
        assert not tasks["friend"].done() and not tasks["other guild"].done()
        assert (await _ok(server, "stop", **args))["text"] == "這位成員沒有進行中的請求。"
    finally:
        for task in tasks.values():
            task.cancel()
        await asyncio.gather(*tasks.values(), return_exceptions=True)


def _exported(path: Path) -> tuple[int, int, dict[str, str]]:
    """(mode, size, {name: text}) of an exported zip."""
    info = path.stat()
    with zipfile.ZipFile(io.BytesIO(path.read_bytes())) as archive:
        files = {name: archive.read(name).decode() for name in archive.namelist()}
    return stat.S_IMODE(info.st_mode), info.st_size, files


async def test_export_writes_a_private_zip_and_returns_its_path(bot, server, tmp_path) -> None:
    member = {"guild": GUILD, "user": MEMBER}
    assert "還沒有個人記憶" in await _failed(server, "export", **member)
    bot.memory.add("user", GUILD, MEMBER, "生日", "3/14")
    bot.memory.add("user", GUILD, FRIEND, "秘密", "不是他的")

    result = await _ok(server, "export", **member)
    path = Path(result["path"])
    assert path.parent == tmp_path / "codex" / "exports"  # no backup volume mounted
    assert result["text"].endswith(str(path))
    mode, size, files = _exported(path)
    assert mode == 0o600 and size == result["bytes"]
    assert f"memory-{GUILD}-{MEMBER}/MEMORY.md" in files
    assert all(name.startswith(f"memory-{GUILD}-{MEMBER}/") for name in files)
    assert not any("秘密" in text for text in files.values())  # only this member's memory

    again = Path((await _ok(server, "export", **member))["path"])
    assert again != path and _exported(path)[2] == files  # never over an earlier export

    backups = tmp_path / "backups"
    backups.mkdir()
    bot.config = replace(bot.config, backup_dir=backups)
    assert Path((await _ok(server, "export", **member))["path"]).parent == backups / "exports"


async def test_status_is_the_members_status_text(bot, server, monkeypatch) -> None:
    async def login(config):
        return "ChatGPT 訂閱登入有效"

    async def usage(config):
        return RateLimits(12.0, 3.0, "test")

    async def no_models():
        return []

    monkeypatch.setattr(bot_module, "codex_login_status", login)
    monkeypatch.setattr(bot_module, "probe_rate_limits", usage)
    monkeypatch.setattr(bot.openrouter, "free_models", no_models)
    monkeypatch.setattr(bot.orcarouter, "free_models", no_models)
    bot.memory.set_style(GUILD, MEMBER, "條列")
    bot.threads.remember(ThreadStore.key(GUILD, CHANNEL, MEMBER), "t1", model="codex:gpt-5.6-luna")

    text = (await _ok(server, "status", guild=GUILD, user=MEMBER))["text"]
    assert "【你的設定】" in text and "風格：條列" in text and "額度 5h 12%" in text
    assert NO_CHANNEL_THREAD in text.split("\n") and "續接：無" not in text
    in_channel = (await _ok(server, "status", guild=GUILD, channel=CHANNEL, user=MEMBER))["text"]
    assert "續接：會接續" in in_channel and NO_CHANNEL_THREAD not in in_channel


def test_the_member_handlers_are_what_the_bot_loads(config, tmp_path) -> None:
    client = DiscordCodexClient(replace(config, codex_home=tmp_path / "codex"))
    for command, handler in admin_members.HANDLERS.items():
        assert client.admin.handlers[command] is handler
        assert client.admin.schemas[command] == admin_members.SCHEMAS[command]
