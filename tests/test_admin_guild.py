import asyncio
import io
import time
import types
from dataclasses import replace
from datetime import UTC, datetime, timedelta

import discord
import pytest

from discord_codex_bot import admin, admin_guild, instructions, summary
from discord_codex_bot import bot as bot_module
from discord_codex_bot.admin_server import AdminServer
from discord_codex_bot.backends import Resolved
from discord_codex_bot.bot import FAILURE_MESSAGE, DiscordCodexClient, Fallback
from discord_codex_bot.codex import CodexResult, CodexUsageLimit
from discord_codex_bot.routing import load_table
from discord_codex_bot.summary import DEFAULT_MESSAGES
from discord_codex_bot.threads import ThreadStore
from discord_codex_bot.usage import RateLimits

GUILD, OTHER_GUILD = 111111111111111111, 333333333333333333
CHANNEL = 222222222222222222  # the conftest allowlisted channel
HIDDEN = 777  # in the guild but not allowlisted
MEMBER, STRANGER = 444444444444444444, 555555555555555555
MODEL = "codex:gpt-5.6-luna"  # what a member with no /model choice runs on here


def _http(kind, status: int):
    return kind(types.SimpleNamespace(status=status, reason="x"), "x")


class FakeGuild:
    def __init__(self, guild_id: int, members: set[int]) -> None:
        self.id = guild_id
        self.members = members

    def get_member(self, user_id: int):
        return None

    async def fetch_member(self, user_id: int):
        if user_id not in self.members:
            raise _http(discord.NotFound, 404)
        return types.SimpleNamespace(id=user_id, guild=self)


def _message(text: str, minutes_ago: float = 0, author: str = "小明"):
    return types.SimpleNamespace(
        content=text,
        created_at=datetime.now(UTC) - timedelta(minutes=minutes_ago),
        author=types.SimpleNamespace(display_name=author, bot=False),
        attachments=[],
        embeds=[],
    )


class FakeChannel:
    """History newest first, as Discord gives it. It has no send: nothing may post here."""

    def __init__(self, channel_id: int, guild: FakeGuild) -> None:
        self.id = channel_id
        self.guild = guild
        self.parent_id = None
        self.name = "閒聊"
        self.messages: list = []  # oldest first
        self.reads: list[dict] = []
        self.broken = False
        self.hidden: set[int] = set()  # members this channel is hidden from

    def permissions_for(self, member):
        return types.SimpleNamespace(
            view_channel=member.id not in self.hidden, manage_threads=False
        )

    def history(self, limit: int, after=None, oldest_first=None):
        self.reads.append({"limit": limit, "after": after, "oldest_first": oldest_first})

        async def newest_first():
            if self.broken:
                raise _http(discord.Forbidden, 403)
            shown = [m for m in reversed(self.messages) if after is None or m.created_at > after]
            for message in shown[:limit]:
                yield message

        return newest_first()


class FakeBackend:
    """Stands in for run_codex: each call answers with the next reply (the last repeats) and a
    fresh thread id; an exception reply is raised. With `gate` set it waits for it first."""

    def __init__(self, *replies) -> None:
        self.replies = list(replies)
        self.calls: list[tuple[str, dict]] = []
        self.gate: asyncio.Event | None = None

    async def __call__(self, prompt, config, *args, **kw):
        self.calls.append((prompt, kw))
        if self.gate is not None:
            await self.gate.wait()
        reply = self.replies.pop(0) if len(self.replies) > 1 else self.replies[0]
        if isinstance(reply, Exception):
            raise reply
        return CodexResult(reply, thread_id=f"t{len(self.calls)}", resumed=bool(kw.get("resume")))


@pytest.fixture
def client(config, tmp_path, monkeypatch) -> DiscordCodexClient:
    """A Bot with writable instruction files (the container's layout) and one guild whose
    member is MEMBER, with the allowlisted CHANNEL and a HIDDEN one."""
    image = tmp_path / "image"
    (image / "persona").mkdir(parents=True)
    (image / "rules").mkdir()
    (image / "rules" / "AGENTS.md").write_text("RULES\n", "utf-8")
    (image / "persona" / "AGENTS.md").write_text("image persona\n", "utf-8")
    (image / "output-style.md").write_text("image style\n", "utf-8")
    cfg = replace(
        config,
        codex_home=tmp_path / "vol",
        codex_workspace=tmp_path / "vol" / "workspace",
        codex_workspace_plain=tmp_path / "vol" / "workspace-plain",
        codex_rules_path=image / "rules" / "AGENTS.md",
        persona_dir=image / "persona",
        output_style_path=image / "output-style.md",
        backup_dir=tmp_path / "backups",
        permanent_memory_dir=tmp_path / "perm",
        attachment_dir=tmp_path / "attachments",
        memory_recall_rounds=2,
    )
    instructions.compose_workspaces(cfg)
    bot = DiscordCodexClient(cfg)
    guild = FakeGuild(GUILD, {MEMBER})
    channels = {CHANNEL: FakeChannel(CHANNEL, guild), HIDDEN: FakeChannel(HIDDEN, guild)}

    async def fetch_channel(channel_id):
        if channel_id not in channels:
            raise _http(discord.NotFound, 404)
        return channels[channel_id]

    monkeypatch.setattr(bot, "get_channel", lambda channel_id: None)
    monkeypatch.setattr(bot, "fetch_channel", fetch_channel)
    monkeypatch.setattr(bot, "get_guild", lambda guild_id: guild if guild_id == GUILD else None)
    bot.fake_channel = channels[CHANNEL]
    return bot


@pytest.fixture
def codex(monkeypatch) -> FakeBackend:
    backend = FakeBackend("答案")
    monkeypatch.setattr(bot_module, "run_codex", backend)
    return backend


def _server(client, root, schemas=None) -> AdminServer:
    server = AdminServer(client, root, admin_guild.HANDLERS, schemas or admin_guild.SCHEMAS)
    server.prepare()
    return server


@pytest.fixture
def run(client, tmp_path):
    """Send one command through the inbox to the real handlers; returns (status, document)."""
    root = tmp_path / "admin"

    async def send(command: str, schemas=None, **args):
        server = _server(client, root, schemas)
        request_id = admin.write_request(root, command, args)
        await server.pass_once()
        await server.drain()
        return admin.read_result(root, request_id)

    return send


def ok(found) -> dict:
    status, document = found
    assert status == "done" and document["ok"], document
    return document["result"]


def failed(found) -> str:
    status, document = found
    assert status == "done" and document["ok"] is False, document
    return document["error"]


def refused(found) -> str:
    status, document = found
    assert status == "rejected", document
    return document["reason"]


# --------------------------------------------------------------------------- linkclean, embedfix


async def test_linkclean_and_embedfix_switch_the_guild_like_the_admin_commands(client, run) -> None:
    assert ok(await run("linkclean.get", guild=GUILD))["text"] == "連結洗參數：全部清洗"
    assert ok(await run("linkclean.set", guild=GUILD, mode="links"))["mode"] == "links"
    assert client.linkclean.mode(GUILD) == "links"
    assert ok(await run("embedfix.set", guild=GUILD, state="off"))["text"] == "預覽修正：關"
    assert client.linkclean.embedfix(GUILD) is False
    ok(await run("linkclean.set", guild=GUILD, mode="off"))
    shown = ok(await run("embedfix.get", guild=GUILD))
    assert shown == {"text": "預覽修正：關（連結洗參數為全關時不會投遞）", "on": False}

    assert "不在允許清單" in refused(await run("linkclean.set", guild=OTHER_GUILD, mode="off"))
    assert "必須是" in refused(await run("linkclean.set", guild=GUILD, mode="sometimes"))
    assert client.linkclean.mode(OTHER_GUILD) == "all" and client.linkclean.mode(GUILD) == "off"


# --------------------------------------------------------------------------- persona


async def test_persona_set_get_and_clear_retire_threads_and_keep_backups(client, run) -> None:
    key = ThreadStore.key(GUILD, CHANNEL, MEMBER)
    workspace = client.config.codex_workspace / "AGENTS.md"
    client.threads.remember(key, "t-old", None, plain=False, model=MODEL)

    status = ok(await run("persona.get"))
    assert "人設：image 預設" in status["text"] and status["persona"]["uploaded"] is False

    report = ok(await run("persona.set", kind="persona", text="  新人設\n"))
    assert "人設：上傳版" in report["text"] and "備份：" in report["text"]
    assert workspace.read_text("utf-8") == "RULES\n\n\n新人設\n"
    # Codex reads AGENTS.md once per thread: a live thread would keep the old persona.
    assert client.threads.current(key, plain=False, model=MODEL) == ""
    kept = sorted((client.config.backup_dir / "instructions").glob("persona-*.md"))
    assert [path.read_text("utf-8").strip() for path in kept] == ["image persona"]
    assert ok(await run("persona.get", kind="persona"))["text"] == "新人設"

    ok(await run("persona.set", kind="style", text="條列"))
    assert instructions.style_text(client.config) == "條列"
    assert ok(await run("persona.get", kind="style"))["uploaded"] is True

    client.threads.remember(key, "t-new", None, plain=False, model=MODEL)
    cleared = ok(await run("persona.clear", kind="persona"))
    assert "已還原成 image 預設" in cleared["text"] and cleared["removed"] is True
    assert workspace.read_text("utf-8") == "RULES\n\n\nimage persona\n"
    assert client.threads.current(key, plain=False, model=MODEL) == ""

    client.threads.remember(key, "t-kept", None, plain=False, model=MODEL)
    again = ok(await run("persona.clear", kind="persona"))
    assert "本來就是 image 預設" in again["text"] and again["removed"] is False
    assert client.threads.current(key, plain=False, model=MODEL) == "t-kept"  # nothing changed

    assert "不可空白" in refused(await run("persona.set", kind="persona", text="   "))
    # The modal's limit, counted on the stripped text as an uploaded file is.
    too_long = "字" * (instructions.MAX_CHARS + 1)
    reason = refused(await run("persona.set", kind="persona", text=too_long))
    assert (
        reason
        == f"人設：檔案 {instructions.MAX_CHARS + 1} 字，超過上限 {instructions.MAX_CHARS} 字。"
    )
    ok(await run("persona.set", kind="persona", text="字" * instructions.MAX_CHARS + "\n\n\n"))
    assert len(instructions.persona_text(client.config)) == instructions.MAX_CHARS
    ok(await run("persona.clear", kind="persona"))
    assert instructions.persona_text(client.config) == "image persona"


async def test_the_cli_reads_a_persona_from_stdin(client, tmp_path, monkeypatch, capsys) -> None:
    async def ready() -> None:
        return None

    root = tmp_path / "admin"
    server = AdminServer(client, root, admin_guild.HANDLERS, admin_guild.SCHEMAS, poll_seconds=0.02)
    server.prepare()
    monkeypatch.setattr(client, "wait_until_ready", ready)
    monkeypatch.setattr(admin, "load_schemas", lambda: dict(admin_guild.SCHEMAS))
    monkeypatch.setattr("sys.stdin", io.StringIO("從標準輸入來的人設\n"))
    loop = asyncio.create_task(server.run_forever())
    try:
        code = await asyncio.to_thread(
            admin.main, ["persona", "set", "--kind", "persona", "--text", "-"], root
        )
    finally:
        loop.cancel()
    assert code == 0
    assert capsys.readouterr().out.startswith("已更新 人設，所有對話串重新開始。")
    assert instructions.persona_text(client.config) == "從標準輸入來的人設"


# --------------------------------------------------------------------------- summary


async def test_summary_returns_the_text_and_posts_nothing(client, run, codex) -> None:
    codex.replies = ["- 週五開團"]
    channel = client.fake_channel
    channel.messages = [_message("週五開團嗎", 30), _message("好啊", 20, "阿華")]
    where = {"guild": GUILD, "channel": CHANNEL, "user": MEMBER}

    result = ok(await run("summary", **where))
    assert result["text"] == "頻道摘要（最近 2 則）\n- 週五開團"
    assert result["messages"] == 2 and result["model"] == "Codex · gpt-5.6-luna · High"
    prompt, kw = codex.calls[0]
    assert "「閒聊」" in prompt and prompt.index("週五開團嗎") < prompt.index("好啊")
    assert kw["resume"] == ""
    assert channel.reads == [{"limit": DEFAULT_MESSAGES, "after": None, "oldest_first": None}]
    # A summary is not the member's conversation, and nothing is left in flight.
    assert client.threads.live_entry(ThreadStore.key(GUILD, CHANNEL, MEMBER)) is None
    assert client.active == {}

    ok(await run("summary", **where, count=5, focus="誰答應了什麼"))
    assert channel.reads[-1]["limit"] == 5 and "誰答應了什麼" in codex.calls[-1][0]


async def test_summary_by_hours_keeps_the_latest_messages(client, run, codex, monkeypatch) -> None:
    monkeypatch.setattr(summary, "MAX_MESSAGES", 3)
    client.fake_channel.messages = [_message("太舊", 180)] + [
        _message(f"新{i}", 10 - i) for i in range(1, 5)
    ]
    result = ok(await run("summary", guild=GUILD, channel=CHANNEL, user=MEMBER, hours=2))
    assert result["text"].startswith("頻道摘要（最近 2 小時，訊息太多，只摘要最新 3 則）\n")
    prompt = codex.calls[0][0]
    assert "新1" not in prompt and "太舊" not in prompt
    assert prompt.index("新2") < prompt.index("新3") < prompt.index("新4")
    read = client.fake_channel.reads[0]
    assert read["limit"] == 4 and read["oldest_first"] is False and read["after"] is not None


async def test_summary_refusals_and_failures(client, run, codex) -> None:
    where = {"guild": GUILD, "channel": CHANNEL, "user": MEMBER}
    assert "只能擇一" in refused(await run("summary", **where, count=5, hours=1))
    assert "不是伺服器" in refused(await run("summary", **{**where, "user": STRANGER}))
    assert "此頻道目前未開放" in refused(await run("summary", **{**where, "channel": HIDDEN}))
    assert ok(await run("summary", **where))["text"] == "這段時間裡沒有可摘要的訊息。"
    assert codex.calls == []

    client.fake_channel.messages = [_message("嗨")]
    client.fake_channel.broken = True
    assert failed(await run("summary", **where)) == "讀不到這個頻道的訊息紀錄。"
    client.fake_channel.broken = False
    codex.replies = [RuntimeError("codex down")]
    assert failed(await run("summary", **where)) == FAILURE_MESSAGE.format(prefix="codex")


async def test_summary_is_refused_in_a_channel_the_member_cannot_see(client, run, codex) -> None:
    client.fake_channel.messages = [_message("只有幹部看得到的內容")]
    client.fake_channel.hidden.add(MEMBER)
    where = {"guild": GUILD, "channel": CHANNEL, "user": MEMBER}
    assert f"使用者 {MEMBER} 看不到頻道 {CHANNEL}" in refused(await run("summary", **where))
    assert client.fake_channel.reads == [] and codex.calls == []  # nothing read, nothing asked
    client.fake_channel.hidden.clear()
    assert ok(await run("summary", **where))["summary"] == "答案"


# --------------------------------------------------------------------------- ask


async def test_ask_answers_as_the_member_and_continues_their_conversation(
    client, run, codex
) -> None:
    codex.replies = ['好的。<memory scope="user" name="綠茶">喜歡綠茶</memory>', "你喜歡綠茶", "嗨"]
    where = {"guild": GUILD, "channel": CHANNEL, "user": MEMBER}
    key = ThreadStore.key(GUILD, CHANNEL, MEMBER)

    first = ok(await run("ask", **where, prompt="  記住我喜歡綠茶 "))
    assert first["text"] == "好的。\n\n（回答模型：Codex · gpt-5.6-luna · High）"
    assert first["resumed"] is False and first["auto"] is False
    # The tag counted as the member's own, as if they had asked in the channel.
    assert [entry.name for entry in client.memory.entries("user", GUILD, MEMBER)] == ["綠茶"]
    prompt, kw = codex.calls[0]
    assert prompt == "記住我喜歡綠茶" and kw["resume"] == "" and kw["effort"] == "high"

    second = ok(await run("ask", **where, prompt="我喜歡什麼？", effort="low"))
    assert codex.calls[1][1]["resume"] == "t1" and codex.calls[1][1]["effort"] == "low"
    assert second["resumed"] is True and "；接續先前的對話）" in second["text"]
    # The member's own next message in the channel goes on from the same thread.
    assert client.threads.current(key, plain=False, model=client._model(GUILD, MEMBER)) == "t2"

    ok(await run("ask", **where, prompt="重來", new=True))
    assert codex.calls[2][1]["resume"] == ""
    assert client.active == {} and client.fake_channel.reads == []


async def test_ask_refusals_and_failures(client, run, codex) -> None:
    where = {"guild": GUILD, "channel": CHANNEL, "user": MEMBER}
    client.config = replace(client.config, max_prompt_chars=10)
    assert "1 到 10 個字元" in refused(
        await run("ask", **where, prompt="這個問題超過十個字了吧真的")
    )
    assert "不是伺服器" in refused(await run("ask", **{**where, "user": STRANGER}, prompt="q"))
    assert "此頻道目前未開放" in refused(
        await run("ask", **{**where, "channel": HIDDEN}, prompt="q")
    )
    assert "不在允許清單" in refused(
        await run("ask", **{**where, "guild": OTHER_GUILD}, prompt="q")
    )
    assert codex.calls == []

    codex.replies = [RuntimeError("codex down")]
    assert failed(await run("ask", **where, prompt="q")) == FAILURE_MESSAGE.format(prefix="codex")


async def test_ask_is_refused_in_a_channel_the_member_cannot_see(client, run, codex) -> None:
    client.fake_channel.hidden.add(MEMBER)
    where = {"guild": GUILD, "channel": CHANNEL, "user": MEMBER}
    assert f"使用者 {MEMBER} 看不到頻道 {CHANNEL}" in refused(await run("ask", **where, prompt="q"))
    assert codex.calls == [] and client.active == {}
    client.fake_channel.hidden.clear()
    assert ok(await run("ask", **where, prompt="q"))["answer"] == "答案"


async def test_the_members_stop_cancels_an_ask(client, codex, tmp_path) -> None:
    root = tmp_path / "admin"
    server = _server(client, root)
    codex.gate = asyncio.Event()
    request_id = admin.write_request(
        root, "ask", {"guild": GUILD, "channel": CHANNEL, "user": MEMBER, "prompt": "q"}
    )
    await server.pass_once()
    key = ThreadStore.key(GUILD, CHANNEL, MEMBER)
    for _ in range(200):
        if key in client.active and codex.calls:
            break
        await asyncio.sleep(0.01)
    client.active[key].cancel()  # what /stop does
    await server.drain()
    status, document = admin.read_result(root, request_id)
    assert status == "done" and document["ok"] is False and "已取消" in document["error"]
    assert client.active == {}


async def test_a_slow_ask_times_out_as_a_timeout_not_as_a_stop(client, run, codex) -> None:
    codex.gate = asyncio.Event()  # never opens
    schemas = {**admin_guild.SCHEMAS, "ask": {**admin_guild.SCHEMAS["ask"], "timeout": 0.2}}
    error = failed(await run("ask", schemas, guild=GUILD, channel=CHANNEL, user=MEMBER, prompt="q"))
    assert error.startswith("超過 0.2 秒") and "已取消" not in error
    assert client.active == {}


# --------------------------------------------------------------------------- help, status, alerts


async def test_help_returns_the_member_guide_or_the_model_sheet(client, run) -> None:
    guide = ok(await run("help"))["text"]
    assert guide == client.help_guide() and guide.startswith("**/codex 指令說明**")
    assert ok(await run("help", sheet=True))["text"] == client.help_sheet()


def _agy_reading() -> dict:
    reset = (datetime.now(UTC) + timedelta(days=3)).isoformat()
    groups = [
        {"buckets": [{"id": "gemini-5h", "remaining_fraction": 0.8, "reset_time": reset}]},
        {"buckets": [{"id": "3p-weekly", "remaining_fraction": 0.5, "reset_time": reset}]},
    ]
    return {"command": {"data": {"groups": groups}}}


async def test_bot_status_shows_what_only_the_running_bot_knows(client, run, monkeypatch) -> None:
    async def login(config):
        return "ChatGPT 訂閱登入有效"

    async def limits(config):
        return RateLimits(12.0, 3.0, "test")

    async def broken():
        raise OSError("disk full")

    monkeypatch.setattr(admin_guild, "codex_login_status", login)
    monkeypatch.setattr(admin_guild, "probe_rate_limits", limits)
    client._last_fallback = Fallback(
        time.time() - 120,
        CodexUsageLimit("spent", "usage_limit_exceeded"),
        "codex",
        Resolved("agy", "gemini-3.8-flash-medium", "medium"),
    )
    client.agy_quota.update(_agy_reading())
    client.agy_quota.mark_spent("3p")
    client._backup_loop = asyncio.create_task(broken())
    client._reminder_loop = asyncio.create_task(asyncio.sleep(60))
    await asyncio.sleep(0)
    try:
        result = ok(await run("bot.status"))
        client.routing = load_table(client.config.routing_path)
        routed = ok(await run("bot.status"))["text"]
    finally:
        client._reminder_loop.cancel()
    text = result["text"]
    assert "Codex：ChatGPT 訂閱登入有效 · 額度 5h 12% / 7d 3%" in text
    assert "Codex 額度用完，改用 Antigravity（gemini-3.8-flash-medium）回答" in text
    assert "agy 額度（剩餘）：3p-weekly 50% · gemini-5h 80%" in text
    assert "3p 用完，到 " in text and "gemini 可用" in text
    assert "Grok：未設定" in text and "自動挑選：關" in text
    assert "\n　⚠️ 備份：已停止（OSError）" in text and "\n　提醒：執行中" in text
    assert result["loops"]["_backup_loop"] == "已停止（OSError）"
    assert result["loops"]["_harvester"] == "沒有啟動"
    assert result["fallback"]["spare"] == "agy:gemini-3.8-flash-medium"
    assert result["codex"] == {"login": "ChatGPT 訂閱登入有效", "five_hour": 12.0, "seven_day": 3.0}
    assert "自動挑選：開" in routed and "\n　chat：L " in routed and "\n　圖片：" in routed


async def test_alerts_list_shows_streaks_and_open_alerts(client, run, monkeypatch) -> None:
    sent: list[str] = []

    async def dm(text: str) -> None:
        sent.append(text)

    monkeypatch.setattr(client, "get_user", lambda user_id: types.SimpleNamespace(send=dm))
    quiet = ok(await run("alerts.list"))["text"]
    assert "連續失敗：無" in quiet and "這次啟動後還沒送出告警。" in quiet

    client.alerts.owner_id = 999
    for _ in range(3):
        await client.alerts.record_failure("codex", "boom")
    await client.alerts.record_failure("grok", "boom")
    result = ok(await run("alerts.list"))
    text = result["text"]
    assert "告警私訊給：999" in text
    assert "連續失敗：codex 3 次 · grok 1 次" in text and "等恢復通知：codex" in text
    assert "　codex:fail：0 分鐘前（冷卻中，還有 30 分鐘）" in text
    assert result["streaks"] == {"codex": 3, "grok": 1} and len(sent) == 1

    await client.alerts.record_success("codex")
    text = ok(await run("alerts.list"))["text"]
    assert "連續失敗：grok 1 次" in text and "等恢復通知：無" in text
    assert text.index("codex:ok") < text.index("codex:fail")  # newest first
