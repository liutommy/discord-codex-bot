"""Client for the X lookup sidecar (xsearch/server.py): one X post by id, an account's recent
posts, or whether an account exists — read through xAI's server-side X search on the operator's
Grok subscription. Everything that comes back is X content, i.e. untrusted text."""

from __future__ import annotations

import logging
import re

import aiohttp

from .config import Config

LOGGER = logging.getLogger(__name__)
POST_ID = re.compile(r"^[0-9]{1,20}$")
HANDLE = re.compile(r"^[A-Za-z0-9_]{1,15}$")


class XSearchError(RuntimeError):
    """The sidecar is off, unreachable, or could not answer."""


def enabled(config: Config) -> bool:
    return bool(config.xsearch_url)


async def _ask(config: Config, kind: str, body: dict) -> dict:
    if not config.xsearch_url:
        raise XSearchError("X lookup is not configured")
    timeout = aiohttp.ClientTimeout(total=config.xsearch_timeout_seconds)
    try:
        async with aiohttp.ClientSession(timeout=timeout) as session:
            async with session.post(
                f"{config.xsearch_url.rstrip('/')}/x/{kind}", json=body
            ) as response:
                answer = await response.json(content_type=None)
                if response.status != 200 or not isinstance(answer, dict):
                    raise XSearchError(f"X lookup {kind} failed: HTTP {response.status}")
                return answer
    except (aiohttp.ClientError, TimeoutError, ValueError) as error:
        raise XSearchError(f"X lookup {kind} failed: {type(error).__name__}") from error


async def fetch_post(config: Config, post_id: str) -> dict | None:
    if not POST_ID.match(post_id):
        raise ValueError("not an X post id")
    answer = await _ask(config, "post", {"id": post_id})
    post = answer.get("post")
    return post if answer.get("found") and isinstance(post, dict) else None


async def recent_posts(
    config: Config, handle: str, since_id: str = "", limit: int = 10
) -> list[dict]:
    if not HANDLE.match(handle) or (since_id and not POST_ID.match(since_id)):
        raise ValueError("not an X handle / post id")
    answer = await _ask(config, "recent", {"handle": handle, "since_id": since_id, "limit": limit})
    return [post for post in answer.get("posts") or [] if isinstance(post, dict)]


async def lookup_user(config: Config, handle: str) -> dict | None:
    if not HANDLE.match(handle):
        raise ValueError("not an X handle")
    answer = await _ask(config, "user", {"handle": handle})
    return answer if answer.get("exists") else None


def post_text(post: dict) -> str:
    """One post as the same kind of text block the fxtwitter path produces."""
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
    lines.append("（經由 xAI X 搜尋取得）")
    return "\n".join(lines)
