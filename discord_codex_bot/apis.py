"""Registered data APIs the model can call with `<api name="…" path="…"/>`. The operator lists
them in apis.json (base URL, fixed headers — `${ENV}` in a header value is read from the
environment so keys stay out of the file — and a one-paragraph usage note the model sees).
The Bot makes the GET with the headers and hands the bounded response back as a RESULT block.
Only paths relative to the registered base are allowed: the model can pick endpoints and query
strings, never hosts."""

from __future__ import annotations

import html
import json
import logging
import os
import re
from dataclasses import dataclass, field
from pathlib import Path

import aiohttp

from .config import Config
from .links import _read_bounded

LOGGER = logging.getLogger(__name__)
API_TAG = re.compile(
    r'<api\s+name="([a-z0-9_-]{1,40})"\s+path="([^"]{1,2000})"\s*/?>(?:\s*</api>)?'
)
MAX_CALLS_PER_ROUND = 2
_ENV = re.compile(r"\$\{([A-Z0-9_]+)\}")


@dataclass(frozen=True, slots=True)
class Api:
    name: str
    base: str
    headers: dict[str, str] = field(default_factory=dict)
    doc: str = ""


def _expand(value: str) -> str:
    return _ENV.sub(lambda m: os.environ.get(m.group(1), ""), value)


def load_registry(path: Path | None) -> dict[str, Api]:
    """apis.json → {name: Api}; unreadable or malformed files register nothing (logged)."""
    if path is None:
        return {}
    try:
        data = json.loads(path.read_text("utf-8"))
    except (OSError, ValueError) as error:
        LOGGER.warning("apis registry %s unreadable: %s", path, type(error).__name__)
        return {}
    out: dict[str, Api] = {}
    for name, spec in (data or {}).items():
        if not isinstance(spec, dict) or not str(spec.get("base", "")).startswith("https://"):
            LOGGER.warning("api %r skipped: needs an https base", name)
            continue
        headers = {str(k): _expand(str(v)) for k, v in (spec.get("headers") or {}).items()}
        out[str(name)] = Api(str(name), str(spec["base"]), headers, str(spec.get("doc") or ""))
    return out


def extract_api_calls(answer: str) -> list[tuple[str, str]]:
    seen: list[tuple[str, str]] = []
    for name, path in API_TAG.findall(answer):
        if (name, path) not in seen:
            seen.append((name, path))
    return seen[:MAX_CALLS_PER_ROUND]


def render_doc(registry: dict[str, Api]) -> str:
    """The model-facing list of APIs and how to use them (goes into the HELP block)."""
    if not registry:
        return ""
    lines = ['可呼叫的資料 API（回覆只放 <api name="…" path="…"/> 即可，Bot 代打並回傳結果）：']
    lines += [f"- {api.name}：{api.doc}" for api in registry.values()]
    return "\n".join(lines)


def _bounded(text: str, limit: int) -> str:
    return text if len(text) <= limit else f"{text[:limit]}\n[已截斷至 {limit} 字]"


async def call_api(name: str, path: str, registry: dict[str, Api], config: Config) -> str:
    """The response body (JSON compacted) for one registered call, or a user-readable failure."""
    api = registry.get(name)
    if api is None:
        return f"（沒有叫 {name} 的 API；可用：{'、'.join(registry) or '無'}）"
    path = html.unescape(path)  # models tend to write &amp; inside tag attributes
    if "://" in path or path.startswith("//") or ".." in path:
        return "（path 只能是相對於該 API 的端點與查詢字串）"
    url = api.base + path.lstrip("/")
    timeout = aiohttp.ClientTimeout(total=config.link_timeout_seconds)
    try:
        async with aiohttp.ClientSession(timeout=timeout, headers=api.headers) as session:
            async with session.get(url) as response:
                raw = await _read_bounded(response, config.link_max_bytes)
                status = response.status
                content_type = response.content_type
    except (aiohttp.ClientError, TimeoutError) as error:
        return f"（{name} 呼叫失敗——{type(error).__name__}）"
    body = raw.decode("utf-8", errors="replace")
    if status >= 400:
        return f"（{name} 回 HTTP {status}：{_bounded(body, 300)}）"
    if "json" in content_type or body.lstrip().startswith(("{", "[")):
        try:
            body = json.dumps(json.loads(body), ensure_ascii=False, separators=(",", ":"))
        except ValueError:
            pass
    return _bounded(body.strip() or "（空回應）", config.apis_max_chars)


def render_result(name: str, path: str, body: str) -> str:
    return f'<RESULT kind="api" name="{name}" path="{path}">\n{body}\n</RESULT>'
