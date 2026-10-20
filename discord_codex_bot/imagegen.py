"""Image generation through the image service on the host (docs/image-service.md).

The service owns the model catalog, the prompt pipeline and the GPU queue; this module speaks
its HTTP contract and nothing more: read the catalog, check a request against it, submit, poll,
download, cancel. `run_job` runs one job end to end and is the only way the Bot generates an
image: the slash command and the model's <image_gen/> tag both go through it, so both are
checked against the same catalog. Who may generate and how many (the per-guild switch, the daily
and in-flight limits) is ImageSettings, kept in the guild-settings database next to linkclean's
switches; the Bot applies it before a job starts.
"""

from __future__ import annotations

import asyncio
import base64
import contextlib
import json
import logging
import mimetypes
import re
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field, fields
from datetime import datetime, timedelta, timezone
from pathlib import Path

import aiohttp

from . import tags
from .clock import sleep_for
from .config import Config

LOGGER = logging.getLogger(__name__)
TAIPEI = timezone(timedelta(hours=8))

# A model added or removed on the service shows up within this long.
CATALOG_TTL_SECONDS = 60.0
REQUEST_TIMEOUT_SECONDS = 30
DOWNLOAD_TIMEOUT_SECONDS = 120
POLL_SECONDS = 2.0
# Polls that may fail in a row (the relay dropping a connection) before the job is given up.
POLL_FAILURES = 3
# The slash command's choices, from the contract; the catalog is still checked at run time.
ASPECTS = ("1:1", "3:4", "4:3", "9:16", "16:9")
UPSCALES = ("off", "2x", "4x")
MAX_COUNT = 4
MAX_SEED = 2**32 - 1
# Request fields a member may set; each must be in the model's `options` when set.
OPTIONS = (
    "negative",
    "aspect",
    "count",
    "seed",
    "enhance",
    "init_image",
    "strength",
    "mask",
    "upscale",
    "detailer",
)
# How the prompt is rewritten before generation; a model lists the ones it takes (`enhancers`).
ENHANCERS = ("local", "cloud", "off")
TERMINAL = frozenset({"done", "failed", "cancelled"})
_JOB_ID = re.compile(r"^[A-Za-z0-9._-]{1,100}$")
_SUFFIXES = {"image/png": ".png", "image/jpeg": ".jpg", "image/webp": ".webp"}
# An input image's type, from its first bytes (what the service decodes, whatever the name says).
_MAGIC = ((b"\x89PNG\r\n\x1a\n", "image/png"), (b"\xff\xd8\xff", "image/jpeg"))
# The host's one GPU queue, shared with the local model: one running, at most this many waiting.
QUEUE_LIMIT = 3

UNAVAILABLE = "圖片生成服務現在不能用，請稍後再試。"
# Also the local model's answer to the same refusal (openrouter.py): it is one queue.
QUEUE_FULL = "地端 GPU 的排隊已滿（已有 {waiting} 個在等），請稍後再試。"
QUEUE_TIMED_OUT = "地端 GPU 排隊太久，已取消，請稍後再試。"
FAILED = "圖片生成失敗，請換個描述或稍後再試。"
TIMED_OUT = "圖片生成超過時限，已取消。"
STOPPED = "圖片生成服務取消了這個工作。"


def queue_full(waiting: object = None) -> str:
    """The member's message for a full GPU queue; `waiting` is the service's count when it gave
    one."""
    count = waiting if isinstance(waiting, int) and waiting > 0 else QUEUE_LIMIT
    return QUEUE_FULL.format(waiting=count)


# The model's tool (codex._prompt): one per answer, attributes in any order.
IMAGE_TAG = re.compile(r'<image_gen((?:\s+[a-z_]{1,10}="[^"]{0,2000}")+)\s*/?>(?:\s*</image_gen>)?')
_ATTR = re.compile(r'([a-z_]{1,10})="([^"]{0,2000})"')
TAG_ATTRS = ("prompt", "model", "aspect", "count", "seed", "negative", "enhance")
MAX_IMAGE_TAGS = 1
# The last catalog any ImageService read, for the prompt (`prompt_note`), which the backends
# build without the client.
_last_catalog: Catalog | None = None


class ImageGenError(Exception):
    """A job that cannot run or did not finish. str() is the member-facing message; `detail`
    says the same for the model, and `fixable` marks a request the asker can correct (a model,
    an option, a value), which the tag path sends back to the model."""

    def __init__(self, message: str, code: str, fixable: bool = False, detail: str = "") -> None:
        super().__init__(message)
        self.code = code
        self.fixable = fixable
        self.detail = detail or code


@dataclass(frozen=True, slots=True)
class ImageModel:
    id: str
    name: str
    options: frozenset[str]
    defaults: dict = field(default_factory=dict)
    enhancers: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class Catalog:
    models: tuple[ImageModel, ...]
    default_model: str
    aspects: tuple[str, ...]
    upscale: tuple[str, ...]
    max_count: int
    max_input_bytes: int = 0  # 0: the service takes no input image

    def get(self, model_id: str) -> ImageModel | None:
        return next((m for m in self.models if m.id == model_id), None)


@dataclass(frozen=True, slots=True)
class InputImage:
    """An image a member gave the job (init_image, mask): its type and size are known from the
    attachment before it is read, so a request can be checked before anything is downloaded;
    `data` holds the bytes once read, and `mime` is then what they are (`input_image`)."""

    mime: str
    size: int
    data: bytes = b""

    def wire(self) -> dict:
        return {"mime": self.mime, "data": base64.b64encode(self.data).decode("ascii")}


@dataclass(frozen=True, slots=True)
class ImageRequest:
    """What a member asked for. None is the model's default and is sent as null."""

    prompt: str
    model: str = ""
    negative: str | None = None
    aspect: str | None = None
    count: int | None = None
    seed: int | None = None
    enhance: str | None = None
    init_image: InputImage | None = None
    strength: float | None = None
    mask: InputImage | None = None
    upscale: str | None = None
    detailer: bool | None = None

    def options(self) -> list[str]:
        return [name for name in OPTIONS if getattr(self, name) is not None]


@dataclass(frozen=True, slots=True)
class Image:
    index: int
    bytes: int
    mime: str
    seed: int | None


@dataclass(frozen=True, slots=True)
class Job:
    job_id: str
    state: str
    position: int = 0  # items ahead in the shared GPU queue
    step: int = 0
    steps: int = 0
    images: tuple[Image, ...] = ()
    queued_s: float = 0.0
    enhance_used: str = ""
    sensitive: bool = False


@dataclass(frozen=True, slots=True)
class Result:
    model: str
    paths: tuple[Path, ...]
    seeds: tuple[int | None, ...]
    enhance_used: str = ""
    sensitive: bool = False  # the service judged the request sexual (from its words)


def spoilered(result: Result, mode: str) -> bool:
    """Whether `result`'s images are posted behind a spoiler under a guild's `mode`: `always`,
    `off`, or `auto` (only the ones the service marked sensitive)."""
    return mode == "always" or (mode == "auto" and result.sensitive)


def parse_catalog(payload: object) -> Catalog:
    """GET /v1/image/models as a Catalog; ValueError when it is not the documented shape."""
    if not isinstance(payload, dict) or not isinstance(payload.get("models"), list):
        raise ValueError("catalog without a models list")
    models = []
    for item in payload["models"]:
        if not isinstance(item, dict) or not isinstance(item.get("id"), str) or not item["id"]:
            raise ValueError("catalog model without an id")
        options = item.get("options") or []
        defaults = item.get("defaults") or {}
        if not isinstance(options, list) or not isinstance(defaults, dict):
            raise ValueError("catalog model with malformed options or defaults")
        enhancers = item.get("enhancers") or []
        if not isinstance(enhancers, list):
            raise ValueError("catalog model with malformed enhancers")
        name = item.get("name") if isinstance(item.get("name"), str) else item["id"]
        models.append(
            ImageModel(
                item["id"],
                name,
                frozenset(map(str, options)),
                defaults,
                tuple(map(str, enhancers)),
            )
        )
    max_count = payload.get("max_count", 1)
    if not isinstance(max_count, int) or max_count < 1:
        raise ValueError("catalog max_count is not a positive integer")
    max_input = payload.get("max_input_bytes", 0)
    if not isinstance(max_input, int) or max_input < 0:
        raise ValueError("catalog max_input_bytes is not a whole number")
    return Catalog(
        tuple(models),
        str(payload.get("default_model") or (models[0].id if models else "")),
        tuple(map(str, payload.get("aspects") or ())),
        tuple(map(str, payload.get("upscale") or ())),
        max_count,
        max_input,
    )


def _bad(option: str, allowed: str) -> ImageGenError:
    return ImageGenError(
        f"{option} 只能是 {allowed}。", "bad_value", True, f"{option} must be one of {allowed}"
    )


def _needs_image(option: str) -> ImageGenError:
    return ImageGenError(
        f"{option} 要搭配 image（原圖）一起給。",
        "bad_value",
        True,
        f"{option} needs init_image",
    )


def sniff(data: bytes) -> str:
    """The image type of `data` from its first bytes: png, jpeg or webp, else ""."""
    for magic, mime in _MAGIC:
        if data.startswith(magic):
            return mime
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "image/webp"
    return ""


def stated_type(content_type: str | None, filename: str) -> str:
    """The type an attachment states before it is read: Discord's content type, or the one its
    file name suggests when Discord omits it or sends a generic one. The bytes are checked again
    once read (`input_image`)."""
    mime = (content_type or "").split(";")[0].strip().lower()
    if mime not in ("", "application/octet-stream"):
        return mime
    return mimetypes.guess_type(filename)[0] or mime


def input_image(data: bytes) -> InputImage:
    """An input image read in full: its type from its bytes, not from its name."""
    return InputImage(sniff(data), len(data), data)


def _check_input(catalog: Catalog, option: str, image: InputImage) -> None:
    if image.mime not in _SUFFIXES:
        raise ImageGenError(
            f"{option} 只能是 PNG、JPEG 或 WebP 圖片。",
            "bad_value",
            True,
            f"{option} must be png, jpeg or webp",
        )
    if image.size > catalog.max_input_bytes:
        limit = catalog.max_input_bytes / 1_000_000
        raise ImageGenError(
            f"{option} 太大：每張最多 {limit:g} MB。",
            "input_too_large",
            True,
            f"{option} is over {catalog.max_input_bytes} bytes",
        )


def resolve(catalog: Catalog, request: ImageRequest) -> ImageModel:
    """The model `request` runs on, after checking every option it sets against the catalog —
    the checks the service makes too, done here so a refusal costs no queue slot. Input images
    are checked by type and size, which an attachment states before it is read."""
    model = catalog.get(request.model or catalog.default_model)
    if model is None:
        ids = ", ".join(m.id for m in catalog.models) or "（沒有）"
        raise ImageGenError(
            f"沒有「{request.model}」這個圖片模型；可用：{ids}。",
            "unknown_model",
            True,
            f"unknown model; the models are {ids}",
        )
    for option in request.options():
        if option not in model.options:
            raise ImageGenError(
                f"「{model.id}」不支援 {option}。",
                "unsupported_option",
                True,
                f"{model.id} does not take {option}; leave it out",
            )
    if request.aspect is not None and request.aspect not in catalog.aspects:
        raise _bad("aspect", ", ".join(catalog.aspects))
    if request.upscale is not None and request.upscale not in catalog.upscale:
        raise _bad("upscale", ", ".join(catalog.upscale))
    if request.count is not None and not 1 <= request.count <= catalog.max_count:
        raise _bad("count", f"1-{catalog.max_count}")
    if request.enhance is not None and request.enhance not in model.enhancers:
        raise _bad("enhance", ", ".join(model.enhancers) or "（沒有）")
    if request.init_image is None:
        for option in ("mask", "strength"):
            if getattr(request, option) is not None:
                raise _needs_image(option)
    if request.strength is not None and not 0 <= request.strength <= 1:
        raise _bad("strength", "0-1")
    for option in ("init_image", "mask"):
        if (image := getattr(request, option)) is not None:
            _check_input(catalog, option, image)
    return model


def image_count(catalog: Catalog | None, request: ImageRequest) -> int:
    """How many images `request` makes: its count, else its model's default, else one."""
    if request.count is not None:
        return request.count
    model = catalog.get(request.model or catalog.default_model) if catalog else None
    default = model.defaults.get("count") if model else None
    return default if isinstance(default, int) and default > 0 else 1


def _job(payload: object) -> Job:
    if not isinstance(payload, dict):
        raise ValueError("job is not an object")
    job_id = payload.get("job_id")
    if not isinstance(job_id, str) or not _JOB_ID.fullmatch(job_id):
        raise ValueError("job without a usable id")  # it goes into the next request's path
    images = []
    for item in payload.get("images") or ():
        if not isinstance(item, dict) or not isinstance(item.get("index"), int):
            raise ValueError("image without an index")
        seed = item.get("seed")
        images.append(
            Image(
                item["index"],
                int(item.get("bytes") or 0),
                str(item.get("mime") or ""),
                seed if isinstance(seed, int) else None,
            )
        )

    def number(key: str) -> int:
        value = payload.get(key)
        return value if isinstance(value, int) else 0

    queued_s = payload.get("queued_s")
    return Job(
        job_id,
        str(payload.get("state") or ""),
        number("position"),
        number("step"),
        number("steps"),
        tuple(images),
        float(queued_s) if isinstance(queued_s, int | float) else 0.0,
        str(payload.get("enhance_used") or "")[:20],
        payload.get("sensitive") is True,
    )


def _error(status: int, payload: object, model: str) -> ImageGenError:
    """An HTTP refusal as the member reads it. Anything the contract does not name is the
    service being unavailable; the reason goes to the log, not to the member."""
    body = payload if isinstance(payload, dict) else {}
    code = body.get("error")
    if status == 400 and code == "unknown_model":
        return ImageGenError(
            f"沒有「{model}」這個圖片模型。", "unknown_model", True, "unknown model"
        )
    if status == 400 and code == "unsupported_option":
        option = str(body.get("option") or "")[:40]
        return ImageGenError(
            f"「{model}」不支援 {option}。",
            "unsupported_option",
            True,
            f"{model} does not take {option}; leave it out",
        )
    if status == 400 and code == "bad_value":
        option = str(body.get("option") or body.get("field") or "")[:40]
        return ImageGenError(
            f"參數 {option} 的值不對。" if option else "有參數的值不對。",
            "bad_value",
            True,
            f"bad value for {option or 'an option'}",
        )
    if status == 413 and code == "input_too_large":
        limit = body.get("max_input_bytes")
        shown = f"每張最多 {limit / 1_000_000:g} MB" if isinstance(limit, int) else "超過上限"
        return ImageGenError(f"附的圖太大：{shown}。", "input_too_large", True, "input too large")
    if status == 429:
        return ImageGenError(queue_full(body.get("queue")), "queue_full")
    LOGGER.warning("Image service refused: HTTP %s %s", status, str(code)[:40])
    return ImageGenError(UNAVAILABLE, "unavailable")


class ImageService:
    """The service's HTTP API. A request carries the key only when IMAGE_GEN_API_KEY is set, and
    every connection closes after its response: the relay serves one connection per worker and
    must not have one held open between polls."""

    def __init__(self, config: Config) -> None:
        self._config = config
        self._catalog: Catalog | None = None
        self._at = float("-inf")
        self._lock = asyncio.Lock()

    @property
    def enabled(self) -> bool:
        return bool(self._config.image_gen_url)

    @property
    def timeout_seconds(self) -> int:
        """A job's run time, from the first reading that is not `queued`."""
        return self._config.image_gen_timeout_seconds

    @property
    def queue_wait_seconds(self) -> int:
        """How long a job may wait in the GPU queue before it is given up."""
        return self._config.image_gen_queue_wait_seconds

    def _url(self, path: str) -> str:
        return self._config.image_gen_url.rstrip("/") + path

    def session(self, timeout: float = REQUEST_TIMEOUT_SECONDS) -> aiohttp.ClientSession:
        headers = {}
        if key := self._config.image_gen_api_key:
            headers["Authorization"] = f"Bearer {key}"
        return aiohttp.ClientSession(
            timeout=aiohttp.ClientTimeout(total=timeout),
            headers=headers,
            connector=aiohttp.TCPConnector(force_close=True),
        )

    def cached(self) -> Catalog | None:
        """The last catalog read, for a check that cannot wait for the service (a slash
        command's first answer); None before the first read."""
        return self._catalog

    async def catalog(self) -> Catalog:
        """The catalog, read again once it is older than CATALOG_TTL_SECONDS. The last good one
        stands in when a read fails; with none, the service is unavailable."""
        async with self._lock:
            if self._catalog is not None and time.monotonic() - self._at < CATALOG_TTL_SECONDS:
                return self._catalog
            try:
                async with self.session() as session:
                    async with session.get(self._url("/v1/image/models")) as response:
                        if response.status != 200:
                            raise _error(response.status, None, "")
                        self._catalog = parse_catalog(await response.json(content_type=None))
                self._at = time.monotonic()
                global _last_catalog
                _last_catalog = self._catalog
            except (aiohttp.ClientError, TimeoutError, ValueError, ImageGenError) as error:
                LOGGER.warning("Image catalog read failed: %s", type(error).__name__)
                if self._catalog is None:
                    raise ImageGenError(UNAVAILABLE, "unavailable") from None
            return self._catalog

    async def _call(
        self, session: aiohttp.ClientSession, method: str, path: str, model: str = "", **kw
    ) -> dict:
        async with session.request(method, self._url(path), **kw) as response:
            status, text = response.status, await response.text()
        try:
            payload = json.loads(text)
        except ValueError:
            payload = None  # a relay's own error page; the status still says what happened
        if status not in (200, 202):
            raise _error(status, payload, model)
        if not isinstance(payload, dict):
            raise ValueError("response is not an object")
        return payload

    async def submit(
        self,
        session: aiohttp.ClientSession,
        request: ImageRequest,
        model: str,
        max_bytes: int,
        guild_id: int | None,
        user_id: int,
    ) -> Job:
        body = {name.name: getattr(request, name.name) for name in fields(ImageRequest)}
        for name in ("init_image", "mask"):
            if body[name] is not None:
                body[name] = body[name].wire()
        body |= {"model": model, "steps": None, "cfg": None}
        body |= {
            "max_bytes": max_bytes,
            "requester": {"guild": str(guild_id), "user": str(user_id)},
        }
        return _job(await self._call(session, "POST", "/v1/image/jobs", model, json=body))

    async def status(self, session: aiohttp.ClientSession, job_id: str) -> Job:
        return _job(await self._call(session, "GET", f"/v1/image/jobs/{job_id}"))

    async def download(
        self,
        session: aiohttp.ClientSession,
        job_id: str,
        image: Image,
        max_bytes: int,
        limit: float = DOWNLOAD_TIMEOUT_SECONDS,
    ) -> tuple[bytes, str]:
        """One image's bytes and MIME type within `limit` seconds, refused past `max_bytes` (the
        service promises to fit it; a larger file would only fail at Discord)."""
        url = self._url(f"/v1/image/jobs/{job_id}/images/{image.index}")
        timeout = aiohttp.ClientTimeout(total=min(limit, DOWNLOAD_TIMEOUT_SECONDS))
        async with session.get(url, timeout=timeout) as response:
            if response.status != 200:
                raise _error(response.status, None, "")
            mime = response.content_type
            data = bytearray()  # read(n) returns what has arrived so far, not n bytes
            while chunk := await response.content.read(max_bytes + 1 - len(data)):
                data.extend(chunk)
        if len(data) > max_bytes or mime not in _SUFFIXES:
            LOGGER.warning(
                "Image %s/%d unusable: %s, %d bytes", job_id, image.index, mime, len(data)
            )
            raise ImageGenError(FAILED, "failed")
        return bytes(data), mime

    async def cancel(self, job_id: str) -> None:
        async with self.session() as session:
            await self._call(session, "DELETE", f"/v1/image/jobs/{job_id}")


async def run_job(
    service: ImageService,
    request: ImageRequest,
    into: Path,
    max_bytes: int,
    guild_id: int | None,
    user_id: int,
    on_progress: Callable[[Job], Awaitable[None]] | None = None,
    clock: Callable[[], float] = time.time,
) -> Result:
    """Check `request` against the catalog, submit it, poll until it ends (`on_progress` gets
    every reading), and download its images into `into`. Raises ImageGenError. The run timeout
    starts at the first reading that is not `queued`: time in the shared GPU queue is not run
    time, and has its own cap. A job left unfinished for any reason — the asker cancelled
    (CancelledError), a limit passed, a download failed — is cancelled on the service too, so
    it stops holding its place or the GPU."""
    model = resolve(await service.catalog(), request)
    async with service.session() as session:
        try:
            job = await service.submit(session, request, model.id, max_bytes, guild_id, user_id)
        except (aiohttp.ClientError, TimeoutError, ValueError) as error:
            LOGGER.warning("Image submit failed: %s", type(error).__name__)
            raise ImageGenError(UNAVAILABLE, "unavailable") from None
        LOGGER.info(
            "Image job %s guild=%s user=%s model=%s", job.job_id, guild_id, user_id, model.id
        )
        try:
            return await _follow(
                service,
                session,
                job,
                model.id,
                into,
                max_bytes,
                on_progress,
                clock,
            )
        except BaseException as error:
            if not (isinstance(error, ImageGenError) and error.code in ("failed", "stopped")):
                with contextlib.suppress(Exception):
                    await service.cancel(job.job_id)
            raise


async def _follow(
    service: ImageService,
    session: aiohttp.ClientSession,
    job: Job,
    model: str,
    into: Path,
    max_bytes: int,
    on_progress: Callable[[Job], Awaitable[None]] | None,
    clock: Callable[[], float],
) -> Result:
    failures = 0
    submitted, started = clock(), None
    while True:
        if started is None and job.state != "queued":
            started = clock()
        if on_progress is not None:
            await on_progress(job)
        if job.state == "done":
            break
        if job.state == "failed":
            LOGGER.warning("Image job %s failed on the service", job.job_id)
            raise ImageGenError(FAILED, "failed")
        if job.state == "cancelled":
            raise ImageGenError(STOPPED, "stopped")
        if started is None and clock() - submitted >= service.queue_wait_seconds:
            LOGGER.warning("Image job %s waited too long in the queue", job.job_id)
            raise ImageGenError(QUEUE_TIMED_OUT, "queue_timeout")
        if started is not None and clock() - started >= service.timeout_seconds:
            LOGGER.warning("Image job %s timed out", job.job_id)
            raise ImageGenError(TIMED_OUT, "timeout")
        await sleep_for(POLL_SECONDS, clock=clock)
        try:
            job = await service.status(session, job.job_id)
            failures = 0
        except (aiohttp.ClientError, TimeoutError, ValueError) as error:
            failures += 1
            LOGGER.warning("Image job %s poll failed: %s", job.job_id, type(error).__name__)
            if failures >= POLL_FAILURES:
                raise ImageGenError(UNAVAILABLE, "unavailable") from None
    if not job.images:
        raise ImageGenError(FAILED, "failed")
    LOGGER.info(
        "Image job %s done: queued %.0fs, ran %.0fs",
        job.job_id,
        job.queued_s,
        clock() - (started if started is not None else submitted),
    )
    await asyncio.to_thread(into.mkdir, parents=True, exist_ok=True)
    # The run timeout ends after the last download, so the downloads share what is left of it.
    deadline = (started if started is not None else submitted) + service.timeout_seconds
    paths = []
    for image in job.images:
        if (left := deadline - clock()) <= 0:
            LOGGER.warning("Image job %s timed out before its downloads ended", job.job_id)
            raise ImageGenError(TIMED_OUT, "timeout")
        try:
            data, mime = await service.download(session, job.job_id, image, max_bytes, left)
        except (aiohttp.ClientError, TimeoutError) as error:
            LOGGER.warning("Image job %s download failed: %s", job.job_id, type(error).__name__)
            if deadline - clock() <= 0:
                raise ImageGenError(TIMED_OUT, "timeout") from None
            raise ImageGenError(UNAVAILABLE, "unavailable") from None
        path = into / f"image-{image.index}{_SUFFIXES[mime]}"
        await asyncio.to_thread(path.write_bytes, data)
        paths.append(path)
    seeds = tuple(image.seed for image in job.images)
    return Result(model, tuple(paths), seeds, job.enhance_used, job.sensitive)


def progress_text(job: Job) -> str:
    """One reading of a job as the placeholder shows it."""
    if job.state == "queued":
        # The GPU queue is shared with the local model, so what is ahead may be a chat answer.
        return f"🎨 排隊中，前面還有 {job.position} 個…" if job.position else "🎨 排隊中…"
    if job.state == "generating" and job.steps:
        return f"🎨 生成中 {job.step}/{job.steps}…"
    return {
        "preparing": "🎨 準備中…",
        "generating": "🎨 生成中…",
        "postprocessing": "🎨 後製中…",
        "done": "🎨 完成，上傳中…",
    }.get(job.state, "🎨 處理中…")


def caption(prompt: str, result: Result, by: int | None = None) -> str:
    """The text posted with the images: the prompt quoted (Discord does not echo a slash
    command's input), the model and the seeds to reproduce them, and who asked when it is a
    channel post of its own."""
    shown = prompt if len(prompt) <= 1500 else prompt[:1500] + "…"
    quoted = "\n".join(f"> {line}" for line in shown.splitlines() or [""])
    seeds = ", ".join(str(s) for s in result.seeds if s is not None)
    who = f"<@{by}> " if by is not None else ""
    footer = f"-# {result.model}" + (f" · seed {seeds}" if seeds else "")
    if result.enhance_used:  # `cloud` may have fallen back to `local`: say what ran
        footer += f" · enhance {result.enhance_used}"
    return f"{who}**畫**：\n{quoted}\n{footer}"


def extract_image_tags(answer: str) -> tuple[str, list[dict[str, str]]]:
    """(answer without the <image_gen/> tags, each tag's attributes). Tags inside code or quoted
    lines are left in the text and not acted on (tags.py)."""
    asks = [dict(_ATTR.findall(body)) for body in tags.findall(IMAGE_TAG, answer)]
    return tags.strip(IMAGE_TAG, answer).strip(), asks


def request_from_tag(attrs: dict[str, str]) -> tuple[ImageRequest | None, str]:
    """(the request a tag asks for, "") or (None, why it cannot be read, for the model)."""
    unknown = sorted(set(attrs) - set(TAG_ATTRS))
    if unknown:
        return None, f"unknown attribute {', '.join(unknown)}; use only {', '.join(TAG_ATTRS)}"
    prompt = attrs.get("prompt", "").strip()
    if not prompt:
        return None, 'prompt="…" is missing'
    numbers: dict[str, int | None] = {}
    for name in ("count", "seed"):
        raw = attrs.get(name, "").strip()
        if raw and not (raw.isascii() and raw.isdigit()):  # isdigit() alone passes "²"
            return None, f"{name} must be a whole number"
        numbers[name] = int(raw) if raw else None
    if numbers["seed"] is not None and numbers["seed"] > MAX_SEED:
        return None, f"seed must be 0-{MAX_SEED}"
    return (
        ImageRequest(
            prompt,
            attrs.get("model", "").strip(),
            negative=attrs.get("negative", "").strip() or None,
            aspect=attrs.get("aspect", "").strip() or None,
            enhance=attrs.get("enhance", "").strip().lower() or None,  # resolve checks it
            **numbers,
        ),
        "",
    )


def prompt_note(config: Config) -> str:
    """The <image_gen/> instructions for codex._prompt; "" while IMAGE_GEN_URL is unset, so a
    Bot without the service never offers it. The last catalog read names the choices."""
    if not config.image_gen_url:
        return ""
    catalog, choices = _last_catalog, ""
    if catalog is not None:
        models = ", ".join(m.id for m in catalog.models)
        choices = (
            f" Models: {models} (default {catalog.default_model}); aspects:"
            f" {', '.join(catalog.aspects)}; count 1-{catalog.max_count}."
        )
    return (
        "The host's local image service is slow and shares the local model's GPU, so use it only"
        " when the member explicitly asks for it: says 地端, 本機, 本地 or local, or names one of"
        " its models, in this message or earlier in this conversation. Otherwise draw with your"
        " own image tool if you have one, or say you cannot"
        " draw here and that the member can ask for the local service or use the -image command."
        " For an explicit request, append"
        ' <image_gen prompt="what to draw"/> after your answer; add model="…", aspect="3:4",'
        ' count="2", seed="123", negative="what to avoid" or enhance="off" (local, cloud or off:'
        " how the prompt is rewritten first) only when the member asks for them."
        + choices
        + " The Bot checks the request, runs it on the image service and"
        " posts the picture in the channel when it is ready, adding its own note to your"
        " answer. Never claim a picture was made, or is being made, without emitting the tag,"
        " and never describe a picture you have not seen. No double quote inside a value."
    )


LOCAL_WORDS = ("地端", "本機", "本地", "local")


def asked_for_local(asked: str, catalog: Catalog | None) -> bool:
    """Whether the member's own words (without a replied-to message) ask for the local image
    service: one of LOCAL_WORDS or a model's id or name from the catalog, as a whole word. The
    <image_gen/> tag runs only then, so a model that emits it on a plain "draw this" does not put
    a slow job on the shared GPU."""
    text = asked.lower()
    names = [w.lower() for w in LOCAL_WORDS]
    if catalog is not None:
        names += [n.lower() for m in catalog.models for n in (m.id, m.name) if n]
    # Whole words, so "plain" does not match "explain"; CJK has no word gaps to need it.
    return any(re.search(rf"(?<![a-z0-9]){re.escape(name)}(?![a-z0-9])", text) for name in names)


# ----- per-guild switch and limits --------------------------------------------------------------

DEFAULT_DAILY = 20
DEFAULT_IN_FLIGHT = 1
SPOILER_MODES = ("auto", "always", "off")
DEFAULT_SPOILER = "auto"  # hide what the service marked sensitive
MAX_DAILY = 1000
MAX_IN_FLIGHT = 10
USAGE_KEEP_DAYS = 7
USAGE_SCHEMA = """
CREATE TABLE IF NOT EXISTS image_usage (
    guild_id INTEGER NOT NULL,
    user_id INTEGER NOT NULL,
    day TEXT NOT NULL,
    images INTEGER NOT NULL,
    PRIMARY KEY (guild_id, user_id, day)
);
"""


def taipei_day() -> str:
    return datetime.now(TAIPEI).date().isoformat()


class ImageSettings:
    """Each guild's image switch and limits, as rows of the guild_settings table that
    linkclean's SwitchStore keeps (no row = the default: on, DEFAULT_DAILY images per member a
    day, DEFAULT_IN_FLIGHT job per member at once), and each member's images per day. Days are
    Taiwan days. The database is the linkclean one, so the nightly backup covers it."""

    def __init__(self, switches, today: Callable[[], str] = taipei_day) -> None:
        self.switches = switches
        self.today = today
        with switches._connect() as connection:
            connection.executescript(USAGE_SCHEMA)

    def _number(self, guild_id: int, key: str, default: int) -> int:
        raw = self.switches._get(guild_id, key)
        return int(raw) if raw is not None and raw.isdigit() else default

    def enabled(self, guild_id: int) -> bool:
        return self.switches._get(guild_id, "image") != "off"

    def daily_limit(self, guild_id: int) -> int:
        return self._number(guild_id, "image_daily", DEFAULT_DAILY)

    def in_flight_limit(self, guild_id: int) -> int:
        return self._number(guild_id, "image_in_flight", DEFAULT_IN_FLIGHT)

    def spoiler(self, guild_id: int) -> str:
        raw = self.switches._get(guild_id, "image_spoiler")
        return raw if raw in SPOILER_MODES else DEFAULT_SPOILER

    def update(
        self,
        guild_id: int,
        enabled: bool | None = None,
        daily: int | None = None,
        in_flight: int | None = None,
        spoiler: str | None = None,
    ) -> None:
        if daily is not None and not 1 <= daily <= MAX_DAILY:
            raise ValueError(f"每日張數要在 1 到 {MAX_DAILY} 之間")
        if in_flight is not None and not 1 <= in_flight <= MAX_IN_FLIGHT:
            raise ValueError(f"同時進行的工作數要在 1 到 {MAX_IN_FLIGHT} 之間")
        if spoiler is not None and spoiler not in SPOILER_MODES:
            raise ValueError(f"spoiler 只能是 {', '.join(SPOILER_MODES)}")
        if enabled is not None:
            self.switches._put(guild_id, "image", "on" if enabled else "off")
        if spoiler is not None:
            self.switches._put(guild_id, "image_spoiler", spoiler)
        if daily is not None:
            self.switches._put(guild_id, "image_daily", str(daily))
        if in_flight is not None:
            self.switches._put(guild_id, "image_in_flight", str(in_flight))

    def used(self, guild_id: int, user_id: int, day: str | None = None) -> int:
        with self.switches._connect() as connection:
            row = connection.execute(
                "SELECT images FROM image_usage WHERE guild_id=? AND user_id=? AND day=?",
                (guild_id, user_id, day or self.today()),
            ).fetchone()
        return 0 if row is None else int(row["images"])

    def charge(self, guild_id: int, user_id: int, images: int) -> str:
        """Count `images` against today; returns the day, for a refund to find the same row."""
        day = self.today()
        cutoff = (datetime.fromisoformat(day) - timedelta(days=USAGE_KEEP_DAYS)).date()
        with self.switches._connect() as connection:
            connection.execute(
                "INSERT INTO image_usage (guild_id, user_id, day, images) VALUES (?, ?, ?, ?) "
                "ON CONFLICT (guild_id, user_id, day) DO UPDATE SET images=images+excluded.images",
                (guild_id, user_id, day, images),
            )
            connection.execute("DELETE FROM image_usage WHERE day < ?", (cutoff.isoformat(),))
        return day

    def refund(self, guild_id: int, user_id: int, images: int, day: str) -> None:
        with self.switches._connect() as connection:
            connection.execute(
                "UPDATE image_usage SET images=MAX(images-?, 0) "
                "WHERE guild_id=? AND user_id=? AND day=?",
                (images, guild_id, user_id, day),
            )


@dataclass(slots=True)
class Reservation:
    """One admitted job: it holds an in-flight slot and its images counted against the day
    until `settle` (bot.py `_image_reserve`). Settling twice does nothing."""

    guild_id: int
    user_id: int
    images: int
    day: str
    release: Callable[[Reservation, bool], None]
    settled: bool = False

    def settle(self, refund: bool) -> None:
        if not self.settled:
            self.settled = True
            self.release(self, refund)
