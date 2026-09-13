"""Web search as a Bot-side tool for every backend. The model asks with `<web query="…"/>`;
the Bot returns titles, URLs and snippets, and the model reads the promising ones with the
existing `<fetch>`. Providers are tried in order: a paid search API when a key is configured,
then the self-hosted SearXNG (free, unlimited, on the compose network)."""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass

import aiohttp

from .config import Config

LOGGER = logging.getLogger(__name__)
WEB_TAG = re.compile(r'<web\s+query="([^"]{1,300})"\s*/?>(?:\s*</web>)?')


@dataclass(frozen=True, slots=True)
class Hit:
    title: str
    url: str
    snippet: str


def extract_web_queries(answer: str) -> list[str]:
    seen: list[str] = []
    for query in WEB_TAG.findall(answer):
        query = query.strip()
        if query and query not in seen:
            seen.append(query)
    return seen


def _hits(items, title_key: str, url_key: str, snippet_key: str, limit: int) -> list[Hit]:
    out: list[Hit] = []
    for item in items or []:
        url = str(item.get(url_key) or "")
        if not url.startswith(("http://", "https://")):
            continue
        out.append(
            Hit(
                title=str(item.get(title_key) or url)[:200],
                url=url,
                snippet=re.sub(r"\s+", " ", str(item.get(snippet_key) or ""))[:300],
            )
        )
        if len(out) >= limit:
            break
    return out


async def _brave(query: str, config: Config, session: aiohttp.ClientSession) -> list[Hit]:
    """Brave Search API (the paid/keyed provider slot; swap the URL/keys for another vendor)."""
    async with session.get(
        "https://api.search.brave.com/res/v1/web/search",
        params={"q": query, "count": config.search_max_results, "search_lang": "zh-hant"},
        headers={"X-Subscription-Token": config.search_api_key, "Accept": "application/json"},
    ) as response:
        response.raise_for_status()
        payload = await response.json(content_type=None)
    results = (payload.get("web") or {}).get("results") or []
    return _hits(results, "title", "url", "description", config.search_max_results)


async def _searxng(query: str, config: Config, session: aiohttp.ClientSession) -> list[Hit]:
    async with session.get(
        f"{config.search_url.rstrip('/')}/search",
        params={"q": query, "format": "json", "language": "zh-TW", "safesearch": 0},
    ) as response:
        response.raise_for_status()
        payload = await response.json(content_type=None)
    return _hits(payload.get("results"), "title", "url", "content", config.search_max_results)


def providers(config: Config) -> list[tuple[str, object]]:
    """(name, coroutine function) in the order to try: keyed API first, SearXNG as fallback."""
    chain: list[tuple[str, object]] = []
    if config.search_api_key and config.search_api == "brave":
        chain.append(("Brave", _brave))
    if config.search_url:
        chain.append(("SearXNG", _searxng))
    return chain


def available(config: Config) -> bool:
    return bool(providers(config))


async def search_web(query: str, config: Config) -> tuple[str, list[Hit]]:
    """(provider name, hits) from the first provider that answers; ("", []) when none did.
    Plain session: SearXNG is on the private compose network by design, and the API host is a
    fixed public vendor — neither is member-supplied, so the SSRF guard does not apply."""
    timeout = aiohttp.ClientTimeout(total=config.link_timeout_seconds)
    async with aiohttp.ClientSession(timeout=timeout) as session:
        for name, provider in providers(config):
            try:
                hits = await provider(query, config, session)
            except (aiohttp.ClientError, TimeoutError, ValueError) as error:
                LOGGER.warning("%s search failed for %r: %s", name, query, type(error).__name__)
                continue
            if hits:
                return name, hits
    return "", []


def render_results(query: str, provider: str, hits: list[Hit]) -> str:
    """The RESULT block handed back to the model (untrusted, like everything fetched)."""
    if not hits:
        empty = "（沒有搜尋結果，或搜尋服務暫時不可用）"
        return f'<RESULT kind="web" query="{query}">\n{empty}\n</RESULT>'
    lines = [f"{i}. {h.title} — {h.url}" + (f"\n   {h.snippet}" if h.snippet else "")
             for i, h in enumerate(hits, start=1)]
    body = "\n".join(lines)
    return f'<RESULT kind="web" query="{query}" via="{provider}">\n{body}\n</RESULT>'
