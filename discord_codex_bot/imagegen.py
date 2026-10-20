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
import contextlib
import json
import logging
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
OPTIONS = ("negative", "aspect", "count", "seed", "hires", "upscale", "detailer", "cloud_enhance")
TERMINAL = frozenset({"done", "failed", "cancelled"})
_JOB_ID = re.compile(r"^[A-Za-z0-9._-]{1,100}$")
_SUFFIXES = {"image/png": ".png", "image/jpeg": ".jpg", "image/webp": ".webp"}

UNAVAILABLE = "圖片生成服務現在不能用，請稍後再試。"
QUEUE_FULL = "圖片生成排隊已滿，請稍後再試。"
FAILED = "圖片生成失敗，請換個描述或稍後再試。"
TIMED_OUT = "圖片生成超過時限，已取消。"
STOPPED = "圖片生成服務取消了這個工作。"

# The model's tool (codex._prompt): one per answer, attributes in any order.
IMAGE_TAG = re.compile(r'<image_gen((?:\s+[a-z_]{1,10}="[^"]{0,2000}")+)\s*/?>(?:\s*</image_gen>)?')
_ATTR = re.compile(r'([a-z_]{1,10})="([^"]{0,2000})"')
TAG_ATTRS = ("prompt", "model", "aspect", "count", "seed", "negative")
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


@dataclass(frozen=True, slots=True)
class Catalog:
    models: tuple[ImageModel, ...]
    default_model: str
    aspects: tuple[str, ...]
    upscale: tuple[str, ...]
    max_count: int

    def get(self, model_id: str) -> ImageModel | None:
        return next((m for m in self.models if m.id == model_id), None)


@dataclass(frozen=True, slots=True)
class ImageRequest:
    """What a member asked for. None is the model's default and is sent as null."""

    prompt: str
    model: str = ""
    negative: str | None = None
    aspect: str | None = None
    count: int | None = None
    seed: int | None = None
    hires: bool | None = None
    upscale: str | None = None
    detailer: bool | None = None
    cloud_enhance: bool | None = None

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
    position: int = 0
    step: int = 0
    steps: int = 0
    images: tuple[Image, ...] = ()


@dataclass(frozen=True, slots=True)
class Result:
    model: str
    paths: tuple[Path, ...]
    seeds: tuple[int | None, ...]


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
        name = item.get("name") if isinstance(item.get("name"), str) else item["id"]
        models.append(ImageModel(item["id"], name, frozenset(map(str, options)), defaults))
    max_count = payload.get("max_count", 1)
    if not isinstance(max_count, int) or max_count < 1:
        raise ValueError("catalog max_count is not a positive integer")
    return Catalog(
        tuple(models),
        str(payload.get("default_model") or (models[0].id if models else "")),
        tuple(map(str, payload.get("aspects") or ())),
        tuple(map(str, payload.get("upscale") or ())),
        max_count,
    )


def _bad(option: str, allowed: str) -> ImageGenError:
    return ImageGenError(
        f"{option} 只能是 {allowed}。", "bad_value", True, f"{option} must be one of {allowed}"
    )


def resolve(catalog: Catalog, request: ImageRequest) -> ImageModel:
    """The model `request` runs on, after checking every option it sets against the catalog —
    the checks the service makes too, done here so a refusal costs no queue slot."""
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

    return Job(
        job_id,
        str(payload.get("state") or ""),
        number("position"),
        number("step"),
        number("steps"),
        tuple(images),
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
    if status == 429:
        return ImageGenError(QUEUE_FULL, "queue_full")
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
        return self._config.image_gen_timeout_seconds

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
        body |= {"model": model, "steps": None, "cfg": None, "split": None}
        body |= {
            "max_bytes": max_bytes,
            "requester": {"guild": str(guild_id), "user": str(user_id)},
        }
        return _job(await self._call(session, "POST", "/v1/image/jobs", model, json=body))

    async def status(self, session: aiohttp.ClientSession, job_id: str) -> Job:
        return _job(await self._call(session, "GET", f"/v1/image/jobs/{job_id}"))

    async def download(
        self, session: aiohttp.ClientSession, job_id: str, image: Image, max_bytes: int
    ) -> tuple[bytes, str]:
        """One image's bytes and MIME type, refused past `max_bytes` (the service promises to
        fit it; a larger file would only fail at Discord)."""
        url = self._url(f"/v1/image/jobs/{job_id}/images/{image.index}")
        timeout = aiohttp.ClientTimeout(total=DOWNLOAD_TIMEOUT_SECONDS)
        async with session.get(url, timeout=timeout) as response:
            if response.status != 200:
                raise _error(response.status, None, "")
            mime = response.content_type
            data = await response.content.read(max_bytes + 1)
        if len(data) > max_bytes or mime not in _SUFFIXES:
            LOGGER.warning(
                "Image %s/%d unusable: %s, %d bytes", job_id, image.index, mime, len(data)
            )
            raise ImageGenError(FAILED, "failed")
        return data, mime

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
    every reading), and download its images into `into`. Raises ImageGenError. A job left
    unfinished for any reason — the asker cancelled (CancelledError), the deadline passed, a
    download failed — is cancelled on the service too, so it stops holding the GPU."""
    model = resolve(await service.catalog(), request)
    deadline = clock() + service.timeout_seconds
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
                deadline,
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
    deadline: float,
) -> Result:
    failures = 0
    while True:
        if on_progress is not None:
            await on_progress(job)
        if job.state == "done":
            break
        if job.state == "failed":
            LOGGER.warning("Image job %s failed on the service", job.job_id)
            raise ImageGenError(FAILED, "failed")
        if job.state == "cancelled":
            raise ImageGenError(STOPPED, "stopped")
        if clock() >= deadline:
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
    await asyncio.to_thread(into.mkdir, parents=True, exist_ok=True)
    paths = []
    for image in job.images:
        try:
            data, mime = await service.download(session, job.job_id, image, max_bytes)
        except (aiohttp.ClientError, TimeoutError) as error:
            LOGGER.warning("Image job %s download failed: %s", job.job_id, type(error).__name__)
            raise ImageGenError(UNAVAILABLE, "unavailable") from None
        path = into / f"image-{image.index}{_SUFFIXES[mime]}"
        await asyncio.to_thread(path.write_bytes, data)
        paths.append(path)
    return Result(model, tuple(paths), tuple(image.seed for image in job.images))


def progress_text(job: Job) -> str:
    """One reading of a job as the placeholder shows it."""
    if job.state == "queued":
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
        "When the member asks you to draw, paint or generate a picture, append"
        ' <image_gen prompt="what to draw"/> after your answer; add model="…", aspect="3:4",'
        ' count="2", seed="123" or negative="what to avoid" only when the member asks for'
        " them." + choices + " The Bot checks the request, runs it on the image service and"
        " posts the picture in the channel when it is ready, adding its own note to your"
        " answer. Never claim a picture was made, or is being made, without emitting the tag,"
        " and never describe a picture you have not seen. No double quote inside a value."
    )


# ----- per-guild switch and limits --------------------------------------------------------------

DEFAULT_DAILY = 20
DEFAULT_IN_FLIGHT = 1
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

    def update(
        self,
        guild_id: int,
        enabled: bool | None = None,
        daily: int | None = None,
        in_flight: int | None = None,
    ) -> None:
        if daily is not None and not 1 <= daily <= MAX_DAILY:
            raise ValueError(f"每日張數要在 1 到 {MAX_DAILY} 之間")
        if in_flight is not None and not 1 <= in_flight <= MAX_IN_FLIGHT:
            raise ValueError(f"同時進行的工作數要在 1 到 {MAX_IN_FLIGHT} 之間")
        if enabled is not None:
            self.switches._put(guild_id, "image", "on" if enabled else "off")
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
