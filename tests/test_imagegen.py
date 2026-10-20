"""Image generation (imagegen.py and its two entry points in bot.py) against a fake image
service speaking the contract in docs/image-service.md over real HTTP."""

import asyncio
import types
from dataclasses import replace
from datetime import timedelta

import discord
import pytest
from aiohttp import web
from aiohttp.test_utils import TestServer

from discord_codex_bot import admin, admin_image, imagegen
from discord_codex_bot import bot as bot_module
from discord_codex_bot.admin_server import AdminServer
from discord_codex_bot.bot import IMAGE_STARTED, DiscordCodexClient
from discord_codex_bot.codex import CodexResult, _prompt
from discord_codex_bot.imagegen import ImageGenError, ImageRequest, ImageSettings
from discord_codex_bot.linkclean import SwitchStore
from discord_codex_bot.ui import CANCELLED

GUILD, OTHER_GUILD, USER = 111111111111111111, 333333333333333333, 5
ALLOWED = 222222222222222222  # the conftest allowlisted channel
PNG = b"\x89PNG\r\n\x1a\n" + b"0" * 32
KEY = "img-SECRET-key"
CATALOG = {
    "models": [
        {
            "id": "chroma1-flash",
            "name": "Chroma1 Flash",
            "prompt_style": "natural",
            "options": ["seed", "negative", "aspect", "count", "hires", "upscale", "detailer"],
            "defaults": {"aspect": "1:1", "count": 1},
        },
        {
            "id": "plain",
            "name": "plain",
            "options": ["seed", "aspect"],
            "defaults": {"count": 2},
        },
    ],
    "default_model": "chroma1-flash",
    "aspects": ["1:1", "3:4", "4:3", "9:16", "16:9"],
    "upscale": ["off", "2x", "4x"],
    "max_count": 4,
}


class FakeImageService:
    """The image service: records every call; a job reports `queued` for `queued_polls` polls,
    stays `generating` while `hold` is not set, then ends in `final`."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, str, dict | None, str | None]] = []
        self.catalog = CATALOG
        self.submit_reply: tuple[int, dict] | None = None
        self.queued_polls = 1
        self.hold: asyncio.Event | None = None
        self.final = "done"
        self.polls = 0
        self.url = ""

    def posts(self) -> list[dict]:
        return [body for method, _, body, _ in self.calls if method == "POST"]

    def deletes(self) -> list[str]:
        return [path for method, path, _, _ in self.calls if method == "DELETE"]

    def app(self) -> web.Application:
        async def record(request: web.Request) -> None:
            body = await request.json() if request.method == "POST" else None
            self.calls.append(
                (request.method, request.path, body, request.headers.get("Authorization"))
            )

        async def models(request):
            await record(request)
            return web.json_response(self.catalog)

        async def submit(request):
            await record(request)
            if self.submit_reply is not None:
                return web.json_response(self.submit_reply[1], status=self.submit_reply[0])
            return web.json_response(
                {"job_id": "j-1", "state": "queued", "position": 2}, status=202
            )

        async def job(request):
            await record(request)
            self.polls += 1
            state = {"job_id": "j-1", "position": 0, "step": 0, "steps": 8, "images": []}
            if self.polls <= self.queued_polls:
                return web.json_response({**state, "state": "queued", "position": 1})
            if self.hold is not None and not self.hold.is_set():
                return web.json_response({**state, "state": "generating", "step": 3})
            images = [{"index": 0, "bytes": len(PNG), "mime": "image/png", "seed": 42}]
            return web.json_response(
                {**state, "state": self.final, "images": images if self.final == "done" else []}
            )

        async def image(request):
            await record(request)
            return web.Response(body=PNG, content_type="image/png")

        async def cancel(request):
            await record(request)
            return web.json_response({"state": "cancelled"})

        app = web.Application()
        app.add_routes(
            [
                web.get("/v1/image/models", models),
                web.post("/v1/image/jobs", submit),
                web.get("/v1/image/jobs/{job_id}", job),
                web.get("/v1/image/jobs/{job_id}/images/{index}", image),
                web.delete("/v1/image/jobs/{job_id}", cancel),
            ]
        )
        return app


@pytest.fixture(autouse=True)
def _quick_polls(monkeypatch):
    monkeypatch.setattr(imagegen, "POLL_SECONDS", 0.01)
    monkeypatch.setattr(imagegen, "_last_catalog", None)


@pytest.fixture
async def service():
    fake = FakeImageService()
    async with TestServer(fake.app()) as server:
        fake.url = f"http://127.0.0.1:{server.port}"
        yield fake


@pytest.fixture
def client(config, tmp_path, service) -> DiscordCodexClient:
    cfg = replace(
        config,
        codex_home=tmp_path / "codex",
        permanent_memory_dir=tmp_path / "perm",
        attachment_dir=tmp_path / "attachments",
        memory_recall_rounds=2,
        image_gen_url=service.url + "/",
        image_gen_api_key=KEY,
    )
    return DiscordCodexClient(cfg)


class FakeChannel:
    def __init__(self, channel_id: int = ALLOWED, guild_id: int = GUILD) -> None:
        self.id = channel_id
        self.parent_id = None
        self.guild = types.SimpleNamespace(id=guild_id)
        self.sent: list[dict] = []

    async def send(self, text, files=None, allowed_mentions=None, view=None):
        self.sent.append(
            {
                "text": text,
                "files": [f.filename for f in files or ()],
                "mentions": allowed_mentions,
            }
        )
        return types.SimpleNamespace(id=len(self.sent))


class FakeInteraction:
    def __init__(self, guild_id: int = GUILD, channel_id: int = ALLOWED, created_at=None) -> None:
        self.guild_id = guild_id
        self.channel_id = channel_id
        self.channel = FakeChannel(channel_id, guild_id)
        self.guild = types.SimpleNamespace(id=guild_id, filesize_limit=9_000_000)
        self.user = types.SimpleNamespace(id=USER)
        self.created_at = created_at or discord.utils.utcnow()
        self.messages: list[tuple[str, bool]] = []
        self.deferred = False
        self.edits: list[dict] = []
        self.followups: list[str] = []
        outer = self

        class Response:
            async def send_message(self, text, ephemeral=False):
                outer.messages.append((text, ephemeral))

            async def defer(self, thinking=False):
                outer.deferred = True

        class Followup:
            async def send(self, text, ephemeral=False):
                outer.followups.append(text)

        self.response, self.followup = Response(), Followup()

    async def edit_original_response(self, content=None, view=None, attachments=None):
        self.edits.append(
            {
                "content": content,
                "view": view,
                "files": [f.filename for f in attachments or ()],
            }
        )


# ----- the client module --------------------------------------------------------------------


def test_resolve_checks_every_option_against_the_catalog() -> None:
    catalog = imagegen.parse_catalog(CATALOG)
    assert imagegen.resolve(catalog, ImageRequest("貓")).id == "chroma1-flash"
    assert imagegen.resolve(catalog, ImageRequest("貓", "plain", seed=1)).id == "plain"
    for request, code in (
        (ImageRequest("貓", "nope"), "unknown_model"),
        (ImageRequest("貓", "plain", detailer=True), "unsupported_option"),
        (ImageRequest("貓", "plain", negative="人"), "unsupported_option"),
        (ImageRequest("貓", aspect="2:1"), "bad_value"),
        (ImageRequest("貓", upscale="8x"), "bad_value"),
        (ImageRequest("貓", count=5), "bad_value"),
    ):
        with pytest.raises(ImageGenError) as caught:
            imagegen.resolve(catalog, request)
        assert caught.value.code == code and caught.value.fixable
    # A model's default count is what a request without count= costs.
    assert imagegen.image_count(catalog, ImageRequest("貓", "plain")) == 2
    assert imagegen.image_count(None, ImageRequest("貓")) == 1


def test_tags_in_code_or_quotes_are_not_read_and_attributes_are_checked() -> None:
    answer = '好\n```\n<image_gen prompt="例子"/>\n```\n> <image_gen prompt="引用"/>\n'
    clean, asks = imagegen.extract_image_tags(answer + '<image_gen prompt="貓" aspect="3:4"/>')
    assert asks == [{"prompt": "貓", "aspect": "3:4"}]
    assert "例子" in clean and "引用" in clean and 'prompt="貓"' not in clean
    request, problem = imagegen.request_from_tag({"prompt": "貓", "count": "2", "seed": "7"})
    assert problem == "" and (request.count, request.seed) == (2, 7)
    assert "missing" in imagegen.request_from_tag({"model": "x"})[1]
    assert "whole number" in imagegen.request_from_tag({"prompt": "貓", "count": "兩"})[1]
    # Characters str.isdigit() accepts but int() rejects are refused, not raised.
    for name, raw in (("count", "³"), ("seed", "²"), ("seed", "١٢")):
        assert "whole number" in imagegen.request_from_tag({"prompt": "貓", name: raw})[1]
    # The seed range the slash command allows; past it the service would refuse a charged job.
    top = str(imagegen.MAX_SEED)
    assert imagegen.request_from_tag({"prompt": "貓", "seed": top})[0].seed == imagegen.MAX_SEED
    assert "seed must be" in imagegen.request_from_tag({"prompt": "貓", "seed": top + "0"})[1]
    assert "unknown attribute" in imagegen.request_from_tag({"prompt": "貓", "hires": "1"})[1]


async def test_run_job_submits_polls_and_downloads_with_the_key(service, client, tmp_path):
    seen = []

    async def on_progress(job):
        seen.append(job.state)

    request = ImageRequest("貓", aspect="3:4")
    out = tmp_path / "out"
    result = await imagegen.run_job(
        client.images, request, out, 8_000_000, GUILD, USER, on_progress
    )
    assert [p.name for p in result.paths] == ["image-0.png"] and result.seeds == (42,)
    assert result.paths[0].read_bytes() == PNG
    assert seen[0] == "queued" and seen[-1] == "done"
    body = service.posts()[0]
    assert body["model"] == "chroma1-flash" and body["aspect"] == "3:4" and body["seed"] is None
    assert body["max_bytes"] == 8_000_000
    assert body["requester"] == {"guild": str(GUILD), "user": str(USER)}
    assert {auth for *_, auth in service.calls} == {f"Bearer {KEY}"}
    assert service.deletes() == [], "a finished job is not cancelled"


async def test_service_refusals_become_member_messages(service, client, tmp_path) -> None:
    for status, payload, code in (
        (429, {"error": "queue_full", "queue": 5}, "queue_full"),
        (503, {"error": "unavailable", "detail": "GPU busy"}, "unavailable"),
        (400, {"error": "unsupported_option", "option": "detailer"}, "unsupported_option"),
    ):
        service.submit_reply = (status, payload)
        with pytest.raises(ImageGenError) as caught:
            await imagegen.run_job(
                client.images, ImageRequest("貓"), tmp_path, 8_000_000, GUILD, USER
            )
        assert caught.value.code == code
    assert "GPU busy" not in str(caught.value)
    service.submit_reply = None
    service.final = "failed"
    with pytest.raises(ImageGenError, match="失敗"):
        await imagegen.run_job(client.images, ImageRequest("貓"), tmp_path, 8_000_000, GUILD, USER)


def test_settings_default_validate_and_count_per_day(tmp_path) -> None:
    days = iter(["2026-10-20", "2026-10-20", "2026-10-21", "2026-10-30"])
    settings = ImageSettings(SwitchStore(tmp_path / "s.sqlite3"), today=lambda: next(days))
    assert settings.enabled(GUILD) and settings.daily_limit(GUILD) == 20
    assert settings.in_flight_limit(GUILD) == 1
    settings.update(GUILD, enabled=False, daily=5, in_flight=2)
    assert (settings.enabled(GUILD), settings.daily_limit(GUILD)) == (False, 5)
    with pytest.raises(ValueError):
        settings.update(GUILD, daily=0)
    day = settings.charge(GUILD, USER, 3)
    settings.refund(GUILD, USER, 1, day)
    assert settings.used(GUILD, USER) == 2
    assert settings.used(GUILD, USER) == 0, "a new day starts from zero"
    settings.charge(GUILD, USER, 1)  # 10-30: the 10-20 row is past USAGE_KEEP_DAYS
    assert settings.used(GUILD, USER, "2026-10-20") == 0


def test_prompt_offers_the_tag_only_with_the_service(config) -> None:
    assert imagegen.prompt_note(config) == ""
    assert "<image_gen" not in _prompt("畫一隻貓", image_gen=imagegen.prompt_note(config))
    note = imagegen.prompt_note(replace(config, image_gen_url="http://img:18082"))
    built = _prompt("畫一隻貓", image_gen=note)
    assert '<image_gen prompt="what to draw"/>' in built and KEY not in built


def test_help_mentions_drawing_only_with_the_service(client) -> None:
    assert "**會畫圖**" in client.help_guide() and "會畫圖" in client.help_sheet()
    client.images._config = replace(client.config, image_gen_url="")
    assert "會畫圖" not in client.help_sheet()


# ----- the slash command ---------------------------------------------------------------------


async def test_slash_paints_progress_and_replaces_the_placeholder(service, client) -> None:
    interaction = FakeInteraction()
    await client.image_command(interaction, "夕陽下的貓", aspect=None, count=1, seed=9)
    assert interaction.deferred and interaction.messages == []
    assert any("排隊中" in (e["content"] or "") for e in interaction.edits)
    last = interaction.edits[-1]
    assert last["files"] == ["image-0.png"] and last["view"] is None
    assert last["content"].startswith("**畫**：\n> 夕陽下的貓") and "seed 42" in last["content"]
    body = service.posts()[0]
    assert body["max_bytes"] == 9_000_000 and body["count"] == 1 and body["seed"] == 9
    assert client.image_settings.used(GUILD, USER) == 1
    assert client._image_in_flight == {}
    assert not list((client.config.attachment_dir).glob("req-*")), "delivery dir removed"


async def test_slash_past_the_interaction_lifetime_posts_in_the_channel(
    service, client, monkeypatch
) -> None:
    interaction = FakeInteraction()
    asked = interaction.created_at
    monkeypatch.setattr(bot_module.discord.utils, "utcnow", lambda: asked + timedelta(minutes=16))
    await client.image_command(interaction, "海邊的燈塔")
    assert interaction.channel.sent and interaction.channel.sent[0]["files"] == ["image-0.png"]
    assert interaction.channel.sent[0]["text"].startswith(f"<@{USER}> **畫**")
    assert interaction.edits[-1]["content"] == bot_module.LATE_NOTICE
    assert all(not e["files"] for e in interaction.edits)


async def test_slash_cancel_deletes_the_job_on_the_service(service, client) -> None:
    service.hold = asyncio.Event()
    interaction = FakeInteraction()
    command = asyncio.create_task(client.image_command(interaction, "慢慢畫"))
    for _ in range(500):
        if any("生成中 3/8" in (e["content"] or "") for e in interaction.edits):
            break
        await asyncio.sleep(0.01)
    view = interaction.edits[-1]["view"]
    view.task.cancel()  # what the ❌ press does (ui.CancelView.cancel)
    await asyncio.wait_for(command, 5)
    assert service.deletes() == ["/v1/image/jobs/j-1"]
    assert interaction.edits[-1] == {"content": CANCELLED, "view": None, "files": []}
    assert client.image_settings.used(GUILD, USER) == 1, "a cancelled job still counts"
    assert client._image_in_flight == {}


async def test_slash_failure_is_shown_and_refunded(service, client) -> None:
    service.submit_reply = (429, {"error": "queue_full", "queue": 5})
    interaction = FakeInteraction()
    await client.image_command(interaction, "貓")
    assert interaction.edits[-1]["content"] == f"⚠️ {imagegen.QUEUE_FULL}"
    assert client.image_settings.used(GUILD, USER) == 0 and client._image_in_flight == {}


async def test_slash_refusals_never_submit_a_job(service, client) -> None:
    """Negative controls: each guard alone keeps the request off the service's queue."""
    outside = FakeInteraction(guild_id=OTHER_GUILD)
    await client.image_command(outside, "貓")
    assert outside.messages and outside.messages[0][1] and service.calls == []

    client.image_settings.update(GUILD, enabled=False)
    off = FakeInteraction()
    await client.image_command(off, "貓")
    assert off.messages == [("這個伺服器沒有開放圖片生成。", True)] and service.calls == []
    client.image_settings.update(GUILD, enabled=True, daily=3)

    client.image_settings.charge(GUILD, USER, 3)
    over = FakeInteraction()
    await client.image_command(over, "貓")
    assert "額度不夠" in over.messages[0][0] and not over.deferred and service.calls == []
    client.image_settings.update(GUILD, daily=20)

    client._image_in_flight[(GUILD, USER)] = 1
    busy = FakeInteraction()
    await client.image_command(busy, "貓")
    assert "同時最多 1 個" in busy.messages[0][0] and service.calls == []
    client._image_in_flight.clear()

    for kwargs in ({"model": "nope"}, {"model": "plain", "detailer": True}):
        refused = FakeInteraction()
        await client.image_command(refused, "貓", **kwargs)
        assert refused.edits[-1]["content"].startswith("⚠️ ") and refused.edits[-1]["view"] is None
    assert service.posts() == [], "unknown model and unsupported option only read the catalog"
    assert client.image_settings.used(GUILD, USER) == 3, "the refused jobs were given back"


async def test_slash_counts_the_model_default_once_the_catalog_is_read(service, client) -> None:
    """A slash command admits its job before anything has read the catalog, at one image; the
    job counts the default model's own count once it reads it, and keeps to the daily limit."""
    service.catalog = {**CATALOG, "default_model": "plain"}  # plain makes 2 by default
    assert client.images.cached() is None
    await client.image_command(FakeInteraction(), "貓")
    assert client.image_settings.used(GUILD, USER) == 2 and len(service.posts()) == 1

    client.image_settings.update(GUILD, daily=3)
    over = FakeInteraction()
    client.images._catalog = None  # a restart: admitted at one image again (2 + 1 <= 3)
    await client.image_command(over, "貓")
    assert "額度不夠" in over.edits[-1]["content"] and len(service.posts()) == 1
    assert client.image_settings.used(GUILD, USER) == 2 and client._image_in_flight == {}


async def test_slash_without_the_service_says_it_is_off(client) -> None:
    client.images._config = replace(client.config, image_gen_url="")
    interaction = FakeInteraction()
    await client.image_command(interaction, "貓")
    assert interaction.messages == [(bot_module.IMAGE_OFF, True)]


async def test_model_autocomplete_reads_the_catalog_for_served_places_only(service, client):
    choices = await client.image_model_options("chroma")
    assert [(c.name, c.value) for c in choices] == [
        ("Chroma1 Flash（chroma1-flash）", "chroma1-flash")
    ]
    calls = len(service.calls)
    outside = types.SimpleNamespace(
        client=client, guild_id=OTHER_GUILD, channel=None, channel_id=ALLOWED
    )
    assert await bot_module._image_model_autocomplete(outside, "") == []
    assert len(service.calls) == calls


# ----- the <image_gen/> tag ------------------------------------------------------------------


def _codex_replies(monkeypatch, *texts: str) -> list[str]:
    prompts: list[str] = []
    replies = iter(texts)

    async def fake_codex(text, config, **kw):
        prompts.append(text)
        return CodexResult(next(replies), (), None, "t", bool(kw.get("resume")))

    monkeypatch.setattr(bot_module, "run_codex", fake_codex)
    return prompts


async def _delivered(client) -> None:
    await asyncio.wait_for(asyncio.gather(*client._image_tasks), 5)


async def test_tag_starts_a_detached_job_posted_without_pings(service, client, monkeypatch):
    channel = FakeChannel()
    monkeypatch.setattr(client, "get_channel", {ALLOWED: channel}.get)
    _codex_replies(monkeypatch, '好，馬上畫。\n<image_gen prompt="一隻橘貓" aspect="3:4"/>')
    result = await client._answer("幫我畫一隻橘貓", [], GUILD, USER, channel_id=ALLOWED)
    assert result.text == f"好，馬上畫。\n\n{IMAGE_STARTED}"
    await _delivered(client)
    sent = channel.sent[0]
    assert sent["files"] == ["image-0.png"] and sent["text"].startswith(f"<@{USER}> **畫**")
    assert sent["mentions"].to_dict() == discord.AllowedMentions.none().to_dict()
    assert service.posts()[0]["aspect"] == "3:4"
    assert client.image_settings.used(GUILD, USER) == 1 and client._image_in_flight == {}


async def test_tag_delivery_rechecks_the_channel(service, client, monkeypatch) -> None:
    channel = FakeChannel()
    monkeypatch.setattr(client, "get_channel", {ALLOWED: channel}.get)
    service.hold = asyncio.Event()
    _codex_replies(monkeypatch, '<image_gen prompt="貓"/>')
    await client._answer("畫貓", [], GUILD, USER, channel_id=ALLOWED)
    # The channel leaves the allowlist while the job runs.
    client.config = replace(client.config, allowed_channel_ids=frozenset({999}))
    service.hold.set()
    await _delivered(client)
    assert channel.sent == [] and len(service.posts()) == 1


async def test_unknown_model_tag_goes_back_to_the_model_and_is_fixed(service, client, monkeypatch):
    channel = FakeChannel()
    monkeypatch.setattr(client, "get_channel", {ALLOWED: channel}.get)
    prompts = _codex_replies(
        monkeypatch,
        '畫好了。\n<image_gen prompt="貓" model="dalle"/>',
        '<image_gen prompt="貓" model="plain"/>',
    )
    result = await client._answer("用 dalle 畫貓", [], GUILD, USER, channel_id=ALLOWED)
    assert "<TAG_ERRORS>" in prompts[1] and "unknown model" in prompts[1] and "plain" in prompts[1]
    assert "沒有「dalle」這個圖片模型" in result.text and result.text.endswith(IMAGE_STARTED)
    await _delivered(client)
    assert [body["model"] for body in service.posts()] == ["plain"]


async def test_tag_refusals_never_submit_a_job(service, client, monkeypatch) -> None:
    """Negative controls for the tag: shown (code/quote) tags, an unknown model, an unsupported
    option, a disabled guild, a member over the limit and a place the Bot does not serve."""
    monkeypatch.setattr(client, "get_channel", {ALLOWED: FakeChannel()}.get)
    _codex_replies(
        monkeypatch,
        '例子：\n```\n<image_gen prompt="貓"/>\n```\n> <image_gen prompt="狗"/>',
        '<image_gen prompt="貓" model="nope"/>',
        "",  # the retry gives up
        '<image_gen prompt="貓" model="plain" negative="人"/>',
        "",
        '<image_gen prompt="貓"/>',
        '<image_gen prompt="貓"/>',
        '<image_gen prompt="貓"/>',
    )
    shown = await client._answer("怎麼請你畫圖", [], GUILD, USER, channel_id=ALLOWED)
    assert '<image_gen prompt="貓"/>' in shown.text and service.calls == []
    unknown = await client._answer("畫", [], GUILD, USER, channel_id=ALLOWED)
    unsupported = await client._answer("畫", [], GUILD, USER, channel_id=ALLOWED)
    assert "圖片沒有生成" in unknown.text and "不支援 negative" in unsupported.text
    client.image_settings.update(GUILD, enabled=False)
    off = await client._answer("畫", [], GUILD, USER, channel_id=ALLOWED)
    assert "這個伺服器沒有開放圖片生成" in off.text
    client.image_settings.update(GUILD, enabled=True, daily=1)
    client.image_settings.charge(GUILD, USER, 1)
    over = await client._answer("畫", [], GUILD, USER, channel_id=ALLOWED)
    assert "額度不夠" in over.text
    client.image_settings.update(GUILD, daily=20)
    outside = await client._answer("畫", [], OTHER_GUILD, USER, channel_id=ALLOWED)
    assert "圖片沒有生成" in outside.text
    assert client._image_tasks == set() and service.posts() == []


async def test_tag_without_the_service_is_not_run(client, monkeypatch) -> None:
    client.images._config = replace(client.config, image_gen_url="")
    _codex_replies(monkeypatch, '畫好了\n<image_gen prompt="貓"/>')
    result = await client._answer("畫貓", [], GUILD, USER, channel_id=ALLOWED)
    assert result.text == "畫好了\n\n（圖片生成尚未啟用。）" and client._image_tasks == set()


# ----- operator commands ---------------------------------------------------------------------


async def test_admin_image_get_and_set(client, tmp_path) -> None:
    root = tmp_path / "admin"
    server = AdminServer(client, root, admin_image.HANDLERS, admin_image.SCHEMAS)
    server.prepare()

    async def run(command: str, **args):
        request_id = admin.write_request(root, command, args)
        await server.pass_once()
        await server.drain()
        return admin.read_result(root, request_id)

    status, document = await run("image.get", guild=GUILD)
    assert document["result"]["text"] == "圖片生成：開\n每人每天：20 張\n每人同時：1 個工作"
    status, document = await run("image.set", guild=GUILD, state="off", daily=5, in_flight=2)
    assert document["result"] | {"text": ""} == {
        "text": "",
        "enabled": False,
        "service": True,
        "daily": 5,
        "in_flight": 2,
    }
    assert not client.image_settings.enabled(GUILD)
    client.image_settings.charge(GUILD, USER, 2)
    status, document = await run("image.get", guild=GUILD, user=USER)
    assert document["result"]["used"] == 2
    status, document = await run("image.set", guild=GUILD)
    assert status == "rejected"
    status, document = await run("image.set", guild=OTHER_GUILD, state="off")
    assert status == "rejected" and client.image_settings.enabled(OTHER_GUILD)
