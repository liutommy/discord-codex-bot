"""Image generation (imagegen.py and its two entry points in bot.py) against a fake image
service speaking the contract in docs/image-service.md over real HTTP."""

import asyncio
import base64
import json
import types
from dataclasses import replace
from datetime import timedelta

import discord
import pytest
from aiohttp import web
from aiohttp.test_utils import TestServer

from discord_codex_bot import admin, admin_image, characters, imagegen
from discord_codex_bot import bot as bot_module
from discord_codex_bot import help as help_module
from discord_codex_bot.admin_server import AdminServer
from discord_codex_bot.bot import IMAGE_STARTED, DiscordCodexClient
from discord_codex_bot.codex import CodexResult, _prompt
from discord_codex_bot.imagegen import ImageGenError, ImageRequest, ImageSettings
from discord_codex_bot.linkclean import SwitchStore
from discord_codex_bot.ui import CANCELLED

GUILD, OTHER_GUILD, USER = 111111111111111111, 333333333333333333, 5
ALLOWED = 222222222222222222  # the conftest allowlisted channel
PNG = b"\x89PNG\r\n\x1a\n" + b"0" * 32
GIF = b"GIF89a" + b"0" * 32
KEY = "img-SECRET-key"
CATALOG = {
    "models": [
        {
            "id": "chroma1-flash",
            "name": "Chroma1 Flash",
            "prompt_style": "natural",
            "options": [
                "seed",
                "negative",
                "aspect",
                "count",
                "enhance",
                "init_image",
                "strength",
                "mask",
                "upscale",
                "detailer",
            ],
            "enhancers": ["local", "off"],
            "defaults": {"aspect": "1:1", "count": 1, "enhance": "local", "strength": 0.6},
        },
        {
            "id": "plain",
            "name": "plain",
            "options": ["seed", "aspect"],
            "defaults": {"count": 2},
        },
        {
            "id": "wai-illustrious",
            "name": "WAI Illustrious",
            "options": ["seed", "init_image", "strength", "ref_image", "ref_strength"],
            "defaults": {"strength": 0.75, "ref_strength": 0.7},
        },
    ],
    "default_model": "chroma1-flash",
    "aspects": ["1:1", "3:4", "4:3", "9:16", "16:9"],
    "upscale": ["off", "2x", "4x"],
    "max_count": 4,
    "max_input_bytes": 1000,
}


IMAGE_CHUNKS = 4


class FakeImageService:
    """The image service (contract v2): records every call. A submitted job waits in the shared
    GPU queue for `queued_polls` polls, its `position` (items ahead) falling to 0 and its
    `queued_s` growing; it then stays `generating` while `hold` is not set, and ends in `final`.
    `submit_reply` replaces the 202 (a 429 queue_full, say); an input over the catalog's
    max_input_bytes is refused with 413, as the service does."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, str, dict | None, str | None]] = []
        self.catalog = CATALOG
        self.submitted: asyncio.Queue[dict] = asyncio.Queue()
        self.submit_reply: tuple[int, dict] | None = None
        self.queued_polls = 1
        self.hold: asyncio.Event | None = None
        self.final = "done"
        self.enhance_used = "local"
        self.sensitive = False
        self.polls = 0
        self.lora_fails = False  # a job with a LoRA ends failed with lora_unavailable
        self._failing = False
        self.url = ""
        self.image = PNG  # sent in IMAGE_CHUNKS writes, as a real server streams a large file

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
            self.submitted.put_nowait(self.calls[-1][2])
            self._failing = self.lora_fails and bool(self.calls[-1][2].get("lora"))
            if self.submit_reply is not None:
                return web.json_response(self.submit_reply[1], status=self.submit_reply[0])
            body = self.calls[-1][2]
            limit = self.catalog.get("max_input_bytes", 0)
            for name in ("init_image", "mask", "ref_image"):
                if body.get(name) and len(base64.b64decode(body[name]["data"])) > limit:
                    error = {"error": "input_too_large", "max_input_bytes": limit}
                    return web.json_response(error, status=413)
            return web.json_response(
                {"job_id": "j-1", "state": "queued", "position": 2}, status=202
            )

        async def job(request):
            await record(request)
            self.polls += 1
            queued_s = float(min(self.polls, self.queued_polls))
            state = {
                "job_id": "j-1",
                "position": 0,
                "step": 0,
                "steps": 8,
                "model": "chroma1-flash",
                "enhance_used": self.enhance_used,
                "sensitive": self.sensitive,
                "images": [],
                "error": None,
                "queued_s": queued_s,
                "elapsed_s": 0.0,
            }
            if self.polls <= self.queued_polls:
                ahead = self.queued_polls - self.polls
                return web.json_response({**state, "state": "queued", "position": ahead})
            if self._failing:
                return web.json_response({**state, "state": "failed", "error": "lora_unavailable"})
            if self.hold is not None and not self.hold.is_set():
                return web.json_response({**state, "state": "generating", "step": 3})
            images = [{"index": 0, "bytes": len(PNG), "mime": "image/png", "seed": 42}]
            return web.json_response(
                {**state, "state": self.final, "images": images if self.final == "done" else []}
            )

        async def image(request):
            await record(request)
            response = web.StreamResponse(headers={"Content-Type": "image/png"})
            await response.prepare(request)
            step = -(-len(self.image) // IMAGE_CHUNKS)
            for start in range(0, len(self.image), step):
                await response.write(self.image[start : start + step])
                await asyncio.sleep(0.01)
            await response.write_eof()
            return response

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


class FakeAttachment:
    """A discord.Attachment as the slash command sees it: the type and size Discord reports, and
    the bytes, counting each read (a refused request must not download anything)."""

    def __init__(
        self,
        data: bytes,
        content_type: str | None = "image/png",
        size: int | None = None,
        filename: str = "image.png",
    ):
        self.id = 77
        self.data = data
        self.content_type = content_type
        self.filename = filename
        self.size = len(data) if size is None else size
        self.reads = 0

    async def read(self) -> bytes:
        self.reads += 1
        return self.data


def _wire(data: bytes, mime: str = "image/png") -> dict:
    return {"mime": mime, "data": base64.b64encode(data).decode("ascii")}


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
        # enhance is checked against the model's own enhancers, not the three names
        (ImageRequest("貓", enhance="cloud"), "bad_value"),
        (ImageRequest("貓", "plain", enhance="off"), "unsupported_option"),
    ):
        with pytest.raises(ImageGenError) as caught:
            imagegen.resolve(catalog, request)
        assert caught.value.code == code and caught.value.fixable
    assert imagegen.resolve(catalog, ImageRequest("貓", enhance="off")).id == "chroma1-flash"


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
    # The seed range the slash command allows; values past it are refused before submission.
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
    assert body["ref_image"] is None and body["ref_strength"] is None, "unset is sent as null"
    assert body["max_bytes"] == 8_000_000
    assert body["requester"] == {"guild": str(GUILD), "user": str(USER)}
    assert {auth for *_, auth in service.calls} == {f"Bearer {KEY}"}
    assert service.deletes() == [], "a finished job is not cancelled"


def test_input_images_are_checked_before_anything_is_read() -> None:
    """Negative controls for image-to-image: each guard alone refuses the request, from the
    type and size an attachment states, before a byte of it is read."""
    catalog = imagegen.parse_catalog(CATALOG)
    small = imagegen.InputImage("image/png", 10)
    ok = ImageRequest("貓", init_image=small, mask=small, strength=0.5)
    assert imagegen.resolve(catalog, ok).id == "chroma1-flash"
    for request, code, said in (
        (ImageRequest("貓", mask=small), "bad_value", "「遮罩」 要搭配 「改這張圖」"),
        (ImageRequest("貓", strength=0.5), "bad_value", "「改動幅度」 要搭配 「改這張圖」"),
        (replace(ok, strength=1.5), "bad_value", "「改動幅度」 只能是 0-1"),
        (replace(ok, init_image=imagegen.InputImage("image/png", 1001)), "input_too_large", ""),
        (replace(ok, mask=imagegen.InputImage("image/png", 1001)), "input_too_large", ""),
        (replace(ok, init_image=imagegen.InputImage("image/gif", 10)), "bad_value", "PNG"),
        (ImageRequest("貓", "plain", init_image=small), "unsupported_option", "改這張圖"),
    ):
        with pytest.raises(ImageGenError) as caught:
            imagegen.resolve(catalog, request)
        assert caught.value.code == code and said in str(caught.value)
    # A reference image: alone or with init_image, on a model that lists it.
    ref = ImageRequest("貓", "wai-illustrious", ref_image=small, ref_strength=0.5)
    assert imagegen.resolve(catalog, ref).id == "wai-illustrious"
    both = replace(ref, init_image=small, strength=0.5)
    assert imagegen.resolve(catalog, both).id == "wai-illustrious"
    for request, code, said in (
        (replace(ref, ref_image=None), "bad_value", "「角色相似度」 要搭配 「照這個角色」"),
        (replace(ref, ref_strength=1.5), "bad_value", "「角色相似度」 只能是 0-1"),
        (replace(ref, ref_strength=-0.1), "bad_value", "「角色相似度」 只能是 0-1"),
        (replace(ref, ref_image=imagegen.InputImage("image/png", 1001)), "input_too_large", ""),
        (replace(ref, ref_image=imagegen.InputImage("image/gif", 10)), "bad_value", "PNG"),
        (replace(ok, ref_image=small), "unsupported_option", "照這個角色"),
        (ImageRequest("貓", ref_strength=0.5), "unsupported_option", "角色相似度"),
        # Each image fits, their sum does not: the service caps the total.
        (
            replace(
                both,
                init_image=imagegen.InputImage("image/png", 600),
                ref_image=imagegen.InputImage("image/png", 600),
            ),
            "input_too_large",
            "合計",
        ),
    ):
        with pytest.raises(ImageGenError) as caught:
            imagegen.resolve(catalog, request)
        assert caught.value.code == code and said in str(caught.value), request
    # A service that states no max_input_bytes takes no input image.
    bare = imagegen.parse_catalog({k: v for k, v in CATALOG.items() if k != "max_input_bytes"})
    with pytest.raises(ImageGenError, match="太大"):
        imagegen.resolve(bare, ImageRequest("貓", init_image=small))
    # The type of the bytes, not of the name: a GIF sent as image/png is not a PNG.
    assert imagegen.input_image(PNG).mime == "image/png"
    assert imagegen.input_image(b"\xff\xd8\xff\xe0" + b"0" * 8).mime == "image/jpeg"
    assert imagegen.input_image(b"RIFF\x00\x00\x00\x00WEBPVP8 ").mime == "image/webp"
    assert imagegen.input_image(GIF).mime == ""


async def test_service_refusals_become_member_messages(service, client, tmp_path) -> None:
    for status, payload, code in (
        (429, {"error": "queue_full", "queue": 3}, "queue_full"),
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
    service.submit_reply = (413, {"error": "input_too_large", "max_input_bytes": 12_000_000})
    with pytest.raises(ImageGenError, match="附圖合計最多 12 MB"):
        await imagegen.run_job(client.images, ImageRequest("貓"), tmp_path, 8_000_000, GUILD, USER)
    service.submit_reply = None
    service.final = "failed"
    with pytest.raises(ImageGenError, match="失敗"):
        await imagegen.run_job(client.images, ImageRequest("貓"), tmp_path, 8_000_000, GUILD, USER)


@pytest.fixture
def fake_time(monkeypatch):
    """A wall clock that moves only between polls, by `step` seconds each: a job queued for N
    polls has waited N * step seconds, however fast the test runs."""
    now = {"t": 1_000_000.0, "step": 100.0}

    async def sleep_for(seconds, clock=None):
        now["t"] += now["step"]
        await asyncio.sleep(0)

    monkeypatch.setattr(imagegen, "sleep_for", sleep_for)
    return now


async def test_a_large_image_arriving_in_parts_is_saved_whole(service, client, tmp_path) -> None:
    """Negative control for the download read: a 1.2 MB image streamed in several writes is
    saved byte for byte; one read() returned only the first part and posted a broken file."""
    service.image = PNG + bytes(1_200_000)
    result = await imagegen.run_job(
        client.images, ImageRequest("貓"), tmp_path / "big", 8_000_000, GUILD, USER
    )
    assert result.paths[0].read_bytes() == service.image
    service.polls = 0
    with pytest.raises(ImageGenError, match="失敗"):  # still refused past max_bytes
        await imagegen.run_job(
            client.images, ImageRequest("貓"), tmp_path / "over", 1_000_000, GUILD, USER
        )


async def test_the_run_timeout_starts_when_the_job_leaves_the_queue(
    service, client, tmp_path, fake_time
) -> None:
    """Negative control for the wait/run split: 600 s in the queue then 200 s generating is
    inside a 300 s run timeout; counted from submission it would be 800 s and time out."""
    client.images._config = replace(
        client.config, image_gen_timeout_seconds=300, image_gen_queue_wait_seconds=1000
    )
    service.queued_polls = 6
    service.hold = asyncio.Event()
    seen = []

    async def on_progress(job):
        seen.append((job.state, job.position))
        if job.state == "generating" and len([s for s, _ in seen if s == "generating"]) == 2:
            service.hold.set()

    result = await imagegen.run_job(
        client.images,
        ImageRequest("貓"),
        tmp_path / "out",
        8_000_000,
        GUILD,
        USER,
        on_progress,
        clock=lambda: fake_time["t"],
    )
    assert result.paths and service.deletes() == []
    # The progress shows the items ahead while queued, then the steps.
    assert seen[:3] == [("queued", 2), ("queued", 5), ("queued", 4)]
    queued = imagegen.Job("j", "queued", position=2)
    assert imagegen.progress_text(queued) == "🎨 排隊中，前面還有 2 個…"
    generating = imagegen.Job("j", "generating", step=3, steps=8)
    assert imagegen.progress_text(generating) == "🎨 生成中 3/8…"
    # ...and a job that runs past the run timeout once out of the queue still times out.
    service.polls, service.queued_polls, service.hold = 0, 1, asyncio.Event()
    with pytest.raises(ImageGenError) as caught:
        await imagegen.run_job(
            client.images,
            ImageRequest("貓"),
            tmp_path / "out2",
            8_000_000,
            GUILD,
            USER,
            clock=lambda: fake_time["t"],
        )
    assert caught.value.code == "timeout" and service.deletes() == ["/v1/image/jobs/j-1"]


async def test_a_job_stuck_in_the_queue_is_given_up_and_cancelled(
    service, client, tmp_path, fake_time
) -> None:
    client.images._config = replace(
        client.config, image_gen_timeout_seconds=300, image_gen_queue_wait_seconds=1000
    )
    service.queued_polls = 10_000  # a service that never starts it
    with pytest.raises(ImageGenError) as caught:
        await asyncio.wait_for(
            imagegen.run_job(
                client.images,
                ImageRequest("貓"),
                tmp_path,
                8_000_000,
                GUILD,
                USER,
                clock=lambda: fake_time["t"],
            ),
            5,
        )
    assert caught.value.code == "queue_timeout" and str(caught.value) == imagegen.QUEUE_TIMED_OUT
    assert service.deletes() == ["/v1/image/jobs/j-1"], "it gives up its place in the queue"
    assert 10 <= service.polls <= 11


def test_settings_defaults_validate_and_persist(tmp_path) -> None:
    path = tmp_path / "s.sqlite3"
    settings = ImageSettings(SwitchStore(path))
    assert settings.enabled(GUILD) and settings.spoiler(GUILD) == "auto"
    settings.update(GUILD, enabled=False, spoiler="always")
    restored = ImageSettings(SwitchStore(path))
    assert not restored.enabled(GUILD) and restored.spoiler(GUILD) == "always"
    assert restored.enabled(OTHER_GUILD) and restored.spoiler(OTHER_GUILD) == "auto"
    with pytest.raises(ValueError):
        settings.update(GUILD, spoiler="sometimes")


def test_prompt_offers_the_tag_only_with_the_service(config) -> None:
    assert imagegen.prompt_note(config) == ""
    assert "<image_gen" not in _prompt("畫一隻貓", image_gen=imagegen.prompt_note(config))
    note = imagegen.prompt_note(replace(config, image_gen_url="http://img:18082"))
    built = _prompt("畫一隻貓", image_gen=note)
    assert '<image_gen prompt="what to draw"/>' in built and KEY not in built
    assert "text only" in note and "照這個角色" in note
    # The member guide for -image is the one source the model explains from.
    assert help_module.COMMAND_GUIDE["-image"][0] in note


def test_prompt_names_the_reference_default_from_the_catalog(config, monkeypatch) -> None:
    edit = {"id": "edit", "name": "edit", "options": ["ref_image"], "defaults": {}}
    on = replace(config, image_gen_url="http://img:18082")
    catalog = {**CATALOG, "models": [*CATALOG["models"], edit], "default_ref_model": "edit"}
    monkeypatch.setattr(imagegen, "_last_catalog", imagegen.parse_catalog(catalog))
    assert "(default chroma1-flash; with a reference image in -image, edit)" in (
        imagegen.prompt_note(on)
    )
    monkeypatch.setattr(imagegen, "_last_catalog", imagegen.parse_catalog(CATALOG))
    assert "with a reference image in -image, chroma1-flash)" in imagegen.prompt_note(on)


def test_help_mentions_drawing_only_with_the_service(client) -> None:
    assert "**會畫圖**" in client.help_guide() and "會畫圖" in client.help_sheet()
    client.images._config = replace(client.config, image_gen_url="")
    assert "會畫圖" not in client.help_sheet()


# ----- the slash command ---------------------------------------------------------------------


async def test_slash_paints_progress_and_replaces_the_placeholder(service, client) -> None:
    interaction = FakeInteraction()
    await client.image_command(interaction, "夕陽下的貓", aspect=None, count=1, seed=9)
    assert interaction.deferred and interaction.messages == []
    assert any("排隊中，前面還有 2 個" in (e["content"] or "") for e in interaction.edits)
    last = interaction.edits[-1]
    assert last["files"] == ["image-0.png"] and last["view"] is None
    assert last["content"].startswith("**畫**：\n> 夕陽下的貓") and "seed 42" in last["content"]
    assert last["content"].endswith("· enhance local"), "what the service used is shown"
    body = service.posts()[0]
    assert body["max_bytes"] == 9_000_000 and body["count"] == 1 and body["seed"] == 9
    assert not list((client.config.attachment_dir).glob("req-*")), "delivery dir removed"


async def test_one_member_can_run_two_slash_jobs(service, client) -> None:
    service.hold = asyncio.Event()
    first, second = FakeInteraction(), FakeInteraction()
    commands = [asyncio.create_task(client.image_command(first, "貓"))]
    try:
        await asyncio.wait_for(service.submitted.get(), 5)
        commands.append(asyncio.create_task(client.image_command(second, "狗")))
        await asyncio.wait_for(service.submitted.get(), 5)
        assert all(not task.done() for task in commands)
        assert [body["prompt"] for body in service.posts()] == ["貓", "狗"]
        assert first.deferred and second.deferred
        assert first.messages == second.messages == []
    finally:
        service.hold.set()
        await asyncio.wait_for(asyncio.gather(*commands), 5)
    assert first.edits[-1]["files"] == second.edits[-1]["files"] == ["image-0.png"]


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


async def test_slash_cancel_while_queued_deletes_the_job(service, client) -> None:
    """A job cancelled while waiting is cancelled on the service too."""
    service.queued_polls = 10_000
    interaction = FakeInteraction()
    command = asyncio.create_task(client.image_command(interaction, "慢慢畫"))
    for _ in range(500):
        if any("排隊中" in (e["content"] or "") for e in interaction.edits):
            break
        await asyncio.sleep(0.01)
    interaction.edits[-1]["view"].task.cancel()
    await asyncio.wait_for(command, 5)
    assert service.deletes() == ["/v1/image/jobs/j-1"]
    assert interaction.edits[-1] == {"content": CANCELLED, "view": None, "files": []}


async def test_slash_queue_full_is_shown(service, client) -> None:
    """A full GPU queue refuses the job with its queue count."""
    service.submit_reply = (429, {"error": "queue_full", "queue": 3})
    interaction = FakeInteraction()
    await client.image_command(interaction, "貓", count=2)
    shown = interaction.edits[-1]["content"]
    assert shown == "⚠️ 地端 GPU 的排隊已滿（已有 3 個在等），請稍後再試。"


async def test_slash_refusals_never_submit_a_job(service, client) -> None:
    """Negative controls: each guard alone keeps the request off the service's queue."""
    outside = FakeInteraction(guild_id=OTHER_GUILD)
    await client.image_command(outside, "貓")
    assert outside.messages and outside.messages[0][1] and service.calls == []

    client.image_settings.update(GUILD, enabled=False)
    off = FakeInteraction()
    await client.image_command(off, "貓")
    assert off.messages == [("這個伺服器沒有開放圖片生成。", True)] and service.calls == []
    client.image_settings.update(GUILD, enabled=True)

    for kwargs in ({"model": "nope"}, {"model": "plain", "detailer": True}):
        refused = FakeInteraction()
        await client.image_command(refused, "貓", **kwargs)
        assert refused.edits[-1]["content"].startswith("⚠️ ") and refused.edits[-1]["view"] is None
    assert service.posts() == [], "unknown model and unsupported option only read the catalog"


async def test_slash_image_to_image_sends_the_inputs_as_base64(service, client) -> None:
    image, mask = FakeAttachment(PNG), FakeAttachment(PNG, "image/png; charset=binary")
    enhance = types.SimpleNamespace(value="off")
    interaction = FakeInteraction()
    await client.image_command(
        interaction, "改成雪景", image=image, strength=0.4, mask=mask, enhance=enhance
    )
    assert interaction.edits[-1]["files"] == ["image-0.png"]
    body = service.posts()[0]
    assert body["init_image"] == _wire(PNG) and body["mask"] == _wire(PNG)
    assert (body["strength"], body["enhance"]) == (0.4, "off")
    assert "split" not in body and "cloud_enhance" not in body and "hires" not in body
    assert image.reads == mask.reads == 1
    assert body["ref_image"] is None and body["ref_strength"] is None


async def test_slash_reference_image_is_sent_alone_or_with_the_image(service, client) -> None:
    ref = FakeAttachment(PNG)
    alone = FakeInteraction()
    await client.image_command(
        alone, "她在海邊散步", model="wai-illustrious", ref_image=ref, ref_strength=0.8
    )
    assert alone.edits[-1]["files"] == ["image-0.png"]
    body = service.posts()[0]
    assert body["ref_image"] == _wire(PNG) and body["ref_strength"] == 0.8
    assert body["init_image"] is None and body["strength"] is None
    assert ref.reads == 1

    image, ref = FakeAttachment(PNG), FakeAttachment(PNG)
    both = FakeInteraction()
    await client.image_command(
        both, "改成雪景", model="wai-illustrious", image=image, strength=0.4, ref_image=ref
    )
    assert both.edits[-1]["files"] == ["image-0.png"]
    body = service.posts()[1]
    assert body["init_image"] == _wire(PNG) and body["ref_image"] == _wire(PNG)
    assert (body["strength"], body["ref_strength"]) == (0.4, None)
    assert image.reads == ref.reads == 1


async def test_slash_input_refusals_never_submit_or_download(service, client) -> None:
    """Negative controls: a mask without an image, an input over the catalog's
    max_input_bytes, a model that takes no input image, an enhance the model does not list and a
    type that is not PNG/JPEG/WebP are refused before the job is submitted, and all but the last
    (which needs the bytes) before the attachment is read."""
    for kwargs, said in (
        ({"mask": FakeAttachment(PNG)}, "「遮罩」 要搭配 「改這張圖」"),
        ({"strength": 0.5}, "「改動幅度」 要搭配 「改這張圖」"),
        ({"image": FakeAttachment(PNG, size=5000)}, "「改這張圖」 太大"),
        ({"image": FakeAttachment(PNG), "mask": FakeAttachment(PNG, size=5000)}, "「遮罩」 太大"),
        ({"image": FakeAttachment(PNG), "model": "plain"}, "不支援 「改這張圖」"),
        ({"image": FakeAttachment(GIF, "image/gif")}, "只能是 PNG、JPEG 或 WebP"),
        ({"enhance": types.SimpleNamespace(value="cloud")}, "enhance 只能是 local, off"),
        ({"ref_image": FakeAttachment(PNG)}, "不支援 「照這個角色」"),
        ({"model": "wai-illustrious", "ref_strength": 0.5}, "「角色相似度」 要搭配 「照這個角色」"),
        (
            {"model": "wai-illustrious", "ref_image": FakeAttachment(PNG, size=5000)},
            "「照這個角色」 太大",
        ),
        (
            {
                "model": "wai-illustrious",
                "image": FakeAttachment(PNG),
                "ref_image": FakeAttachment(PNG, size=5000),
            },
            "「照這個角色」 太大",
        ),
    ):
        refused = FakeInteraction()
        await client.image_command(refused, "貓", **kwargs)
        assert said in refused.edits[-1]["content"], kwargs
        attached = [v for v in kwargs.values() if isinstance(v, FakeAttachment)]
        assert all(a.reads == 0 for a in attached), f"downloaded for nothing: {kwargs}"
    # A file named and typed as PNG whose bytes are a GIF: read, then refused, never submitted.
    disguised = FakeAttachment(GIF, "image/png")
    refused = FakeInteraction()
    await client.image_command(refused, "貓", image=disguised)
    assert "只能是 PNG、JPEG 或 WebP" in refused.edits[-1]["content"] and disguised.reads == 1
    assert service.posts() == []


async def test_an_image_without_a_content_type_is_typed_by_its_name_then_its_bytes(
    service, client
) -> None:
    """Discord may omit an attachment's content type: the file name stands in until the bytes
    are read. Negative control: a nameless .bin of GIF bytes is still refused unread."""
    interaction = FakeInteraction()
    untyped = FakeAttachment(PNG, None, filename="photo.PNG")
    await client.image_command(interaction, "貓", image=untyped)
    assert interaction.edits[-1]["files"] == ["image-0.png"] and untyped.reads == 1
    assert service.posts()[0]["init_image"] == _wire(PNG)
    unknown = FakeAttachment(GIF, None, filename="file.bin")
    refused = FakeInteraction()
    await client.image_command(refused, "貓", image=unknown)
    assert "只能是 PNG、JPEG 或 WebP" in refused.edits[-1]["content"] and unknown.reads == 0
    assert len(service.posts()) == 1


async def test_downloads_share_what_is_left_of_the_run_timeout(
    service, client, tmp_path, fake_time, monkeypatch
) -> None:
    """A job done just before the run timeout cannot spend another full download timeout per
    image: the download gets the time left, and none left is a timeout that cancels the job."""
    limits = []
    real = imagegen.ImageService.download

    async def download(self, session, job_id, image, max_bytes, limit=120):
        limits.append(limit)
        return await real(self, session, job_id, image, max_bytes, limit)

    async def run(run_seconds, into):
        client.images._config = replace(
            client.config, image_gen_timeout_seconds=run_seconds, image_gen_queue_wait_seconds=1000
        )
        # Generating from poll 1 (t+100), done on poll 2 (t+200): 100 s of the run used.
        service.polls, service.queued_polls, service.hold = 0, 0, asyncio.Event()

        async def on_progress(job):
            if job.state == "generating":
                service.hold.set()

        return await imagegen.run_job(
            client.images,
            ImageRequest("貓"),
            tmp_path / into,
            8_000_000,
            GUILD,
            USER,
            on_progress,
            clock=lambda: fake_time["t"],
        )

    monkeypatch.setattr(imagegen.ImageService, "download", download)
    await run(250, "a")
    assert limits == [pytest.approx(150.0)] and service.deletes() == []
    limits.clear()
    with pytest.raises(ImageGenError) as caught:
        await run(100, "b")
    assert caught.value.code == "timeout" and limits == []
    assert service.deletes() == ["/v1/image/jobs/j-1"]


async def test_slash_uses_the_service_default_model(service, client) -> None:
    service.catalog = {**CATALOG, "default_model": "plain"}
    assert client.images.cached() is None
    interaction = FakeInteraction()
    await client.image_command(interaction, "貓")
    assert service.posts()[0]["model"] == "plain"
    assert service.posts()[0]["count"] is None, "the service supplies the model's default count"
    assert interaction.edits[-1]["files"] == ["image-0.png"]


def test_ref_image_without_model_uses_default_ref_model() -> None:
    edit = {"id": "edit", "name": "edit", "options": ["seed", "ref_image"], "defaults": {}}
    catalog = imagegen.parse_catalog(
        {**CATALOG, "models": [*CATALOG["models"], edit], "default_ref_model": "edit"}
    )
    small = imagegen.InputImage("image/png", 10)
    ref = ImageRequest("貓", ref_image=small)
    assert imagegen.resolve(catalog, ref).id == "edit"
    assert imagegen.resolve(catalog, ImageRequest("貓")).id == "chroma1-flash"
    assert imagegen.resolve(catalog, replace(ref, model="wai-illustrious")).id == "wai-illustrious"
    # init_image or ref_strength is not an edit option: the request stays on default_model.
    for request in (replace(ref, init_image=small), replace(ref, ref_strength=0.5)):
        assert (
            imagegen.resolve(catalog, replace(request, model="wai-illustrious")).id
            == "wai-illustrious"
        )
        with pytest.raises(ImageGenError) as caught:
            imagegen.resolve(catalog, request)
        assert caught.value.code == "unsupported_option" and "chroma1-flash" in str(caught.value)
    # An off value sets nothing, as the service counts: the request stays on the edit model.
    for off in (
        replace(ref, upscale="off"),
        replace(ref, detailer=False),
    ):
        assert off.options() == ["ref_image"] and imagegen.resolve(catalog, off).id == "edit"
    assert replace(ref, upscale="2x").options() == ["ref_image", "upscale"]
    # A service without the key: ref_image requests run on default_model.
    assert imagegen.parse_catalog(CATALOG).default_ref_model == ""
    with pytest.raises(ImageGenError) as caught:
        imagegen.resolve(imagegen.parse_catalog(CATALOG), ref)
    assert caught.value.code == "unsupported_option"


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
    result = await client._answer("用地端幫我畫一隻橘貓", [], GUILD, USER, channel_id=ALLOWED)
    assert result.text == f"好，馬上畫。\n\n{IMAGE_STARTED}"
    await _delivered(client)
    sent = channel.sent[0]
    assert sent["files"] == ["image-0.png"] and sent["text"].startswith(f"<@{USER}> **畫**")
    assert sent["mentions"].to_dict() == discord.AllowedMentions.none().to_dict()
    assert service.posts()[0]["aspect"] == "3:4"


async def test_two_image_tags_start_two_jobs(service, client, monkeypatch) -> None:
    channel = FakeChannel()
    monkeypatch.setattr(client, "get_channel", {ALLOWED: channel}.get)
    service.hold = asyncio.Event()
    _codex_replies(monkeypatch, '<image_gen prompt="貓"/>\n<image_gen prompt="狗"/>')
    try:
        result = await client._answer("用地端畫貓和狗", [], GUILD, USER, channel_id=ALLOWED)
        await asyncio.wait_for(asyncio.gather(service.submitted.get(), service.submitted.get()), 5)
        assert result.text.count(IMAGE_STARTED) == 2
        assert sorted(body["prompt"] for body in service.posts()) == ["狗", "貓"]
        assert len(client._image_tasks) == 2 and channel.sent == []
    finally:
        service.hold.set()
        await _delivered(client)
    assert len(channel.sent) == 2
    assert all(sent["files"] == ["image-0.png"] for sent in channel.sent)


async def test_tags_past_the_gpu_queue_are_not_run(service, client, monkeypatch) -> None:
    """Negative control: an answer with more tags than the GPU queue holds starts that many
    jobs and says how many were left out, instead of one refused request and failure post each."""
    channel = FakeChannel()
    monkeypatch.setattr(client, "get_channel", {ALLOWED: channel}.get)
    tags = "\n".join(f'<image_gen prompt="貓{i}"/>' for i in range(6))
    _codex_replies(monkeypatch, tags)
    result = await client._answer("用地端畫六隻貓", [], GUILD, USER, channel_id=ALLOWED)
    assert result.text.count(IMAGE_STARTED) == bot_module.IMAGE_TAGS_RUN == 4
    assert "多的 2 個沒有執行" in result.text
    await _delivered(client)
    assert len(service.posts()) == 4


async def test_images_are_spoilered_by_the_guild_mode(service, client, monkeypatch) -> None:
    """auto hides only what the service marked sensitive, always hides everything, off hides
    nothing; the slash command and the tag both follow it. Negative control: a sensitive job
    under auto is posted as SPOILER_ (Discord's spoiler), a plain one is not."""
    for mode, sensitive, name in (
        ("auto", True, "SPOILER_image-0.png"),
        ("auto", False, "image-0.png"),
        ("always", False, "SPOILER_image-0.png"),
        ("off", True, "image-0.png"),
    ):
        client.image_settings.update(GUILD, spoiler=mode)
        service.sensitive, service.polls = sensitive, 0
        interaction = FakeInteraction()
        await client.image_command(interaction, "貓")
        assert interaction.edits[-1]["files"] == [name], (mode, sensitive)
    assert client.image_settings.spoiler(GUILD) == "off"
    client.image_settings.update(GUILD, spoiler="auto")
    service.sensitive, service.polls = True, 0
    channel = FakeChannel()
    monkeypatch.setattr(client, "get_channel", {ALLOWED: channel}.get)
    _codex_replies(monkeypatch, '<image_gen prompt="一隻橘貓"/>')
    await client._answer("地端畫", [], GUILD, USER, channel_id=ALLOWED)
    await _delivered(client)
    assert channel.sent[0]["files"] == ["SPOILER_image-0.png"]
    with pytest.raises(ValueError):
        client.image_settings.update(GUILD, spoiler="sometimes")


async def test_tag_delivery_rechecks_the_channel(service, client, monkeypatch) -> None:
    channel = FakeChannel()
    monkeypatch.setattr(client, "get_channel", {ALLOWED: channel}.get)
    service.hold = asyncio.Event()
    _codex_replies(monkeypatch, '<image_gen prompt="貓"/>')
    await client._answer("地端畫貓", [], GUILD, USER, channel_id=ALLOWED)
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
    result = await client._answer("用地端 dalle 畫貓", [], GUILD, USER, channel_id=ALLOWED)
    assert "<TAG_ERRORS>" in prompts[1] and "unknown model" in prompts[1] and "plain" in prompts[1]
    assert "沒有「dalle」這個圖片模型" in result.text and result.text.endswith(IMAGE_STARTED)
    await _delivered(client)
    assert [body["model"] for body in service.posts()] == ["plain"]


async def test_tag_enhance_is_checked_against_the_model_and_sent(service, client, monkeypatch):
    channel = FakeChannel()
    monkeypatch.setattr(client, "get_channel", {ALLOWED: channel}.get)
    prompts = _codex_replies(
        monkeypatch,
        '<image_gen prompt="貓" enhance="cloud"/>',
        '<image_gen prompt="貓" enhance="off"/>',
    )
    result = await client._answer("地端畫貓，不要改寫", [], GUILD, USER, channel_id=ALLOWED)
    assert "enhance must be one of local, off" in prompts[1]
    assert result.text.endswith(IMAGE_STARTED)
    await _delivered(client)
    assert [body["enhance"] for body in service.posts()] == ["off"]


async def test_tag_refusals_never_submit_a_job(service, client, monkeypatch) -> None:
    """Negative controls for the tag: shown (code/quote) tags, an unknown model, an unsupported
    option, a disabled guild and a place the Bot does not serve."""
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
    )
    shown = await client._answer("怎麼請你畫圖", [], GUILD, USER, channel_id=ALLOWED)
    assert '<image_gen prompt="貓"/>' in shown.text and service.calls == []
    unknown = await client._answer("地端畫", [], GUILD, USER, channel_id=ALLOWED)
    unsupported = await client._answer("地端畫", [], GUILD, USER, channel_id=ALLOWED)
    assert "圖片沒有生成" in unknown.text and "不支援 negative" in unsupported.text
    client.image_settings.update(GUILD, enabled=False)
    off = await client._answer("地端畫", [], GUILD, USER, channel_id=ALLOWED)
    assert "這個伺服器沒有開放圖片生成" in off.text
    client.image_settings.update(GUILD, enabled=True)
    outside = await client._answer("地端畫", [], OTHER_GUILD, USER, channel_id=ALLOWED)
    assert "圖片沒有生成" in outside.text
    assert client._image_tasks == set() and service.posts() == []


async def test_tag_runs_only_when_the_member_asks_for_the_local_service(
    service, client, monkeypatch
) -> None:
    """Negative control: a plain "draw a cat" does not put a job on the shared GPU even when the
    model emits the tag; naming the local service or one of its models does, and so does a
    follow-up in a conversation where the member already asked for it."""
    _codex_replies(monkeypatch, *['<image_gen prompt="貓"/>'] * 6)
    plain = await client._answer("畫一隻貓", [], GUILD, USER, channel_id=ALLOWED)
    assert "沒有用地端生圖" in plain.text and "/codex-image" in plain.text
    assert service.posts() == []
    named = await client._answer("用 Chroma1 Flash 畫貓", [], GUILD, USER, channel_id=ALLOWED)
    assert named.text.endswith(IMAGE_STARTED)
    await _delivered(client)
    # The fake model answers in one conversation ("t"): the follow-up needs no repeat.
    again = await client._answer("改成 16:9 再畫一張", [], GUILD, USER, channel_id=ALLOWED)
    assert again.text.endswith(IMAGE_STARTED)
    await _delivered(client)
    client._local_image_threads.clear()  # a new conversation
    local = await client._answer("本機畫一隻貓", [], GUILD, USER, channel_id=ALLOWED)
    assert local.text.endswith(IMAGE_STARTED)
    await _delivered(client)
    assert len(service.posts()) == 3
    client._local_image_threads.clear()
    # A replied-to message saying 本地 is not the member asking; "explain" is not model "plain".
    quoted = bot_module.with_quoted_message("畫這個", "別人", "今天的本地新聞", 0)
    replied = await client._answer(quoted, [], GUILD, USER, channel_id=ALLOWED)
    assert "沒有用地端生圖" in replied.text and len(service.posts()) == 3
    catalog = imagegen.parse_catalog(CATALOG)
    assert not imagegen.asked_for_local("explain this drawing", catalog)
    assert imagegen.asked_for_local("用 plain 畫", catalog)
    assert imagegen.asked_for_local("用地端sdxl畫", catalog), "CJK words need no word gap"
    assert "earlier in this conversation" in imagegen.prompt_note(client.config)
    # A server with images off still says so, rather than asking for 地端 first.
    client.image_settings.update(GUILD, enabled=False)
    closed = await client._answer("畫一隻貓", [], GUILD, USER, channel_id=ALLOWED)
    assert "這個伺服器沒有開放圖片生成" in closed.text


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
    assert document["result"] | {"text": ""} == {
        "text": "",
        "enabled": True,
        "service": True,
        "spoiler": "auto",
    }
    assert document["result"]["text"] == (
        "圖片生成：開\n暴雷隱藏：auto（服務依提示詞判定為成人內容的圖才隱藏）"
    )
    status, document = await run("image.set", guild=GUILD, state="off", spoiler="always")
    assert document["result"] | {"text": ""} == {
        "text": "",
        "enabled": False,
        "service": True,
        "spoiler": "always",
    }
    assert not client.image_settings.enabled(GUILD)
    status, document = await run("image.get", guild=GUILD)
    assert document["result"] | {"text": ""} == {
        "text": "",
        "enabled": False,
        "service": True,
        "spoiler": "always",
    }
    status, document = await run("image.set", guild=GUILD)
    assert status == "rejected"
    status, document = await run("image.set", guild=OTHER_GUILD, state="off")
    assert status == "rejected" and client.image_settings.enabled(OTHER_GUILD)


def test_help_sheet_lists_the_options_by_their_discord_names(client) -> None:
    """HELP for the model names the attachment options as Discord shows them (rename)."""
    line = next(
        row for row in client.help_sheet().splitlines() if "-image" in row and "參數" in row
    )
    assert "照這個角色" in line and "改這張圖" in line and "角色相似度" in line
    assert "ref_image" not in line and "ref_strength" not in line


def test_help_explains_both_kinds_of_reference_image() -> None:
    from discord_codex_bot.help import COMMAND_GUIDE

    text, examples = COMMAND_GUIDE["-image"]
    assert "目前只有 WAI" not in text
    assert "編輯模型" in text and "不支援「角色相似度」" in text
    assert any(
        "model:qwen-image-2.1-turbo" in e and "照這個角色:" in e and "角色相似度" not in e
        for e in examples
    ), "an edit-model example without ref_strength"
    assert any("model:wai-illustrious" in e and "角色相似度:" in e for e in examples)


# ----- /image character: (characters.py) -----------------------------------------------------

CHARACTER_CATALOG = {
    **CATALOG,
    "models": [
        *CATALOG["models"][:2],
        {
            "id": "wai-illustrious",
            "name": "WAI Illustrious",
            "options": ["seed", "init_image", "ref_image", "ref_strength", "character", "lora"],
            "knowledge": {"cutoff": "2024-03-01", "min_posts": 500},
            "lora_base": "Illustrious",
        },
        {"id": "qwen-image-edit-2511", "name": "Qwen Edit", "options": ["ref_image"]},
    ],
    "default_model": "wai-illustrious",
    "default_ref_model": "qwen-image-edit-2511",
}
PEKORA_LORA = {
    "id": 1,
    "name": "Pekora",
    "modelVersions": [
        {
            "id": 10,
            "baseModel": "Illustrious",
            "trainedWords": ["usada pekora", "rabbit ears"],
            "images": [{"url": "https://image.civitai.com/x/1.png", "nsfwLevel": 1}],
        }
    ],
}


class CharacterWeb:
    """Danbooru and Civitai: every tag asked about is a character with a solo sample picture
    and rabbit ears; `posts_before` is its count before any cutoff."""

    def __init__(self, posts_before: int = 760, loras=()) -> None:
        self.posts_before = posts_before
        self.loras = list(loras)
        self.urls: list[str] = []

    async def json(self, url, params):
        self.urls.append(url)
        if url.endswith("/tags.json"):
            names = params["search[name_comma]"].split(",")
            return [{"name": n, "category": 4, "post_count": 1000} for n in names]
        if url.endswith("/counts/posts.json"):
            return {"counts": {"posts": self.posts_before}}
        if url.endswith("/posts.json"):
            tag = params["tags"].split()[0]
            url = f"https://cdn.donmai.us/{tag}.png"
            return [{"file_ext": "png", "large_file_url": url, "tag_string_general": "solo"}]
        if url.endswith("/related_tag.json"):
            return {"related_tags": [{"tag": {"name": "rabbit_ears"}, "frequency": 0.9}]}
        if url.endswith("/models"):
            return {"items": self.loras}
        raise ValueError(url)

    async def image(self, url, max_bytes):
        self.urls.append(url)
        return PNG


def _judge(monkeypatch, *answers) -> list[dict]:
    """The Bot's judge turns (run_batch): each prompt and its arguments, answered in order."""
    calls: list[dict] = []
    queue = [json.dumps(a) for a in answers]

    async def batch(prompt, config, **kw):
        calls.append({"prompt": prompt, **kw})
        return queue.pop(0)

    monkeypatch.setattr(bot_module, "run_batch", batch)
    return calls


async def _nothing() -> None:
    return None


class CharacterInteraction(FakeInteraction):
    """A slash interaction whose member answers the private character question with `answers`
    in order: "yes", "next", "cancel", or None to let it time out."""

    def __init__(self, *answers) -> None:
        super().__init__()
        self.answers = list(answers)
        self.questions: list[tuple[str, str]] = []
        self.private: list[dict] = []  # edits of the private message
        self.presses: list[asyncio.Future] = []
        outer = self

        class Message:
            async def edit(self, content=None, view=None, attachments=None):
                files = None if attachments is None else [f.filename for f in attachments]
                outer.private.append({"content": content, "view": view, "files": files})
                if view is not None:
                    outer.ask(content, attachments[0], view)

        class Followup:
            async def send(self, text, ephemeral=False, file=None, view=None, wait=False):
                outer.followups.append(text)
                assert ephemeral and wait, "the question is private and edited later"
                outer.ask(text, file, view)
                return Message()

        self.followup = Followup()

    def ask(self, text, file, view) -> None:
        self.questions.append((text, file.filename))
        answer = self.answers.pop(0)
        press = types.SimpleNamespace(user=self.user, response=types.SimpleNamespace())
        press.response.defer = _nothing

        async def respond() -> None:
            if answer is None:
                view.stop()  # what a time-out does
                return
            await {"yes": view.yes, "next": view.next, "cancel": view.cancel}[answer].callback(
                press
            )

        self.presses.append(asyncio.ensure_future(respond()))


def _shown(interaction) -> list[str]:
    return [e["content"] or "" for e in interaction.edits]


async def test_character_known_by_the_default_model_is_sent_with_its_traits(
    service, client, monkeypatch
) -> None:
    service.catalog = CHARACTER_CATALOG
    calls = _judge(monkeypatch, {"tags": ["usada_pekora"]})
    client.character_web = CharacterWeb(posts_before=760)
    interaction = CharacterInteraction("yes")
    await client.image_command(interaction, "在海邊散步", character="兔田佩克拉")
    body = service.posts()[0]
    assert body["model"] == "wai-illustrious" and body["prompt"] == "在海邊散步"
    assert body["character"] == {"tag": "usada_pekora", "traits": ["rabbit ears"]}
    assert body["lora"] is None and body["ref_image"] is None
    # Only the name went to the cloud judge, isolated, Codex first and then Argon.
    (judge,) = calls
    assert "兔田佩克拉" in judge["prompt"] and "海邊" not in judge["prompt"]
    assert judge["spare"] == characters.JUDGE_SPARE and judge["isolated"] is True
    assert judge["spare"][0] == "gemini-4-argon-medium"
    assert judge["schema"] == client.config.character_schema_path
    assert judge["web_search"] is True, "the name judge may search the web for the name"
    question = "是這個角色嗎？`usada_pekora`（Danbooru 上 1000 張圖）"
    assert interaction.questions == [(question, "character.png")]
    assert interaction.private[-1] == {
        "content": "✅ 角色：`usada_pekora`",
        "view": None,
        "files": None,
    }, "the confirmed picture stays, the buttons go"
    assert any("用認得這個角色的 wai-illustrious\n🎨 排隊中" in t for t in _shown(interaction))
    assert interaction.edits[-1]["files"] == ["image-0.png"]
    assert all(
        url.split("/")[2] in ("danbooru.donmai.us", "cdn.donmai.us")
        for url in (client.character_web.urls)
    )


async def test_character_next_shows_the_following_candidate(service, client, monkeypatch):
    service.catalog = CHARACTER_CATALOG
    _judge(monkeypatch, {"tags": ["pekora_(cat)", "usada_pekora"]})
    client.character_web = CharacterWeb()
    interaction, image = CharacterInteraction("next", "yes"), FakeAttachment(PNG)
    await client.image_command(interaction, "在海邊散步", character="佩克拉", image=image)
    assert [q[0].split("`")[1] for q in interaction.questions] == ["pekora_(cat)", "usada_pekora"]
    body = service.posts()[0]
    assert body["character"]["tag"] == "usada_pekora"
    assert body["init_image"] == _wire(PNG) and image.reads == 1, "the member's image goes along"


@pytest.mark.parametrize("answers", [("cancel",), (None,), ("next", "cancel")])
async def test_character_cancelled_or_unanswered_submits_nothing(
    service, client, monkeypatch, answers
) -> None:
    service.catalog = CHARACTER_CATALOG
    _judge(monkeypatch, {"tags": ["pekora_(cat)", "usada_pekora"]})
    client.character_web = CharacterWeb()
    interaction = CharacterInteraction(*answers)
    await client.image_command(interaction, "在海邊散步", character="佩克拉")
    assert service.posts() == []
    assert interaction.edits[-1]["content"] == CANCELLED
    assert interaction.private[-1]["content"] == CANCELLED
    assert interaction.private[-1]["files"] == [] and interaction.private[-1]["view"] is None


@pytest.mark.parametrize(
    ("tags", "answers"),
    [([], ()), (["../not a tag"], ()), (["usada_pekora"], ("next",))],
)
async def test_character_not_found_suggests_a_reference_image(
    service, client, monkeypatch, tags, answers
) -> None:
    service.catalog = CHARACTER_CATALOG
    _judge(monkeypatch, {"tags": tags})
    client.character_web = CharacterWeb()
    interaction = CharacterInteraction(*answers)
    await client.image_command(interaction, "在海邊散步", character="誰")
    assert service.posts() == []
    shown = interaction.edits[-1]["content"]
    assert shown.startswith("⚠️ 找不到「誰」這個角色") and "照這個角色" in shown


async def test_character_unknown_everywhere_takes_a_lora_then_the_reference_model(
    service, client, monkeypatch
) -> None:
    service.catalog = CHARACTER_CATALOG
    service.lora_fails = True
    calls = _judge(monkeypatch, {"tags": ["isaki_riona"]}, {"pick": 2})
    client.character_web = CharacterWeb(posts_before=0, loras=[PEKORA_LORA])
    interaction = CharacterInteraction("yes")
    await client.image_command(interaction, "在海邊散步", character="isaki riona")
    first, second = service.posts()
    assert first["model"] == "wai-illustrious" and first["character"]["tag"] == "isaki_riona"
    assert first["lora"] == {
        "version_id": 10,
        "weight": None,
        "trigger": "usada pekora, rabbit ears",
    }
    # The service could not get the LoRA: the next way, the sample as the reference image.
    assert second["model"] == "qwen-image-edit-2511" and second["ref_image"] == _wire(PNG)
    assert second["character"] is None and second["lora"] is None
    assert service.deletes() == [], "the service ended the failed job itself"
    shown = _shown(interaction)
    assert any("加上 LoRA「Pekora」" in t for t in shown)
    assert any("改用 qwen-image-edit-2511 照範例圖畫" in t for t in shown)
    assert interaction.edits[-1]["files"] == ["image-0.png"]
    lora_turn = calls[1]
    assert lora_turn["schema"] == client.config.lora_schema_path
    assert not lora_turn.get("web_search"), "comparing pictures needs no web search"
    assert [p.name for p in lora_turn["images"]] == ["reference.png", "lora-2.png"]
    assert "海邊" not in lora_turn["prompt"]


async def test_character_refusals_never_ask_the_judge(service, client, monkeypatch) -> None:
    service.catalog = CHARACTER_CATALOG
    calls = _judge(monkeypatch)
    client.character_web = CharacterWeb()
    cases = [
        ({"ref_image": FakeAttachment(PNG)}, bot_module.CHARACTER_WITH_REF),
        ({"character": "x" * 101}, "character 最多 100 字。"),
    ]
    for kwargs, message in cases:
        refused = CharacterInteraction()
        await client.image_command(refused, "貓", **{"character": "佩克拉", **kwargs})
        assert refused.messages == [(message, True)]
    unknown = CharacterInteraction()
    await client.image_command(unknown, "貓", character="佩克拉", model="nope")
    assert unknown.edits[-1]["content"].startswith("⚠️ 沒有「nope」這個圖片模型")
    assert calls == [] and service.posts() == [] and client.character_web.urls == []


async def test_character_buttons_answer_only_the_requester() -> None:
    from discord_codex_bot.ui import NOT_YOURS, CharacterView

    view = CharacterView(USER, timeout=5)
    sent: list[tuple[str, bool]] = []

    async def send_message(text, ephemeral=False):
        sent.append((text, ephemeral))

    other = types.SimpleNamespace(
        user=types.SimpleNamespace(id=USER + 1),
        response=types.SimpleNamespace(send_message=send_message),
    )
    assert await view.interaction_check(other) is False
    assert sent == [(NOT_YOURS, True)] and view.answer is None
    labels = [item.label for item in view.children]
    assert labels == ["是這個角色", "不是，看下一個", "取消"]


def test_chat_and_help_point_named_characters_to_the_command(config) -> None:
    note = imagegen.prompt_note(replace(config, image_gen_url="http://img:18082"))
    assert "character" in note and "Never put the member's scene into any cloud call" in note
    text, examples = help_module.COMMAND_GUIDE["-image"]
    assert "character" in text and "照這個角色" in text
    assert any("character:" in e for e in examples)
