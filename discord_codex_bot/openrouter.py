from __future__ import annotations

import asyncio
import base64
import json
import logging
import mimetypes
import time
import uuid
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from pathlib import Path

import aiohttp

from .codex import CodexResult, _prompt, output_style
from .config import Config

LOGGER = logging.getLogger(__name__)
RETRY_AFTER_FAILURE_SECONDS = 60
UNSTABLE = "（免費模型可能暫時不可用，換一個試試）"
# The routers' reasoning.effort vocabulary; the shared option's "max" is their "xhigh".
_EFFORT = {"low": "low", "medium": "medium", "high": "high", "xhigh": "xhigh", "max": "xhigh"}


@dataclass(frozen=True, slots=True)
class Router:
    """One OpenAI-compatible model router. Both are stateless (the Bot keeps the transcript),
    list a catalog at /models and take chat completions; they differ in host, key, how a free
    model is marked, and the thread-id prefix that tells their transcripts apart."""

    key: str  # backend id stored per member ("<key>:<model>")
    label: str  # shown to members
    api: str  # base URL
    thread_prefix: str
    api_key: Callable[[Config], str]
    is_free: Callable[[dict], bool]
    excluded: tuple[str, ...] = ()


def _openrouter_free(entry: dict) -> bool:
    """OpenRouter: zero prompt and completion price."""
    pricing = entry.get("pricing") or {}
    return pricing.get("prompt") == "0" and pricing.get("completion") == "0"


def _orcarouter_free(entry: dict) -> bool:
    """OrcaRouter: the -free tier — ids ending in -free (plus its free auto-router). Its /models
    lists them with no price at all rather than "0" (verified 2026-09-13)."""
    model_id = str(entry.get("id") or "")
    return model_id.endswith("-free") or model_id == "orcarouter/free"


ROUTERS: dict[str, Router] = {
    "openrouter": Router(
        key="openrouter",
        label="OpenRouter",
        api="https://openrouter.ai/api/v1",
        thread_prefix="or-",
        api_key=lambda config: config.openrouter_api_key,
        is_free=_openrouter_free,
        # Free entries that are not chat models for our purpose (probed 2026-09-12): safety
        # classifiers answer "User Safety: safe"; inkling refuses plain chat (403 "agentic").
        excluded=("content-safety", "thinkingmachines/inkling"),
    ),
    "orcarouter": Router(
        key="orcarouter",
        label="OrcaRouter",
        api="https://api.orcarouter.ai/v1",
        thread_prefix="oc-",
        api_key=lambda config: config.orcarouter_api_key,
        is_free=_orcarouter_free,
    ),
}
OPENROUTER_ROUTER = ROUTERS["openrouter"]


@dataclass(frozen=True, slots=True)
class Model:
    id: str
    name: str
    image: bool  # accepts image input
    reasoning: bool  # accepts the reasoning.effort parameter
    context: int


def parse_catalog(payload: dict, router: Router = OPENROUTER_ROUTER) -> list[Model]:
    """Free chat models from /models: the router's free rule, text out, text in. Text-only
    output is a pricing guard too: media models (e.g. Lyria) also show token prices of 0 while
    charging per song in the description. Image-capable models sort first, then by name — the
    order the autocomplete shows. Entries without an architecture (OrcaRouter's free ids) are
    taken as plain text models."""
    out: list[Model] = []
    for entry in payload.get("data") or []:
        if not router.is_free(entry):
            continue
        if any(marker in str(entry["id"]) for marker in router.excluded):
            continue
        arch = entry.get("architecture") or {}
        inputs = arch.get("input_modalities") or ["text"]
        if (arch.get("output_modalities") or ["text"]) != ["text"] or "text" not in inputs:
            continue
        out.append(
            Model(
                id=str(entry["id"]),
                name=str(entry.get("name") or entry["id"]),
                image="image" in inputs,
                reasoning="reasoning" in (entry.get("supported_parameters") or []),
                context=int(entry.get("context_length") or 0),
            )
        )
    out.sort(key=lambda m: (not m.image, m.name.lower()))
    return out


def _session(
    config: Config, timeout_seconds: int, router: Router = OPENROUTER_ROUTER
) -> aiohttp.ClientSession:
    return aiohttp.ClientSession(
        timeout=aiohttp.ClientTimeout(total=timeout_seconds),
        headers={
            "Authorization": f"Bearer {router.api_key(config)}",
            "HTTP-Referer": "https://github.com/liutommy/discord-codex-bot",
            "X-Title": "discord-codex-bot",
        },
    )


class Catalog:
    """A router's free-model list, refreshed from its API at most once per TTL. Free models come
    and go, so the API is the only source of truth; the last good list survives a failed refresh."""

    def __init__(self, config: Config, router: Router = OPENROUTER_ROUTER) -> None:
        self._config = config
        self.router = router
        self.models: list[Model] = []
        self._at = float("-inf")
        self._lock = asyncio.Lock()

    def _fresh(self) -> bool:
        return time.monotonic() - self._at < self._config.openrouter_catalog_ttl_seconds

    async def free_models(self) -> list[Model]:
        if self._fresh():
            return self.models
        async with self._lock:
            if self._fresh():
                return self.models
            try:
                timeout = self._config.link_timeout_seconds
                async with _session(self._config, timeout, self.router) as session:
                    async with session.get(f"{self.router.api}/models") as response:
                        response.raise_for_status()
                        payload = await response.json(content_type=None)
                self.models = parse_catalog(payload, self.router)
                self._at = time.monotonic()
            except (aiohttp.ClientError, TimeoutError, ValueError, KeyError) as error:
                LOGGER.warning(
                    "%s catalog refresh failed: %s", self.router.label, type(error).__name__
                )
                ttl = self._config.openrouter_catalog_ttl_seconds
                self._at = time.monotonic() - ttl + RETRY_AFTER_FAILURE_SECONDS
        return self.models

    def get(self, model_id: str) -> Model | None:
        return next((m for m in self.models if m.id == model_id), None)


def transcript_path(config: Config, thread_id: str) -> Path:
    return config.openrouter_dir / f"{thread_id}.json"


def is_router_thread(thread_id: str) -> bool:
    return any(thread_id.startswith(r.thread_prefix) for r in ROUTERS.values())


def load_transcript(config: Config, thread_id: str) -> list[dict]:
    """Stored messages of one Bot-kept conversation; [] when unknown or unreadable."""
    if not is_router_thread(thread_id):
        return []
    try:
        data = json.loads(transcript_path(config, thread_id).read_text("utf-8"))
        messages = data.get("messages")
        return messages if isinstance(messages, list) else []
    except (OSError, ValueError):
        return []


def _save_transcript(config: Config, thread_id: str, model: str, messages: list[dict]) -> None:
    path = transcript_path(config, thread_id)
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {"model": model, "at": time.time(), "messages": messages}
    path.write_text(json.dumps(payload, ensure_ascii=False), "utf-8")


def message_text(content) -> str:
    if isinstance(content, str):
        return content
    return "".join(
        part.get("text", "") for part in content if isinstance(part, dict) and "text" in part
    )


def trim_history(messages: list[dict], budget: int) -> list[dict]:
    """The newest whole messages that fit in `budget` characters (the model's context is what
    it is; the Bot bounds what it re-sends every turn)."""
    kept: list[dict] = []
    size = 0
    for message in reversed(messages):
        size += len(message_text(message.get("content")))
        if size > budget and kept:
            break
        kept.append(message)
    return list(reversed(kept))


def _system_prompt(config: Config, plain: bool) -> str:
    """The persona Codex reads from AGENTS.md in its cwd, sent as the system message here; a
    member with a personal style gets the persona-free workspace's file, like the other backends."""
    workspace = config.codex_workspace_plain if plain else config.codex_workspace
    try:
        return (workspace / "AGENTS.md").read_text("utf-8").strip()
    except OSError:
        return ""


def _image_part(path: Path) -> dict:
    mime = mimetypes.guess_type(path.name)[0] or "image/jpeg"
    data = base64.b64encode(path.read_bytes()).decode("ascii")
    return {"type": "image_url", "image_url": {"url": f"data:{mime};base64,{data}"}}


async def run_router(
    router: Router,
    user_prompt: str,
    config: Config,
    model: str,
    images: Sequence[Path] = (),
    resume: str = "",
    memory: str = "",
    raw: bool = False,
    personal_style: str = "",
    schema: Path | None = None,
    plain: bool = False,
    links: str = "",
    effort: str = "",
    catalog: Catalog | None = None,
    help: str = "",
    files: str = "",
    on_delta=None,
) -> CodexResult:
    """One turn on an OpenAI-compatible router with the same contract as run_codex. The
    conversation lives in a Bot-kept transcript (thread id `<prefix>…`); `resume` replays it,
    bounded by OPENROUTER_HISTORY_CHARS. Images go inline (base64) when the catalog says the
    model reads them; effort is sent only when the catalog says the model takes it."""
    plain = plain or bool(personal_style)
    info = catalog.get(model) if catalog else None
    prompt = (
        user_prompt
        if raw
        else _prompt(
            user_prompt, memory, output_style(config), personal_style, links, help, files
        )
    )
    history = load_transcript(config, resume) if resume else []
    resumed = bool(history)
    thread_id = resume if resumed else router.thread_prefix + uuid.uuid4().hex[:16]

    parts: list[dict] = [{"type": "text", "text": prompt}]
    if images and (info is None or info.image):
        parts += [await asyncio.to_thread(_image_part, image) for image in images]
    elif images:
        parts[0]["text"] += "\n\n（成員附了圖片，但這個模型看不到圖片。）"
    system = _system_prompt(config, plain)
    messages = [{"role": "system", "content": system}] if system else []
    messages += trim_history(history, config.openrouter_history_chars)
    messages.append({"role": "user", "content": parts if len(parts) > 1 else parts[0]["text"]})
    body: dict = {"model": model, "messages": messages}
    if effort and (info is None or info.reasoning) and effort in _EFFORT:
        body["reasoning"] = {"effort": _EFFORT[effort]}
    if schema is not None:
        raw_schema = await asyncio.to_thread(schema.read_text, "utf-8")
        body["response_format"] = {
            "type": "json_schema",
            "json_schema": {"name": "answer", "strict": True, "schema": json.loads(raw_schema)},
        }
    label = router.label
    if on_delta is not None:
        body["stream"] = True
    try:
        async with _session(config, config.codex_timeout_seconds, router) as session:
            async with session.post(f"{router.api}/chat/completions", json=body) as response:
                status = response.status
                if on_delta is not None and status == 200:
                    payload = await _read_sse(response, on_delta)
                else:
                    payload = await response.json(content_type=None)
    except (aiohttp.ClientError, TimeoutError, ValueError) as error:
        raise RuntimeError(f"{label} 連線失敗：{type(error).__name__}{UNSTABLE}") from error
    error = payload.get("error") if isinstance(payload, dict) else None
    if status != 200 or error:
        detail = (error or {}).get("message") if isinstance(error, dict) else str(error or "")
        where = f"HTTP {status}" if status != 200 else "上游錯誤"
        raise RuntimeError(f"{label} {where}：{detail or '無說明'}{UNSTABLE}")
    choices = payload.get("choices") or []
    text = message_text((choices[0].get("message") or {}).get("content") or "") if choices else ""
    if not text.strip():
        raise RuntimeError(f"{label} 沒有回傳文字{UNSTABLE}")
    # Stored without the image bytes: replaying base64 every turn would swamp the history.
    stored_user = prompt + (f"\n\n[附圖 {len(images)} 張]" if images else "")
    _save_transcript(
        config, thread_id, model,
        history + [{"role": "user", "content": stored_user},
                   {"role": "assistant", "content": text.strip()}],
    )
    return CodexResult(text.strip(), (), None, thread_id, resumed)


async def _read_sse(response, on_delta) -> dict:
    """Consume an OpenAI-style SSE stream, reporting the accumulated answer text after each
    content delta (reasoning deltas are not shown), and return a completion-shaped payload."""
    accumulated = ""
    async for raw in response.content:
        line = raw.decode("utf-8", errors="replace").strip()
        if not line.startswith("data:"):
            continue
        data = line[5:].strip()
        if data == "[DONE]":
            break
        try:
            chunk = json.loads(data)
        except json.JSONDecodeError:
            continue
        if isinstance(chunk, dict) and chunk.get("error"):
            return chunk  # surfaced by the caller like a non-streaming error payload
        choices = chunk.get("choices") or []
        delta = (choices[0].get("delta") or {}) if choices else {}
        piece = delta.get("content")
        if piece:
            accumulated += str(piece)
            await on_delta(accumulated)
    return {"choices": [{"message": {"content": accumulated}}]}


async def run_openrouter(user_prompt: str, config: Config, model: str, **kw) -> CodexResult:
    """OpenRouter turn — `run_router` on the OpenRouter descriptor."""
    return await run_router(OPENROUTER_ROUTER, user_prompt, config, model, **kw)
