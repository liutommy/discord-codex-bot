"""Client for the X lookup sidecar (xsearch/server.py): one X post by id, an account's recent
posts, or whether an account exists — read through xAI's server-side X search on the operator's
Grok subscription. Everything that comes back is X content, i.e. untrusted text."""

from __future__ import annotations

import asyncio
import logging
import re
import time
from collections import deque

import aiohttp

from . import tags
from .config import Config

LOGGER = logging.getLogger(__name__)
POST_ID = re.compile(r"[0-9]{1,19}")  # fullmatch: ASCII digits only, below 2**63
HANDLE = re.compile(r"[A-Za-z0-9_]{1,15}")


# A model's request for X search, answered by the Bot for every backend (bot.py read loop).
XSEARCH_TAG = re.compile(r'<xsearch\s+query="([^"]{1,200})"\s*/?>(?:\s*</xsearch>)?')


def extract_xsearch_queries(answer: str) -> list[str]:
    seen: list[str] = []
    for query in tags.findall(XSEARCH_TAG, answer):
        query = " ".join(query.split())
        if query and query not in seen:
            seen.append(query)
    return seen


def render_search(query: str, posts: list[dict] | None, error: str = "") -> str:
    """The RESULT block for one <xsearch>; every post is X content read out by a model."""
    if posts is None:
        body = f"（X 搜尋無法使用：{error}）" if error else "（X 搜尋無法使用）"
    elif not posts:
        body = "（X 搜尋沒有找到相關貼文）"
    else:
        body = "\n\n".join(
            f"{post_text(post)}\nhttps://x.com/{post.get('author_handle', 'i')}/status/"
            f"{post.get('id', '')}"
            for post in posts
        )
    return f'<RESULT kind="xsearch" query="{query}">\n{body}\n</RESULT>'


def is_post_id(value: str) -> bool:
    return bool(POST_ID.fullmatch(value)) and int(value) < 1 << 63


# X links in chat fall back to a Grok session each, and members can paste many: at most this
# many per rolling hour from the link path (tracking has its own interval).
POST_LOOKUPS_PER_HOUR = 30
_post_lookups: deque[float] = deque()
MAX_QUERY_CHARS = 200  # the sidecar's own bound (xsearch/server.py)
# Distinct searches in one answer. Each is a Grok session on the operator's plan, shared with X
# tracking, and the tags may come from a page or post the model read rather than from the member:
# without a bound one question could spend hundreds of sessions. The most <web> searches seen in
# one answer on 2026-11-04 was 8.
MAX_SEARCHES_PER_ANSWER = 20


class XSearchError(RuntimeError):
    """The sidecar is off, unreachable, or could not answer."""


class XSearchBusy(XSearchError):
    """HTTP 429: another lookup held the sidecar past its queue wait. Nothing was spent."""

    busy = True  # what tracking checks, without importing this module


class Page(list):
    """Posts of one "recent" page. `full`: the sidecar's word on whether the model filled the
    page before its filtering; None from a sidecar built before it said so."""

    full: bool | None = None


def enabled(config: Config) -> bool:
    return bool(config.xsearch_url)


# The sidecar runs one lookup session at a time and turns away a request that waited past its
# own 30 s queue (429). This bot's lookups — several X links in one message, a tracking check —
# take turns here instead, within one deadline for the wait and the request
# together. A request is only sent with the time the sidecar may take to
# answer it (xsearch_sidecar_seconds) still left: once a session starts it runs to the end
# whether anyone waits or not, so one given up on early is quota spent for nothing. The rest of
# the deadline is the wait. For the same reason a turn is held until the sidecar has answered,
# even when the caller stopped waiting.
_TURN = asyncio.Semaphore(1)


async def _ask(config: Config, kind: str, body: dict) -> dict:
    if not config.xsearch_url:
        raise XSearchError("X lookup is not configured")
    started = time.monotonic()
    wait = max(0, config.xsearch_timeout_seconds - config.xsearch_sidecar_seconds)
    try:
        async with asyncio.timeout(wait):
            await _TURN.acquire()
    except TimeoutError:
        raise XSearchBusy(f"X lookup {kind} failed: still queued behind this bot's own") from None
    budget = config.xsearch_timeout_seconds - (time.monotonic() - started)
    request = asyncio.ensure_future(_post(config, kind, body, budget))
    request.add_done_callback(_end_turn)
    return await asyncio.shield(request)  # a caller cancelled here leaves the request running


def _end_turn(request: asyncio.Future) -> None:
    _TURN.release()
    if not request.cancelled():
        request.exception()  # retrieved: a request nobody waits for any more fails quietly


async def _post(config: Config, kind: str, body: dict, budget: float) -> dict:
    timeout = aiohttp.ClientTimeout(total=budget)
    try:
        async with aiohttp.ClientSession(timeout=timeout) as session:
            async with session.post(
                f"{config.xsearch_url.rstrip('/')}/x/{kind}", json=body
            ) as response:
                answer = await response.json(content_type=None)
                if response.status == 429:
                    raise XSearchBusy(f"X lookup {kind} failed: HTTP 429")
                if response.status != 200 or not isinstance(answer, dict):
                    raise XSearchError(f"X lookup {kind} failed: HTTP {response.status}")
                return answer
    except (aiohttp.ClientError, TimeoutError, ValueError) as error:
        raise XSearchError(f"X lookup {kind} failed: {type(error).__name__}") from error


async def fetch_post(config: Config, post_id: str) -> dict | None:
    if not is_post_id(post_id):
        raise ValueError("not an X post id")
    now = time.monotonic()
    while _post_lookups and now - _post_lookups[0] > 3600:
        _post_lookups.popleft()
    if len(_post_lookups) >= POST_LOOKUPS_PER_HOUR:
        raise XSearchError("X post lookups are rate limited")
    _post_lookups.append(now)
    answer = await _ask(config, "post", {"id": post_id})
    post = answer.get("post")
    return post if answer.get("found") and isinstance(post, dict) else None


async def recent_posts(
    config: Config, handle: str, since_id: str = "", limit: int = 10, until_id: str = ""
) -> Page:
    """Up to `limit` of the account's newest posts after `since_id` — and, paging back, before
    `until_id`. A sidecar built before until_id existed ignores it, so callers filter by id too."""
    ids = (since_id, until_id)
    if not HANDLE.fullmatch(handle) or any(i and not is_post_id(i) for i in ids):
        raise ValueError("not an X handle / post id")
    body = {"handle": handle, "since_id": since_id, "limit": limit}
    if until_id:
        body["until_id"] = until_id
    answer = await _ask(config, "recent", body)
    page = Page(post for post in answer.get("posts") or [] if isinstance(post, dict))
    page.full = answer["full"] if isinstance(answer.get("full"), bool) else None
    return page


async def search_posts(config: Config, query: str, limit: int = 8) -> list[dict]:
    """Posts X search finds about `query`, as the model in the sidecar read them out."""
    query = " ".join(query.split())
    if not 1 <= len(query) <= MAX_QUERY_CHARS:
        raise ValueError(f"an X search query is 1-{MAX_QUERY_CHARS} characters")
    answer = await _ask(config, "search", {"query": query, "limit": limit})
    return [post for post in answer.get("posts") or [] if isinstance(post, dict)]


async def lookup_user(config: Config, handle: str) -> dict | None:
    if not HANDLE.fullmatch(handle):
        raise ValueError("not an X handle")
    answer = await _ask(config, "user", {"handle": handle})
    return answer if answer.get("exists") else None


def post_text(post: dict, url_handle: str = "") -> str:
    """One post as the same kind of text block the fxtwitter path produces. Unlike fxtwitter's,
    every field here was read out by a model from X search results — a post can talk it into
    misreporting — so the block says so, and flags an author that differs from the link's."""
    flags = "（回覆）" if post.get("is_reply") else "（轉發）" if post.get("is_repost") else ""
    lines = [
        f"X 貼文 @{post.get('author_handle', '')}（{post.get('author_name', '')}）"
        f" {post.get('created_at', '')}{flags}",
        post.get("text") or "（無文字）",
    ]
    if post.get("quoted_text"):
        lines.append(f"引用：{post['quoted_text']}")
    if post.get("media"):
        lines.append(f"[媒體：{'、'.join(post['media'])}；未附圖]")
    counts = [
        f"{label} {post[key]}"
        for key, label in (("like_count", "讚"), ("repost_count", "轉發"), ("reply_count", "回覆"))
        if isinstance(post.get(key), int)
    ]
    if counts:
        lines.append(" · ".join(counts))
    author = str(post.get("author_handle", ""))
    if url_handle and author and author.lower() != url_handle.lower():
        lines.append(f"（注意：連結裡的帳號是 @{url_handle}，X 搜尋回報的作者是 @{author}）")
    lines.append("（經由 xAI X 搜尋取得；作者與內容由模型摘錄，未經驗證）")
    return "\n".join(lines)
