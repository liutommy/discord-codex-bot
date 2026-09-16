"""Member-visible embed fixing: swap a post link's host for a proxy that hands Discord's
crawler the OpenGraph tags the original site withholds -- video above all.

How a Discord preview works: Discord fetches the link with its own crawler and reads only
the ``og:`` / ``twitter:`` meta tags; a video plays inline only when ``og:video`` points at an
mp4. X, TikTok, Pixiv and Tumblr give that crawler little or nothing. A fixer proxy answers
the crawler with real tags built from the site's API and sends a human straight back to
the original site (302 or meta refresh), so nobody stays on the proxy.

Every rewrite is guarded live: the proxy page is fetched with the crawler's User-Agent and
the link is swapped only when that page actually carries a video or image tag. A proxy
that is down, blocked or serving an error page never replaces a working link; the
per-guild switch (``/<prefix>-embedfix``) turns the whole thing off without a rebuild.

PROXIES was verified live on 2026-09-16 (crawler UA vs. human UA, one real post each).
Sites without a working, redirecting proxy that beats the native preview are deliberately
absent: Instagram (ddinstagram / kkinstagram dead; instagramez sends humans to an ad
domain), Reddit (proxies add nothing over the native card), Bluesky (native already carries
video). Threads is vxthreads.com -- the .net domain from older lists refuses connections.
"""

from __future__ import annotations

import json
import logging
import re
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from urllib.parse import urlsplit, urlunsplit

import aiohttp

LOGGER = logging.getLogger(__name__)

CRAWLER_UA = "Mozilla/5.0 (compatible; Discordbot/2.0; +https://discordapp.com)"
CRAWLER_HEADERS = {"User-Agent": CRAWLER_UA}
FETCH_TIMEOUT = 6
# Proxy pages are a few KB; Pixiv's illust JSON runs to several hundred KB. The body is read
# to EOF in chunks: `StreamReader.read(n)` returns *up to* n bytes and can stop early, which
# truncated the JSON mid-string and blocked every Pixiv swap as "rating unknown" (2026-09-16).
MAX_BYTES = 2_000_000
# Pixiv's public illust endpoint answers logged-out with `xRestrict` (0 all ages, 1 R-18,
# 2 R-18G) -- verified 2026-09-16 on eight works. Discord blurs a spoilered link's embed, so
# an age-restricted work is delivered as ||link||; when the rating cannot be read the link is
# left alone (no preview beats an unblurred one).
PIXIV_AJAX = "https://www.pixiv.net/ajax/illust/{id}"
PIXIV_HEADERS = {"Referer": "https://www.pixiv.net/"}
_PIXIV_ID = re.compile(r"^/(?:en/)?artworks/(\d+)")
# X marks a post's media as sensitive; the vxtwitter API exposes that as `possibly_sensitive`
# (the fxtwitter API has no such field -- checked 2026-09-16). Its Cloudflare front answers a
# generic User-Agent with 403 and the crawler's with 200, hence CRAWLER_HEADERS on that call.
X_API = "https://api.vxtwitter.com{path}"
_X_STATUS = re.compile(r"^/[A-Za-z0-9_]{1,20}/status/\d+")


@dataclass(frozen=True)
class Rule:
    hosts: frozenset[str]  # without www. / mobile.
    path: re.Pattern[str]
    proxies: tuple[str, ...]  # tried in order; the first whose page carries media wins


PROXIES: tuple[Rule, ...] = (
    Rule(
        frozenset({"x.com", "twitter.com"}),
        re.compile(r"^/[A-Za-z0-9_]{1,20}/status/\d+"),
        ("fixupx.com", "vxtwitter.com"),
    ),
    Rule(
        frozenset({"tiktok.com"}),
        re.compile(r"^/@[^/]+/video/\d+|^/t/[A-Za-z0-9]+"),
        ("tnktok.com", "tiktxk.com"),
    ),
    Rule(
        frozenset({"threads.com", "threads.net"}),
        # Post links and the app's /share/<code> links (which vxthreads resolves to the post).
        re.compile(r"^/@[^/]+/post/[A-Za-z0-9_-]+|^/share/[A-Za-z0-9_-]+"),
        ("vxthreads.com",),
    ),
    Rule(frozenset({"pixiv.net"}), re.compile(r"^/(?:en/)?artworks/\d+"), ("phixiv.net",)),
    Rule(frozenset({"tumblr.com"}), re.compile(r"^/[A-Za-z0-9_-]+/\d{6,}"), ("tpmblr.com",)),
)
PROXY_HOSTS = frozenset(host for rule in PROXIES for host in rule.proxies)
MEDIA_TAGS = {
    "og:video",
    "og:video:url",
    "og:video:secure_url",
    "og:image",
    "og:image:url",
    "og:image:secure_url",
    "twitter:player",
    "twitter:player:stream",
    "twitter:image",
}
_META = re.compile(r"<meta\b([^>]*)>", re.IGNORECASE)
_ATTR = re.compile(r'([\w:-]+)\s*=\s*"([^"]*)"')


def _host(url: str) -> tuple[str, str]:
    parts = urlsplit(url)
    host = (parts.hostname or "").lower()
    for prefix in ("www.", "mobile.", "m."):
        host = host.removeprefix(prefix)
    return host, parts.path


def candidates(url: str) -> list[str]:
    """The proxy forms of `url`, in preference order; empty when the link is not a post on
    a supported site or is already a proxy link."""
    host, path = _host(url)
    if not host or host in PROXY_HOSTS:
        return []
    parts = urlsplit(url)
    for rule in PROXIES:
        if host in rule.hosts and rule.path.match(path):
            return [urlunsplit(parts._replace(netloc=proxy)) for proxy in rule.proxies]
    return []


def has_media(html: str) -> bool:
    """True when the page carries an OpenGraph / Twitter-card video or image -- attribute
    order is not fixed (vxtwitter writes content= before property=), so each tag is parsed."""
    for tag in _META.findall(html):
        attrs = {k.lower(): v for k, v in _ATTR.findall(tag)}
        name = (attrs.get("property") or attrs.get("name") or "").lower()
        if name in MEDIA_TAGS and len(attrs.get("content", "")) >= 5:
            return True
    return False


Fetch = Callable[[str, dict[str, str]], Awaitable[str | None]]


@dataclass(frozen=True)
class Fix:
    url: str
    spoiler: bool  # deliver as ||url|| so Discord blurs the preview


async def rating(url: str, fetch: Fetch) -> bool | None:
    """Whether the work behind `url` is age-restricted: False for sites without a rating,
    None when the site has one but it could not be read."""
    host, path = _host(url)
    if host == "pixiv.net" and (match := _PIXIV_ID.match(path)):
        text = await fetch(PIXIV_AJAX.format(id=match.group(1)), PIXIV_HEADERS)
        value = _json_path(text, "body", "xRestrict")
        restricted = None if value is None else int(value) >= 1
    elif host in ("x.com", "twitter.com") and (match := _X_STATUS.match(path)):
        text = await fetch(X_API.format(path=match.group(0)), CRAWLER_HEADERS)
        value = _json_path(text, "possibly_sensitive")
        restricted = None if value is None else bool(value)
    else:
        return False
    if restricted is None:
        LOGGER.info("embedfix: rating for %s unreadable; link left alone", url)
    return restricted


def _json_path(text: str | None, *keys: str):
    try:
        node = json.loads(text or "")
        for key in keys:
            node = node[key]
        return node
    except (ValueError, TypeError, KeyError):
        return None


async def pick(url: str, fetch: Fetch) -> Fix | None:
    """The first candidate proxy whose page (as the crawler sees it) carries media, with
    the spoiler flag from the site's rating; None when the caller should keep the link."""
    for candidate in candidates(url):
        html = await fetch(candidate, CRAWLER_HEADERS)
        if not html or not has_media(html):
            LOGGER.info("embedfix: %s has no media for the crawler; not used", candidate)
            continue
        restricted = await rating(url, fetch)
        if restricted is None:
            return None
        return Fix(candidate, restricted)
    return None


async def fetch_text(
    session: aiohttp.ClientSession, url: str, headers: dict[str, str]
) -> str | None:
    """The page body (crawler headers: as Discord's crawler would receive it). Any failure
    is None: a proxy that cannot be read is a proxy that must not replace a working link."""
    try:
        async with session.get(
            url,
            headers=headers,
            timeout=aiohttp.ClientTimeout(total=FETCH_TIMEOUT),
            allow_redirects=True,
        ) as response:
            if response.status != 200:
                return None
            body = bytearray()
            async for chunk in response.content.iter_chunked(65_536):
                body += chunk
                if len(body) > MAX_BYTES:
                    return None  # not a proxy page or a rating answer; do not guess
            return body.decode(response.charset or "utf-8", errors="replace")
    except (aiohttp.ClientError, TimeoutError, UnicodeError, OSError) as exc:
        LOGGER.info("embedfix: %s unreachable (%s)", url, type(exc).__name__)
        return None
