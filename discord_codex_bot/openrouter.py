from __future__ import annotations

import asyncio
import base64
import hashlib
import json
import logging
import mimetypes
import re
import time
import uuid
from collections.abc import Callable, Sequence
from dataclasses import dataclass, replace
from pathlib import Path

import aiohttp

from .codex import BackendError, CodexResult, _prompt, output_style
from .config import Config, local_params

LOGGER = logging.getLogger(__name__)
RETRY_AFTER_FAILURE_SECONDS = 60
CONNECT_RETRY_SECONDS = 1
UNSTABLE = "（免費模型可能暫時不可用，換一個試試）"
# The routers' reasoning.effort vocabulary; the shared option's "max" is their "xhigh".
_EFFORT = {"low": "low", "medium": "medium", "high": "high", "xhigh": "xhigh", "max": "xhigh"}
_THINK_OPEN, _THINK_CLOSE = "<think>", "</think>"


@dataclass(frozen=True, slots=True)
class Router:
    """One OpenAI-compatible model router. All are stateless (the Bot keeps the transcript),
    list a catalog at /models and take chat completions; they differ in host, key, how a free
    model is marked, and the thread-id prefix that tells their transcripts apart."""

    key: str  # backend id stored per member ("<key>:<model>")
    label: str  # shown to members
    api: Callable[[Config], str]  # base URL
    thread_prefix: str
    api_key: Callable[[Config], str]
    enabled: Callable[[Config], bool]  # offered to members at all
    is_free: Callable[[dict], bool]
    excluded: tuple[str, ...] = ()
    unstable: str = UNSTABLE  # appended to every error the member sees
    free_tier: bool = True  # the catalog is a free tier that comes and goes
    # Refused connections retried, a second apart: nothing reached the server, so a retry
    # cannot run a request twice. The local relay refuses while every worker is busy.
    connect_retries: int = 0
    # Drop a leading <think>…</think> block (local reasoning models). Off for the hosted
    # routers: an answer may start with a literal "<think>" there.
    drops_think: bool = False
    # Output cap sent with every request; None leaves the provider's own default.
    max_tokens: Callable[[Config], int] | None = None
    # Extra request fields (sampling) under the Bot's own: the Bot's keys always win.
    params: Callable[[Config], dict] | None = None
    # Path-shaped ids shown by their last part: mlx_lm.server and llama-server list a model by
    # its file path on the host, which members must not see.
    short_ids: bool = False

    @property
    def models_word(self) -> str:
        """What its catalog is called to members: "N 個免費模型", or "N 個模型" locally."""
        return "免費模型" if self.free_tier else "模型"


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
        api=lambda config: "https://openrouter.ai/api/v1",
        thread_prefix="or-",
        api_key=lambda config: config.openrouter_api_key,
        enabled=lambda config: bool(config.openrouter_api_key),
        is_free=_openrouter_free,
        # Free entries that are not chat models for our purpose (probed 2026-09-12): safety
        # classifiers answer "User Safety: safe"; inkling refuses plain chat (403 "agentic").
        excluded=("content-safety", "thinkingmachines/inkling"),
    ),
    "orcarouter": Router(
        key="orcarouter",
        label="OrcaRouter",
        api=lambda config: "https://api.orcarouter.ai/v1",
        thread_prefix="oc-",
        api_key=lambda config: config.orcarouter_api_key,
        enabled=lambda config: bool(config.orcarouter_api_key),
        is_free=_orcarouter_free,
    ),
    # A server on the Docker host the operator runs: whatever it serves is offered, no price
    # rule; llama-server and mlx_lm.server list no architecture, so models count as text-only.
    "local": Router(
        key="local",
        label="地端模型",
        api=lambda config: config.local_llm_url.rstrip("/"),
        thread_prefix="lo-",
        api_key=lambda config: config.local_llm_api_key,
        enabled=lambda config: bool(config.local_llm_url),
        is_free=lambda entry: True,
        unstable="（地端模型可能沒有在跑，換一個試試）",
        free_tier=False,
        connect_retries=3,
        drops_think=True,
        max_tokens=lambda config: config.local_llm_max_tokens,
        params=local_params,
        short_ids=True,
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
    api_id: str = ""  # the id the server takes, when `id` is a shortened one


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
    if router.short_ids:
        out = _shorten(out)
    out.sort(key=lambda m: (not m.image, m.name.lower()))
    return out


_PATH_ID = re.compile(r"^(/|~|[A-Za-z]:[\\/])")
# A directory part of a host path inside free text: every component but the last.
_PATH_DIRS = re.compile(r"(?<![\w.:/\\-])(?:~|[A-Za-z]:)?[\\/](?:[^\s'\"`\\/]+[\\/])+")


def short_id(model_id: str) -> str:
    """A path-shaped id (`/Users/x/models/qwen`, `C:\\models\\qwen.gguf`) as its last part;
    anything else (a repo id like `mlx-community/qwen`) unchanged."""
    if not _PATH_ID.match(model_id):
        return model_id
    return re.split(r"[\\/]", model_id.rstrip("/\\"))[-1] or model_id


def scrub_paths(text: str) -> str:
    """`text` with each host path cut to its last part: a local server's error names the model
    by its path, which members must not see."""
    return _PATH_DIRS.sub("", text)


def _tag(full_id: str) -> str:
    """Four hex digits fixed by the full path, so an alias names the same model on every refresh."""
    return hashlib.sha256(full_id.encode()).hexdigest()[:4]


def _shorten(models: list[Model]) -> list[Model]:
    """Each path-shaped model under its short id, keeping the full one for requests. A short id
    another model also shows (two paths ending in the same name, or a repo id) becomes
    "<name>~<tag>", the tag fixed by the full path. A path-shaped name is cut the same way."""
    short = [short_id(m.id) for m in models]
    taken = [s for m, s in zip(models, short, strict=True) if s == m.id]
    out = []
    for model, name in zip(models, short, strict=True):
        if name != model.id and (short.count(name) > 1 or name in taken):
            name = f"{name}~{_tag(model.id)}"
        if name == model.id:
            out.append(replace(model, name=short_id(model.name)))
            continue
        shown = name if model.name == model.id else short_id(model.name)
        out.append(replace(model, id=name, name=shown, api_id=model.id))
    return out


def _session(
    config: Config, timeout_seconds: int, router: Router = OPENROUTER_ROUTER
) -> aiohttp.ClientSession:
    return aiohttp.ClientSession(
        timeout=aiohttp.ClientTimeout(total=timeout_seconds), headers=_headers(config, router)
    )


def _headers(config: Config, router: Router) -> dict[str, str]:
    """No Authorization header without a key: a local server may run without one."""
    headers = {
        "HTTP-Referer": "https://github.com/liutommy/discord-codex-bot",
        "X-Title": "discord-codex-bot",
    }
    if key := router.api_key(config):
        headers["Authorization"] = f"Bearer {key}"
    return headers


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
                    async with session.get(f"{self.router.api(self._config)}/models") as response:
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
        """The model shown as `model_id`; for a stored "<name>~<tag>" whose twin has since gone,
        the model that alias was made for, now shown as plain <name>."""
        found = next((m for m in self.models if m.id == model_id), None)
        if found is not None or not self.router.short_ids or "~" not in model_id:
            return found
        name, tag = model_id.rsplit("~", 1)
        return next(
            (m for m in self.models if m.id == name and m.api_id and _tag(m.api_id) == tag), None
        )


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
    speaker: int | None = None,
    history: str = "",
) -> CodexResult:
    """One turn on an OpenAI-compatible router with the same contract as run_codex. The
    conversation lives in a Bot-kept transcript (thread id `<prefix>…`); `resume` replays it,
    bounded by OPENROUTER_HISTORY_CHARS. Images go inline (base64) when the catalog says the
    model reads them; effort is sent only when the catalog says the model takes it."""
    info = catalog.get(model) if catalog else None
    prompt = (
        user_prompt
        if raw
        else _prompt(
            user_prompt,
            memory,
            output_style(config),
            personal_style,
            links,
            help,
            files,
            speaker,
            history=history,
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
    body: dict = {
        **(router.params(config) if router.params is not None else {}),
        "model": (info.api_id if info else "") or model,
        "messages": messages,
    }
    if router.max_tokens is not None:
        body["max_tokens"] = router.max_tokens(config)
    if effort and (info is None or info.reasoning) and effort in _EFFORT:
        body["reasoning"] = {"effort": _EFFORT[effort]}
    if schema is not None:
        raw_schema = await asyncio.to_thread(schema.read_text, "utf-8")
        body["response_format"] = {
            "type": "json_schema",
            "json_schema": {"name": "answer", "strict": True, "schema": json.loads(raw_schema)},
        }
    label, unstable = router.label, router.unstable
    if not router.enabled(config):
        raise BackendError(f"{label} 未設定")
    if on_delta is not None:
        body["stream"] = True
    try:
        for attempt in range(router.connect_retries + 1):
            try:
                status, payload = await _post(router, config, body, on_delta)
                break
            except aiohttp.ClientConnectorError as refused:
                if attempt == router.connect_retries or not isinstance(
                    refused.os_error, ConnectionRefusedError
                ):
                    raise
                await asyncio.sleep(CONNECT_RETRY_SECONDS)
    except (aiohttp.ClientError, TimeoutError, ValueError) as error:
        raise BackendError(f"{label} 連線失敗：{type(error).__name__}{unstable}") from error
    error = payload.get("error") if isinstance(payload, dict) else None
    if status != 200 or error:
        detail = (error or {}).get("message") if isinstance(error, dict) else str(error or "")
        where = f"HTTP {status}" if status != 200 else "上游錯誤"
        LOGGER.warning("%s %s: %s", router.label, where, detail)
        shown = scrub_paths(detail) if router.short_ids else detail
        raise BackendError(f"{label} {where}：{shown or '無說明'}{unstable}")
    choices = payload.get("choices") or []
    choice = choices[0] if choices else {}
    message = choice.get("message") or {}
    raw = message_text(message.get("content") or "")
    text = strip_think(raw) if router.drops_think else raw
    # The server answered, so "may not be running" no longer applies: say what came back.
    truncated = choice.get("finish_reason") == "length"
    if not text.strip():
        if not truncated:
            raise BackendError(f"{label} 有回應，但沒有答案文字，換一個模型試試")
        thought = bool(message.get("reasoning") or message.get("reasoning_content")) or (
            router.drops_think and raw.lstrip().startswith(_THINK_OPEN)
        )
        cap = f"（{body['max_tokens']} token）" if "max_tokens" in body else ""
        spent = "思考過程就用完了輸出長度上限" if thought else "輸出在寫出答案前就達到長度上限"
        raise BackendError(
            f"{label} {spent}{cap}，還沒寫出答案。把問題問得更具體，或換一個模型試試"
        )
    # Stored without the image bytes: replaying base64 every turn would swamp the history.
    stored_user = prompt + (f"\n\n[附圖 {len(images)} 張]" if images else "")
    _save_transcript(
        config,
        thread_id,
        model,
        history
        + [
            {"role": "user", "content": stored_user},
            {"role": "assistant", "content": text.strip()},
        ],
    )
    shown = text.strip() + ("\n\n（回答達到長度上限，後面被截斷。）" if truncated else "")
    return CodexResult(shown, (), None, thread_id, resumed)


def strip_think(text: str, streaming: bool = False) -> str:
    """The answer without a leading <think>…</think> block, where Qwen-style local models put
    their reasoning. An unclosed block is reasoning so far: no answer yet. While `streaming`, a
    start that may still grow into <think> ("<thi") counts as one too."""
    body = text.lstrip()
    if streaming and body and _THINK_OPEN.startswith(body):
        return ""
    if not body.startswith(_THINK_OPEN):
        return text
    _, closed, answer = body.partition(_THINK_CLOSE)
    return answer.lstrip() if closed else ""


async def _post(router: Router, config: Config, body: dict, on_delta) -> tuple[int, dict]:
    async with _session(config, config.codex_timeout_seconds, router) as session:
        url = f"{router.api(config)}/chat/completions"
        async with session.post(url, json=body) as response:
            if on_delta is not None and response.status == 200:
                return response.status, await _read_sse(response, on_delta, router.drops_think)
            return response.status, await response.json(content_type=None)


async def _read_sse(response, on_delta, drops_think: bool = False) -> dict:
    """Consume an OpenAI-style SSE stream, reporting the accumulated answer text after each
    content delta (reasoning deltas and a leading <think> block are not shown), and return a
    completion-shaped payload."""
    accumulated = reasoning = ""
    finish_reason = None
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
        finish_reason = (choices[0].get("finish_reason") if choices else None) or finish_reason
        reasoning += str(delta.get("reasoning") or delta.get("reasoning_content") or "")
        piece = delta.get("content")
        if piece:
            accumulated += str(piece)
            shown = strip_think(accumulated, streaming=True) if drops_think else accumulated
            if shown:
                await on_delta(shown)
    message = {"content": accumulated, "reasoning": reasoning}
    return {"choices": [{"message": message, "finish_reason": finish_reason}]}


async def run_openrouter(user_prompt: str, config: Config, model: str, **kw) -> CodexResult:
    """OpenRouter turn — `run_router` on the OpenRouter descriptor."""
    return await run_router(OPENROUTER_ROUTER, user_prompt, config, model, **kw)
