import asyncio
import json
import logging
import os
import stat
import time
import types
from dataclasses import replace
from pathlib import Path

import discord
import pytest

from discord_codex_bot import admin
from discord_codex_bot.admin import AdminError, Rejected
from discord_codex_bot.admin_server import (
    DUPLICATE,
    INTERRUPTED,
    PARTLY,
    AdminServer,
    check_context,
    require_member,
    resolve_guild_channel,
)
from discord_codex_bot.bot import DiscordCodexClient

GUILD, OTHER_GUILD = 111111111111111111, 333333333333333333
CHANNEL = 222222222222222222  # the conftest allowlisted channel
MEMBER, STRANGER = 444444444444444444, 555555555555555555


class Calls:
    def __init__(self) -> None:
        self.seen: list[tuple[str, dict]] = []
        self.release = asyncio.Event()


def _schemas() -> dict:
    return {
        "test.echo": {
            "help": "echo",
            "args": {
                "n": {"type": "int", "required": True, "max": 100},
                "note": {"type": "str", "max_len": 10},
            },
        },
        "test.wait": {"help": "waits for the test to release it"},
        "test.slow": {"help": "never finishes in time", "timeout": 0.05},
        "test.fail": {"help": "fails"},
        "test.boom": {"help": "raises"},
        "test.refuse": {"help": "refuses"},
        "test.ctx": {
            "help": "context",
            "args": {
                "guild": {"type": "id", "required": True},
                "channel": {"type": "id"},
                "user": {"type": "id"},
                "who": {"type": "list[id]", "max_items": 3},
            },
        },
    }


def _handlers(calls: Calls) -> dict:
    async def echo(client, args):
        calls.seen.append(("test.echo", args))
        return {"text": f"echo {args['n']}", "n": args["n"]}

    async def wait(client, args):
        calls.seen.append(("test.wait", args))
        await calls.release.wait()
        return {"text": "released"}

    async def slow(client, args):
        calls.seen.append(("test.slow", args))
        await asyncio.sleep(10)

    async def fail(client, args):
        calls.seen.append(("test.fail", args))
        raise AdminError("找不到你的提醒 #3")

    async def boom(client, args):
        calls.seen.append(("test.boom", args))
        raise KeyError("secret member text")

    async def refuse(client, args):
        calls.seen.append(("test.refuse", args))
        raise Rejected("不是那個伺服器的成員")

    async def ctx(client, args):
        calls.seen.append(("test.ctx", args))
        return {"text": "ok"}

    return {
        "test.echo": echo,
        "test.wait": wait,
        "test.slow": slow,
        "test.fail": fail,
        "test.boom": boom,
        "test.refuse": refuse,
        "test.ctx": ctx,
    }


class FakeClient:
    def __init__(self) -> None:
        self.config = types.SimpleNamespace(allowed_guild_ids=frozenset({GUILD}))

    async def wait_until_ready(self) -> None:
        return None


@pytest.fixture
def root(tmp_path) -> Path:
    return tmp_path / "admin"


@pytest.fixture
def calls() -> Calls:
    return Calls()


@pytest.fixture
def server(root, calls) -> AdminServer:
    server = AdminServer(FakeClient(), root, _handlers(calls), _schemas(), poll_seconds=0.02)
    server.prepare()
    return server


def _files(directory: Path) -> list[str]:
    return sorted(path.name for path in directory.iterdir())


def _body(command: str = "test.echo", args: dict | None = None, **changes) -> dict:
    body = {
        "v": 1,
        "id": "a" * 32,
        "command": command,
        "args": {"n": 1} if args is None else args,
        "created": time.time(),
        "by": "box",
    }
    body.update(changes)
    return body


def _drop(root: Path, name: str, data: bytes | dict) -> Path:
    path = root / "inbox" / name
    path.write_bytes(json.dumps(data).encode() if isinstance(data, dict) else data)
    return path


async def _process(server: AdminServer) -> None:
    await server.pass_once()
    await server.drain()


def _rejections(root: Path) -> list[dict]:
    return [json.loads(path.read_text()) for path in sorted((root / "rejected").iterdir())]


async def test_a_request_round_trips_through_the_inbox(root, server, calls, caplog) -> None:
    caplog.set_level(logging.INFO, logger="discord_codex_bot.admin_server")
    request_id = admin.write_request(root, "test.echo", {"n": 3})
    path = root / "inbox" / f"{request_id}.json"
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert json.loads(path.read_text())["by"]

    await _process(server)

    assert calls.seen == [("test.echo", {"n": 3})]
    status, document = admin.read_result(root, request_id)
    assert status == "done"
    assert document["ok"] is True and document["error"] is None
    assert document["result"] == {"text": "echo 3", "n": 3}
    assert stat.S_IMODE((root / "done" / f"{request_id}.json").stat().st_mode) == 0o600
    assert _files(root / "inbox") == [] and _files(root / "processing") == []
    assert f"Admin test.echo id={request_id} ok=True" in caplog.text


async def test_the_cli_waits_for_the_running_loop(root, server, calls, monkeypatch, capsys) -> None:
    monkeypatch.setattr(admin, "load_schemas", _schemas)
    loop = asyncio.create_task(server.run_forever())
    try:
        code = await asyncio.to_thread(admin.main, ["test", "echo", "--n", "7"], root)
        failed = await asyncio.to_thread(admin.main, ["test", "fail", "--json"], root)
    finally:
        loop.cancel()
    assert code == 0
    assert failed == 1
    out = capsys.readouterr().out
    assert out.startswith("echo 7\n")
    document = json.loads(out[len("echo 7\n") :])
    assert document["status"] == "done" and document["ok"] is False
    assert document["error"] == "找不到你的提醒 #3"
    assert [command for command, _ in calls.seen] == ["test.echo", "test.fail"]


@pytest.mark.parametrize(
    ("name", "data", "reason"),
    [
        ("not-an-id.json", _body(id="not-an-id"), "id 必須是"),
        (f"{'b' * 32}.json", _body(), "檔名與 id 不符"),
        (f"{'a' * 32}.json", _body(command="test.nope"), "不認識的指令"),
        (f"{'a' * 32}.json", _body(command="bot.status"), "不認識的指令"),
        (f"{'a' * 32}.json", _body(args={}), "缺少 --n"),
        (f"{'a' * 32}.json", _body(args={"n": True}), "--n 必須是整數"),
        (f"{'a' * 32}.json", _body(args={"n": 101}), "--n 不可大於 100"),
        (f"{'a' * 32}.json", _body(args={"n": 1, "x": 1}), "不認識的參數"),
        (f"{'a' * 32}.json", _body(args={"n": 1, "note": "x" * 11}), "--note 最多 10"),
        (f"{'a' * 32}.json", _body(args={"n": 1, "note": "  "}), "--note 不可空白"),
        (f"{'a' * 32}.json", _body(v=2), "版本"),
        (f"{'a' * 32}.json", _body(created=time.time() - 2 * 86400), "超過一天"),
        (f"{'a' * 32}.json", _body(extra=1), "不認識的欄位"),
        (f"{'a' * 32}.json", b"{not json", "不是有效的 JSON"),
        (f"{'a' * 32}.json", b"[]", "必須是 JSON 物件"),
        (f"{'a' * 32}.json", b" " * (64 * 1024 + 1), "未讀取"),
    ],
)
async def test_a_bad_request_is_rejected_and_kept_never_run(
    root, server, calls, caplog, name, data, reason
) -> None:
    _drop(root, name, data)
    await _process(server)
    assert calls.seen == []
    [rejection] = _rejections(root)
    assert reason in rejection["reason"]
    assert _files(root / "inbox") == [] and _files(root / "processing") == []
    assert _files(root / "done") == []
    assert any(r.levelno == logging.WARNING and "rejected" in r.message for r in caplog.records)


async def test_a_symlinked_request_is_not_followed(root, server, calls, tmp_path) -> None:
    target = tmp_path / "elsewhere.json"
    target.write_text(json.dumps(_body()))
    (root / "inbox" / f"{'a' * 32}.json").symlink_to(target)
    await _process(server)
    assert calls.seen == []
    [rejection] = _rejections(root)
    assert rejection["request"] is None and "無法開啟" in rejection["reason"]


async def test_the_same_id_never_runs_twice(root, server, calls) -> None:
    request_id = admin.write_request(root, "test.echo", {"n": 1})
    copy = (root / "inbox" / f"{request_id}.json").read_bytes()
    await _process(server)
    first = (root / "done" / f"{request_id}.json").read_bytes()

    _drop(root, f"{request_id}.json", copy)
    await _process(server)

    assert len(calls.seen) == 1
    assert (root / "done" / f"{request_id}.json").read_bytes() == first
    [rejection] = _rejections(root)
    assert rejection["reason"] == DUPLICATE
    assert admin.read_result(root, request_id)[0] == "done"  # `admin result` shows the original


async def test_a_copy_of_a_running_request_waits_and_is_then_refused(root, server, calls) -> None:
    request_id = admin.write_request(root, "test.wait", {})
    copy = (root / "inbox" / f"{request_id}.json").read_bytes()
    await server.pass_once()
    await asyncio.sleep(0)
    _drop(root, f"{request_id}.json", copy)

    await server.pass_once()  # the copy must not replace the running one's claimed file
    assert _files(root / "inbox") == [f"{request_id}.json"]
    assert [n for n in _files(root / "processing") if n.endswith(".json")] == [f"{request_id}.json"]

    calls.release.set()
    await server.drain()
    await _process(server)
    assert [command for command, _ in calls.seen] == ["test.wait"]
    assert admin.read_result(root, request_id)[1]["ok"] is True
    assert [r["reason"] for r in _rejections(root)] == [DUPLICATE]


async def test_a_restart_moves_interrupted_requests_to_rejected(root, calls, caplog) -> None:
    first = AdminServer(FakeClient(), root, _handlers(calls), _schemas())
    first.prepare()
    request_id = admin.write_request(root, "test.wait", {})
    finished = admin.write_request(root, "test.echo", {"n": 2})
    os.replace(root / "inbox" / f"{request_id}.json", root / "processing" / f"{request_id}.json")
    os.replace(root / "inbox" / f"{finished}.json", root / "processing" / f"{finished}.json")
    (root / "done" / f"{finished}.json").write_text('{"ok": true}')  # only the clean-up was lost

    second = AdminServer(FakeClient(), root, _handlers(calls), _schemas())
    assert second.prepare() == 1
    await _process(second)

    assert calls.seen == []
    assert _files(root / "processing") == []
    status, document = admin.read_result(root, request_id)
    assert status == "rejected" and document["reason"] == INTERRUPTED
    assert document["request"]["id"] == request_id
    assert admin.read_result(root, finished) == ("done", {"ok": True})
    assert "interrupted before it ran" in caplog.text


async def test_a_restart_after_the_handler_began_says_it_may_be_partly_done(root, calls) -> None:
    first = AdminServer(FakeClient(), root, _handlers(calls), _schemas())
    first.prepare()
    request_id = admin.write_request(root, "test.wait", {})
    os.replace(root / "inbox" / f"{request_id}.json", root / "processing" / f"{request_id}.json")
    (root / "processing" / f"{request_id}.started").write_bytes(b"")
    second = AdminServer(FakeClient(), root, _handlers(calls), _schemas())
    assert second.prepare() == 1
    assert _files(root / "processing") == []
    status, document = admin.read_result(root, request_id)
    assert status == "rejected" and document["reason"] == PARTLY


async def test_directories_are_private_and_a_writable_one_is_refused(
    root, server, calls, capsys, caplog, monkeypatch
) -> None:
    for path in (root, *(root / name for name in admin.SUBDIRS)):
        assert stat.S_IMODE(path.stat().st_mode) == 0o700
    os.chmod(root / "inbox", 0o770)
    request_id = admin.write_request(root, "test.echo", {"n": 1})

    assert await server.pass_once() == 0  # nothing is read while others could write there
    assert _files(root / "inbox") == [f"{request_id}.json"]
    assert "群組或其他人可寫" in caplog.text
    monkeypatch.setattr(admin, "load_schemas", _schemas)
    assert admin.main(["test", "echo", "--n", "1"], root) == 1
    assert "拒絕執行" in capsys.readouterr().err
    assert _files(root / "inbox") == [f"{request_id}.json"]  # the CLI wrote nothing

    server.prepare()  # a start chmods it back
    assert stat.S_IMODE((root / "inbox").stat().st_mode) == 0o700
    await _process(server)
    assert calls.seen == [("test.echo", {"n": 1})]


def test_the_cli_refuses_a_missing_directory(root, monkeypatch, capsys) -> None:
    monkeypatch.setattr(admin, "load_schemas", _schemas)
    assert admin.main(["pending"], root) == 1
    assert "不存在" in capsys.readouterr().err


async def test_a_timeout_withdraws_a_request_the_bot_never_claimed(
    root, server, calls, monkeypatch, capsys
) -> None:
    monkeypatch.setattr(admin, "load_schemas", _schemas)
    code = await asyncio.to_thread(
        admin.main, ["test", "echo", "--n", "1", "--timeout", "0.3"], root
    )
    assert code == 2
    assert "已撤回" in capsys.readouterr().err
    assert _files(root / "inbox") == []
    await _process(server)
    assert calls.seen == []


def test_a_timeout_after_the_claim_reports_it_still_running(root, server) -> None:
    request_id = admin.write_request(root, "test.echo", {"n": 1})
    os.replace(root / "inbox" / f"{request_id}.json", root / "processing" / f"{request_id}.json")
    assert admin.wait_result(root, request_id, 0.05, poll=0.01) == ("running", None)
    assert [n for n in _files(root / "processing") if n.endswith(".json")] == [f"{request_id}.json"]


async def test_failures_timeouts_and_refusals_are_reported(root, server, calls, caplog) -> None:
    ids = {name: admin.write_request(root, name, {}) for name in ("test.slow", "test.boom")}
    ids["test.refuse"] = admin.write_request(root, "test.refuse", {})
    await _process(server)

    slow = admin.read_result(root, ids["test.slow"])[1]
    assert slow["ok"] is False and "超過 0.05 秒" in slow["error"]
    boom = admin.read_result(root, ids["test.boom"])[1]
    assert boom["ok"] is False and "KeyError" in boom["error"]
    assert "secret member text" not in boom["error"]
    assert "Admin test.boom id=" in caplog.text
    status, refused = admin.read_result(root, ids["test.refuse"])
    assert status == "rejected" and refused["reason"] == "不是那個伺服器的成員"
    assert refused["request"]["command"] == "test.refuse"


def test_prune_removes_only_old_results_and_scratch(root, server) -> None:
    now = time.time()
    old = now - 8 * 86400
    names = {
        "done": ["old.json", "new.json"],
        "rejected": ["old.json", "new.json"],
        "inbox": ["old.json.tmp", "old.json"],
    }
    for directory, files in names.items():
        for name in files:
            path = root / directory / name
            path.write_text("{}")
            if name.startswith("old"):
                os.utime(path, (old, old))
    assert server.prune(now) == 3
    assert _files(root / "done") == ["new.json"]
    assert _files(root / "rejected") == ["new.json"]
    assert _files(root / "inbox") == ["old.json"]  # a request is the loop's to judge, not prune's


def test_result_and_pending_commands(root, server, monkeypatch, capsys) -> None:
    monkeypatch.setattr(admin, "load_schemas", _schemas)
    queued = admin.write_request(root, "test.echo", {"n": 1})
    assert admin.main(["pending"], root) == 0
    assert f"{queued}  排隊中  test.echo" in capsys.readouterr().out
    assert admin.main(["result", queued], root) == 2

    finished = "c" * 32
    (root / "done" / f"{finished}.json").write_text(
        json.dumps({"id": finished, "ok": True, "result": {"text": "好了"}, "error": None})
    )
    assert admin.main(["result", finished], root) == 0
    assert capsys.readouterr().out == "好了\n"
    assert admin.main(["result", "d" * 32], root) == 1
    assert admin.main(["result", "../etc"], root) == 1


def test_control_characters_reach_the_terminal_escaped(root, server, monkeypatch, capsys) -> None:
    # A result can carry a post title or a model's answer; an escape sequence in it must not
    # clear the operator's screen or rewrite the line above.
    monkeypatch.setattr(admin, "load_schemas", _schemas)
    text = "標題\x1b[2J\r蓋掉\x9b31m\t欄\n第二行\x7f"
    finished, failed, refused = "c" * 32, "d" * 32, "e" * 32
    (root / "done" / f"{finished}.json").write_text(
        json.dumps({"id": finished, "ok": True, "result": {"text": text}, "error": None})
    )
    (root / "done" / f"{failed}.json").write_text(
        json.dumps({"id": failed, "ok": False, "result": None, "error": text})
    )
    (root / "rejected" / f"{refused}.json").write_text(json.dumps({"reason": text}))
    escaped = "標題\\x1b[2J\\x0d蓋掉\\x9b31m\t欄\n第二行\\x7f"

    assert admin.main(["result", finished], root) == 0
    assert capsys.readouterr().out == escaped + "\n"
    assert admin.main(["result", failed], root) == 1
    assert capsys.readouterr().err == f"失敗：{escaped}\n"
    assert admin.main(["result", refused], root) == 1
    assert capsys.readouterr().err == f"被拒絕，沒有執行：{escaped}\n"
    assert admin.main(["result", finished, "--json"], root) == 0
    out = capsys.readouterr().out
    assert not admin.CONTROL.search(out.replace("\n", ""))
    assert json.loads(out)["result"]["text"] == text  # the same JSON, only escaped

    _drop(root, f"{'f' * 32}.json", _body(command="\x1b]0;x\x07", id="f" * 32))
    assert admin.main(["pending"], root) == 0
    assert "\x1b" not in capsys.readouterr().out


def test_help_names_each_option_by_its_flag(capsys) -> None:
    schemas = admin.load_schemas()
    parser = admin.build_parser(schemas)
    for command in schemas:
        with pytest.raises(SystemExit):
            parser.parse_args([*command.split("."), "--help"])
        assert "ARG_" not in capsys.readouterr().out, command
    with pytest.raises(SystemExit):
        parser.parse_args(["remind", "add", "--help"])
    shown = capsys.readouterr().out
    assert "--guild GUILD" in shown and "--who WHO [WHO ...]" in shown


def test_cli_options_come_from_the_schema(root, server, monkeypatch, capsys) -> None:
    parser = admin.build_parser(_schemas())
    namespace = parser.parse_args(
        ["test", "ctx", "--guild", str(GUILD), "--user", "5", "--who", "6", "7", "--who", "8"]
    )
    assert namespace.command == "test.ctx"
    assert admin._arguments(namespace, _schemas()["test.ctx"]) == {
        "guild": GUILD,
        "user": 5,
        "who": [6, 7, 8],
    }
    monkeypatch.setattr(admin, "load_schemas", _schemas)
    with pytest.raises(SystemExit) as usage:
        admin.main(["test", "ctx", "--guild", "0"], root)
    assert usage.value.code == 1  # a usage error, not 2 ("not finished in time")
    assert admin.main(["test", "echo", "--n", "1", "--note", "x" * 11], root) == 1
    assert "最多 10 個字元" in capsys.readouterr().err
    assert _files(root / "inbox") == []


@pytest.mark.parametrize(
    "schema",
    [
        {"args": {}},  # no help
        {"help": "x", "args": {"text": {"type": "str"}}},  # no max_len
        {"help": "x", "args": {"n": {"type": "int", "maxlen": 3}}},  # a typo
        {"help": "x", "args": {"guild": {"type": "str", "max_len": 20}}},
        {"help": "x", "args": {"channel": {"type": "id"}}},  # no guild to check it against
        {"help": "x", "args": {"json": {"type": "bool"}}},
        {"help": "x", "timeout": 3600},
    ],
)
def test_a_malformed_schema_is_refused(schema) -> None:
    with pytest.raises(ValueError):
        admin.check_schema("test.bad", schema)


def test_the_handler_modules_define_matching_valid_commands(config, tmp_path) -> None:
    schemas = admin.load_schemas()  # every schema checked
    admin.build_parser(schemas)
    client = DiscordCodexClient(replace(config, codex_home=tmp_path / "codex"))
    assert client.admin.root == tmp_path / "codex" / "admin"
    assert set(client.admin.handlers) == set(schemas) == set(client.admin.schemas)


# --------------------------------------------------------------------------- context checks


def _http(kind, status: int):
    return kind(types.SimpleNamespace(status=status, reason="x"), "x")


class FakeGuild:
    def __init__(self, guild_id: int, members: set[int]) -> None:
        self.id = guild_id
        self.members = members
        self.fetched: list[int] = []

    def get_member(self, user_id: int):
        return None  # an empty member cache: the API decides

    async def fetch_member(self, user_id: int):
        self.fetched.append(user_id)
        if user_id not in self.members:
            raise _http(discord.NotFound, 404)
        return types.SimpleNamespace(id=user_id, guild=self)


@pytest.fixture
def bot(config, tmp_path, monkeypatch) -> DiscordCodexClient:
    client = DiscordCodexClient(replace(config, codex_home=tmp_path / "codex"))
    guild = FakeGuild(GUILD, {MEMBER})
    channels = {
        CHANNEL: types.SimpleNamespace(id=CHANNEL, guild=guild, parent_id=None),
        777: types.SimpleNamespace(id=777, guild=guild, parent_id=None),  # not allowlisted
        888: types.SimpleNamespace(id=888, guild=FakeGuild(OTHER_GUILD, set()), parent_id=None),
    }

    async def fetch_channel(channel_id):
        if channel_id not in channels:
            raise _http(discord.NotFound, 404)
        return channels[channel_id]

    monkeypatch.setattr(client, "get_channel", lambda channel_id: None)
    monkeypatch.setattr(client, "fetch_channel", fetch_channel)
    monkeypatch.setattr(client, "get_guild", lambda guild_id: guild if guild_id == GUILD else None)
    client.fake_guild = guild
    return client


async def test_context_checks_follow_the_member_rules(bot) -> None:
    channel = await resolve_guild_channel(bot, GUILD, CHANNEL)
    assert channel.id == CHANNEL
    assert (await require_member(bot, GUILD, MEMBER)).id == MEMBER
    await check_context(bot, {"guild": GUILD, "channel": CHANNEL, "user": MEMBER, "who": [MEMBER]})

    refusals = [
        ({"guild": OTHER_GUILD}, "不在允許清單"),
        ({"guild": GUILD, "channel": 888}, "不屬於伺服器"),
        ({"guild": GUILD, "channel": 999}, "找不到頻道"),
        ({"guild": GUILD, "channel": 777}, "此頻道目前未開放"),  # the client's own _access
        ({"guild": GUILD, "user": STRANGER}, "不是伺服器"),
        ({"guild": GUILD, "who": [MEMBER, STRANGER]}, "不是伺服器"),
        ({"channel": CHANNEL}, "需要同時給 --guild"),
    ]
    for args, reason in refusals:
        with pytest.raises(Rejected, match=reason):
            await check_context(bot, args)


async def test_require_member_reports_an_unclear_answer_as_a_refusal(bot, monkeypatch) -> None:
    async def broken(user_id):
        raise _http(discord.HTTPException, 503)

    monkeypatch.setattr(bot.fake_guild, "fetch_member", broken)
    with pytest.raises(Rejected, match="HTTP 503"):
        await require_member(bot, GUILD, MEMBER)


async def test_the_server_checks_context_before_the_handler_runs(root, bot, calls) -> None:
    server = AdminServer(bot, root, _handlers(calls), _schemas())
    server.prepare()
    refused = admin.write_request(root, "test.ctx", {"guild": GUILD, "user": STRANGER})
    allowed = admin.write_request(
        root, "test.ctx", {"guild": GUILD, "channel": CHANNEL, "user": MEMBER}
    )
    await _process(server)
    assert calls.seen == [("test.ctx", {"guild": GUILD, "channel": CHANNEL, "user": MEMBER})]
    status, document = admin.read_result(root, refused)
    assert status == "rejected" and "不是伺服器" in document["reason"]
    assert admin.read_result(root, allowed)[1]["ok"] is True


async def test_a_poison_request_is_rejected_and_never_stops_the_inbox(
    root, server, calls, monkeypatch
) -> None:
    # nesting too deep for the parser, a lone surrogate in a refused request, a 400-digit time
    _drop(root, "b" * 32 + ".json", b"[" * 60000)
    _drop(root, "c" * 32 + ".json", _body(id="c" * 32, args={"n": 1, "x": 1}, by="\ud800"))
    _drop(root, "d" * 32 + ".json", _body(id="d" * 32, created=10**400))
    await _process(server)
    assert _files(root / "processing") == [] and calls.seen == []
    reasons = sorted(r["reason"] for r in _rejections(root))
    assert any("RecursionError" in r for r in reasons) and any(
        "OverflowError" in r for r in reasons
    )
    assert len(reasons) == 3
    # the same files left behind by a restart: set aside, and the inbox goes on working
    for name in ("e" * 32, "f" * 32):
        (root / "processing" / f"{name}.json").write_bytes(b"[" * 60000)
    later = AdminServer(FakeClient(), root, _handlers(calls), _schemas(), poll_seconds=0.02)
    assert later.prepare() == 2
    request_id = admin.write_request(root, "test.echo", {"n": 3})
    await _process(later)
    assert admin.read_result(root, request_id)[0] == "done"


async def test_a_context_check_that_cannot_reach_discord_rejects_it(
    root, server, calls, monkeypatch
) -> None:
    from discord_codex_bot import admin_server

    async def down(client, args):
        raise discord.HTTPException(types.SimpleNamespace(status=503, reason="down"), "down")

    monkeypatch.setattr(admin_server, "check_context", down)
    request_id = admin.write_request(root, "test.ctx", {"guild": GUILD, "user": MEMBER})
    await _process(server)
    status, document = admin.read_result(root, request_id)
    assert status == "rejected" and "無法向 Discord 確認" in document["reason"]
    assert calls.seen == [] and _files(root / "processing") == []


async def test_a_request_file_of_another_user_is_not_read(root, server, calls, monkeypatch) -> None:
    real = os.fstat

    def foreign(fd):
        info = real(fd)
        return types.SimpleNamespace(
            st_mode=info.st_mode, st_uid=info.st_uid + 1, st_size=info.st_size
        )

    request_id = admin.write_request(root, "test.echo", {"n": 1})
    monkeypatch.setattr(os, "fstat", foreign)
    await _process(server)
    monkeypatch.undo()
    status, document = admin.read_result(root, request_id)
    assert status == "rejected" and "不是 Bot 的使用者" in document["reason"]
    assert calls.seen == []


def test_an_interrupted_cli_withdraws_its_request(root, server, monkeypatch, capsys) -> None:
    monkeypatch.setattr(admin, "load_schemas", _schemas)

    def interrupted(_seconds):
        raise KeyboardInterrupt

    monkeypatch.setattr(admin.time, "sleep", interrupted)
    assert admin.main(["test", "echo", "--n", "1"], root) != 0
    assert _files(root / "inbox") == []  # withdrawn: it cannot run later behind their back
    assert "已送出" in capsys.readouterr().err


async def test_a_request_nobody_waits_for_any_more_does_not_run(root, server, calls) -> None:
    _drop(root, "a" * 32 + ".json", _body(created=time.time() - 3 * 3600))
    await _process(server)
    assert calls.seen == [] and len(_rejections(root)) == 1
