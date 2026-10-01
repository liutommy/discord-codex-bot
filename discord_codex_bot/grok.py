"""Grok as a model provider: member turns answered by the xsearch sidecar's /chat — Grok Build
on the operator's SuperGrok / X Premium+ plan, with no client tools (the sidecar guarantees it;
see xsearch/server.py). The Bot's own <web>/<fetch>/<run> loop works here as for every backend,
and Grok's server-side X search is available to the model directly.

The sidecar starts every session from an empty home, so Grok keeps no conversation: like the
routers, the Bot keeps a transcript (thread id `gk-…`) and replays it within GROK_HISTORY_CHARS.
"""

from __future__ import annotations

import json
import logging
import re
import time
import uuid
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path

import aiohttp

from .backends import BackendUnavailable
from .codex import CodexResult, _prompt, output_style
from .config import Config
from .openrouter import _system_prompt, message_text, trim_history

LOGGER = logging.getLogger(__name__)
THREAD_PREFIX = "gk-"
THREAD_ID = re.compile(r"gk-[0-9a-f]{16}")
CATALOG_SECONDS = 6 * 3600
USAGE_SECONDS = 60


class GrokUnavailable(BackendUnavailable):
    """Grok cannot answer this turn right now; the next backend in the chain should."""

    label = "服務異常"


class GrokQuota(GrokUnavailable):
    label = "額度用完"


class GrokLogin(GrokUnavailable):
    label = "登入失效"


class GrokBusy(GrokUnavailable):
    label = "忙碌"


class GrokRefused(GrokUnavailable):
    """The sidecar refused the session: it was not the locked-down one (a prompt injection
    that got a tool call through, or Grok's output format changed). Its answer is never used."""

    label = "安全檢查未通過"


class GrokBadRequest(RuntimeError):
    """The Bot asked for something the sidecar will not do (unknown model or effort, prompt
    too long): a bug or a stale choice, reported as is rather than hidden by a fallback."""


_STATUS = {401: GrokLogin, 402: GrokQuota, 429: GrokBusy, 503: GrokRefused}


@dataclass(frozen=True, slots=True)
class GrokModel:
    id: str
    name: str
    efforts: tuple[str, ...]
    default_effort: str


_catalog: tuple[float, list[GrokModel]] = (0.0, [])
_usage: tuple[float, dict | None] = (0.0, None)


def enabled(config: Config) -> bool:
    return bool(config.xsearch_url)


async def _call(config: Config, method: str, path: str, body: dict | None, limit: float) -> dict:
    if not enabled(config):
        raise GrokUnavailable("Grok is not configured (XSEARCH_URL)")
    try:
        async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=limit)) as session:
            # UTF-8, not `json=` (which escapes every CJK character into six ASCII bytes)
            data = None if body is None else json.dumps(body, ensure_ascii=False).encode()
            async with session.request(
                method,
                f"{config.xsearch_url.rstrip('/')}{path}",
                data=data,
                headers={"Content-Type": "application/json"},
            ) as response:
                payload = await response.json(content_type=None)
                status = response.status
    except (aiohttp.ClientError, TimeoutError, ValueError) as error:
        raise GrokUnavailable(f"Grok sidecar unreachable: {type(error).__name__}") from error
    if status == 200 and isinstance(payload, dict):
        return payload
    detail = payload.get("error", "") if isinstance(payload, dict) else ""
    if status == 400:
        raise GrokBadRequest(f"Grok refused the request: {detail}")
    raise _STATUS.get(status, GrokUnavailable)(f"Grok HTTP {status}: {detail}")


async def models(config: Config) -> list[GrokModel]:
    """The models this plan offers with their effort menus, cached CATALOG_SECONDS; [] when the
    sidecar cannot say (Grok then simply is not offered)."""
    global _catalog
    fetched_at, cached = _catalog
    if cached and time.monotonic() - fetched_at < CATALOG_SECONDS:
        return cached
    try:
        payload = await _call(config, "GET", "/models", None, 90)
    except (GrokUnavailable, GrokBadRequest) as error:
        LOGGER.warning("Grok model catalog unavailable: %s", error)
        return cached
    parsed = [
        GrokModel(
            str(m.get("id")),
            str(m.get("name") or m.get("id")),
            tuple(str(e) for e in m.get("efforts") or []),
            str(m.get("default_effort") or ""),
        )
        for m in payload.get("models") or []
        if isinstance(m, dict) and m.get("id")
    ]
    if parsed:
        _catalog = (time.monotonic(), parsed)
    return parsed or cached


def cached_models() -> list[GrokModel]:
    return list(_catalog[1])


def cached_model(model_id: str) -> GrokModel | None:
    """From the last catalog read, without I/O (for choosing an effort synchronously)."""
    return next((m for m in _catalog[1] if m.id == model_id), None)


async def usage(config: Config) -> dict | None:
    """{"weekly_percent", "reset_at", "live"} of the plan, cached USAGE_SECONDS; None if the
    sidecar cannot read it (callers then do not gate on it)."""
    global _usage
    fetched_at, cached = _usage
    if cached is not None and time.monotonic() - fetched_at < USAGE_SECONDS:
        return cached
    try:
        reading = await _call(config, "GET", "/usage", None, 15)
    except (GrokUnavailable, GrokBadRequest) as error:
        LOGGER.info("Grok usage unavailable: %s", error)
        return cached
    _usage = (time.monotonic(), reading)
    return reading


def transcript_path(config: Config, thread_id: str) -> Path:
    if not THREAD_ID.fullmatch(thread_id):
        raise ValueError("not a Grok thread id")
    return config.grok_dir / f"{thread_id}.json"


def load_transcript(config: Config, thread_id: str) -> list[dict]:
    if not THREAD_ID.fullmatch(thread_id):
        return []
    try:
        data = json.loads(transcript_path(config, thread_id).read_text("utf-8"))
        messages = data.get("messages")
        return messages if isinstance(messages, list) else []
    except (OSError, ValueError):
        return []


def render_history(messages: Sequence[dict]) -> str:
    """Earlier turns as text: Grok takes one prompt per session, not a message list."""
    lines = []
    for message in messages:
        who = "成員" if message.get("role") == "user" else "前輩"
        lines.append(f"[{who}]\n{message_text(message.get('content'))}")
    return "\n\n".join(lines)


async def run_grok(
    user_prompt: str,
    config: Config,
    model: str,
    effort: str = "",
    images: Sequence[Path] = (),
    resume: str = "",
    memory: str = "",
    raw: bool = False,
    personal_style: str = "",
    schema: Path | None = None,
    plain: bool = False,
    links: str = "",
    help: str = "",
    files: str = "",
    on_delta=None,
) -> CodexResult:
    """One turn on Grok with the same contract as run_codex. Grok cannot see images (its
    headless mode reports image input unsupported): the caller routes such turns elsewhere."""
    if images:
        raise GrokUnavailable("Grok cannot read images")
    prompt = (
        user_prompt
        if raw
        else _prompt(user_prompt, memory, output_style(config), personal_style, links, help, files)
    )
    history = load_transcript(config, resume) if resume else []
    resumed = bool(history)
    thread_id = resume if resumed else THREAD_PREFIX + uuid.uuid4().hex[:16]
    kept = trim_history(history, config.grok_history_chars)
    full = (
        f"以下是先前的對話：\n\n{render_history(kept)}\n\n——\n\n現在的訊息：\n\n{prompt}"
        if kept
        else prompt
    )
    body = {
        "prompt": full,
        "system": _system_prompt(config, plain),
        "model": model,
        "effort": effort,
    }
    answer = await _call(config, "POST", "/chat", body, config.xsearch_timeout_seconds + 120)
    text = str(answer.get("text") or "").strip()
    if not text:
        raise GrokUnavailable("Grok returned no text")
    _save_transcript_to(config, thread_id, model, history, prompt, text)
    return CodexResult(text, (), None, thread_id, resumed)


def _save_transcript_to(
    config: Config, thread_id: str, model: str, history: list[dict], prompt: str, text: str
) -> None:
    messages = history + [
        {"role": "user", "content": prompt},
        {"role": "assistant", "content": text},
    ]
    path = transcript_path(config, thread_id)
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {"model": model, "at": time.time(), "messages": messages}
    path.write_text(json.dumps(payload, ensure_ascii=False), "utf-8")
