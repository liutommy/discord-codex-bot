"""Registered data APIs the model can call with `<api name="…" path="…"/>`. The operator lists
them in apis.json (base URL, fixed headers — `${ENV}` in a header value is read from the
environment so keys stay out of the file — and a one-paragraph usage note the model sees).
The Bot makes the GET with the headers and hands the bounded response back as a RESULT block.
Only paths relative to the registered base are allowed: the model can pick endpoints and query
strings, never hosts."""

from __future__ import annotations

import asyncio
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
# Some APIs report throttling *inside* a 200 response (MediaWiki answers a rate-limited query
# with HTTP 200 and {"error":{"code":"ratelimited"}}), so the status code alone cannot be trusted.
RATE_LIMIT_CODES = {"ratelimited", "rate_limited", "toomanyrequests"}
RETRY_AFTER_SECONDS = 3


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


def _api_error(body: str) -> tuple[str, str]:
    """("code", "info") when the body carries an API-level error the HTTP status did not report,
    else ("", ""). Without this a throttled MediaWiki reply reaches the model as if it were data."""
    try:
        payload = json.loads(body)
    except ValueError:
        return "", ""
    error = payload.get("error") if isinstance(payload, dict) else None
    if not isinstance(error, dict):
        return "", ""
    return str(error.get("code") or "unknown"), str(error.get("info") or "")[:200]


async def _get(url: str, api: Api, config: Config) -> tuple[int, str, str]:
    timeout = aiohttp.ClientTimeout(total=config.link_timeout_seconds)
    async with aiohttp.ClientSession(timeout=timeout, headers=api.headers) as session:
        async with session.get(url) as response:
            raw = await _read_bounded(response, config.link_max_bytes)
            return response.status, response.content_type, raw.decode("utf-8", errors="replace")


async def call_api(name: str, path: str, registry: dict[str, Api], config: Config) -> str:
    """The response body (JSON compacted) for one registered call, or a user-readable failure.
    A throttled source is retried once and then reported as throttling, not as an empty result:
    the model was answering "no record found" to what was really "come back in a minute"."""
    api = registry.get(name)
    if api is None:
        return f"（沒有叫 {name} 的 API；可用：{'、'.join(registry) or '無'}）"
    path = html.unescape(path)  # models tend to write &amp; inside tag attributes
    if "://" in path or path.startswith("//") or ".." in path:
        return "（path 只能是相對於該 API 的端點與查詢字串）"
    url = api.base + path.lstrip("/")
    body = ""
    for attempt in (1, 2):
        try:
            status, content_type, body = await _get(url, api, config)
        except (aiohttp.ClientError, TimeoutError) as error:
            return f"（{name} 呼叫失敗——{type(error).__name__}）"
        code, info = _api_error(body)
        throttled = status == 429 or code in RATE_LIMIT_CODES
        if throttled and attempt == 1:
            LOGGER.info("%s throttled (%s); one retry in %ss", name, code or status,
                        RETRY_AFTER_SECONDS)
            await asyncio.sleep(RETRY_AFTER_SECONDS)
            continue
        if throttled:
            return (f"（{name} 被限流，已自動重試一次仍被擋。**這不是查無資料**：同一條查詢稍後"
                    f"會成功。不要改寫成「沒有紀錄」，也不要換到查不到這類資料的來源硬答；"
                    f"告訴成員稍後再問即可。{info}）")
        if status >= 400:
            return f"（{name} 回 HTTP {status}：{_bounded(body, 300)}）"
        if code:
            return f"（{name} 回報錯誤 {code}：{info}）"
        if "json" in content_type or body.lstrip().startswith(("{", "[")):
            try:
                body = json.dumps(json.loads(body), ensure_ascii=False, separators=(",", ":"))
            except ValueError:
                pass
        return _bounded(body.strip() or "（空回應）", config.apis_max_chars)
    return f"（{name} 被限流）"  # unreachable: both attempts return above


def render_result(name: str, path: str, body: str) -> str:
    return f'<RESULT kind="api" name="{name}" path="{path}">\n{body}\n</RESULT>'
