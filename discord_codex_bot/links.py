from __future__ import annotations

import asyncio
import ipaddress
import logging
import os
import re
import socket
from html.parser import HTMLParser
from pathlib import Path
from urllib.parse import urlsplit

import aiohttp
from aiohttp.resolver import ThreadedResolver

from .config import Config

LOGGER = logging.getLogger(__name__)
URL_RE = re.compile(r"https?://[^\s<>()\[\]\"'`]+")
FETCH_TAG = re.compile(
    r'<fetch\s+url="(https?://[^"]{1,2000})"(?:\s+render="([^"]*)")?\s*/?>(?:\s*</fetch>)?'
)
BLOCKED_MARKERS = ("被網站的機器人驗證擋住", "HTTP 403", "HTTP 429", "頁面沒有可讀文字", "HTTP 503")
USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/128.0 Safari/537.36"
)
_SKIP = {"script", "style", "noscript", "template", "svg", "head"}
_BLOCK = {"p", "div", "br", "li", "tr", "h1", "h2", "h3", "h4", "h5", "h6", "section", "article",
          "pre", "blockquote", "td", "th", "dt", "dd", "hr", "table", "ul", "ol"}


def find_urls(text: str, limit: int) -> list[str]:
    seen: list[str] = []
    for match in URL_RE.findall(text):
        url = match.rstrip(".,;:!?。，、」』）")
        if url not in seen:
            seen.append(url)
        if len(seen) >= limit:
            break
    return seen


def extract_fetch_tags(answer: str) -> list[tuple[str, bool]]:
    """[(url, render)] — render=True when the model asked for the page as rendered (screenshot)."""
    out: dict[str, bool] = {}
    for url, render in FETCH_TAG.findall(answer):
        out[url] = out.get(url, False) or render.strip().lower() in ("1", "true", "yes")
    return list(out.items())


class _Text(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []
        self.title = ""
        self._skip = 0
        self._in_title = False

    def handle_starttag(self, tag, attrs):
        if tag in _SKIP:
            self._skip += 1
        elif tag == "title":
            self._in_title = True
        elif tag in _BLOCK:
            self.parts.append("\n")

    def handle_endtag(self, tag):
        if tag in _SKIP and self._skip:
            self._skip -= 1
        elif tag == "title":
            self._in_title = False
        elif tag in _BLOCK:
            self.parts.append("\n")

    def handle_data(self, data):
        if self._in_title:
            self.title += data
        elif not self._skip:
            self.parts.append(data)


def html_to_text(html: str) -> tuple[str, str]:
    parser = _Text()
    parser.feed(html)
    text = "".join(parser.parts)
    text = re.sub(r"[ \t\r\f\v]+", " ", text)
    text = re.sub(r"\n\s*\n+", "\n\n", text).strip()
    return parser.title.strip(), text


def _public_address(ip: str) -> bool:
    address = ipaddress.ip_address(ip)
    return not (
        address.is_private
        or address.is_loopback
        or address.is_link_local
        or address.is_multicast
        or address.is_reserved
        or address.is_unspecified
    )


class _PublicResolver(ThreadedResolver):
    """aiohttp resolver that refuses non-public answers at connect time. Every redirect hop and
    every re-resolution goes through it, so a redirect to a LAN host or a DNS answer that changes
    between check and connect (rebinding) is refused where it would otherwise be used."""

    async def resolve(self, host, port=0, family=socket.AF_INET):
        results = await super().resolve(host, port, family)
        if not results or not all(_public_address(result["host"]) for result in results):
            raise socket.gaierror(f"{host} resolves to a private or reserved address")
        return results


async def _resolve_public(host: str) -> str:
    """Resolve `host` and return one address only if every answer is a public IP."""
    infos = await asyncio.get_running_loop().getaddrinfo(host, None, type=socket.SOCK_STREAM)
    addresses = {info[4][0] for info in infos}
    if not addresses or not all(_public_address(ip) for ip in addresses):
        raise ValueError("host resolves to a private or reserved address")
    return sorted(addresses)[0]


async def fetch_link(url: str, config: Config) -> str:
    """Fetch one http(s) URL into bounded plain text; returns a user-readable failure otherwise.

    Guarded: public addresses only (no LAN/loopback), bounded size, time and redirects. The
    content is untrusted member/web input and is labelled as such when injected.
    """
    parts = urlsplit(url)
    if parts.scheme not in ("http", "https") or not parts.hostname:
        return f"（{url}：只支援 http/https）"
    try:
        await _resolve_public(parts.hostname)
    except (ValueError, socket.gaierror) as error:
        return f"（{url}：無法連線——{error}）"
    timeout = aiohttp.ClientTimeout(total=config.link_timeout_seconds)
    headers = {"User-Agent": USER_AGENT, "Accept-Language": "zh-TW,zh;q=0.9,en;q=0.8"}
    try:
        connector = aiohttp.TCPConnector(resolver=_PublicResolver(), use_dns_cache=False)
        async with aiohttp.ClientSession(
            timeout=timeout, headers=headers, connector=connector
        ) as session:
            async with session.get(url, max_redirects=5, allow_redirects=True) as response:
                if response.status >= 400:
                    return f"（{url}：HTTP {response.status}，打不開）"
                content_type = response.headers.get("Content-Type", "")
                raw = await response.content.read(config.link_max_bytes)
    except (aiohttp.ClientError, TimeoutError) as error:
        return f"（{url}：抓取失敗——{type(error).__name__}）"
    body = raw.decode(response.charset or "utf-8", errors="replace")
    if "html" in content_type:
        title, text = html_to_text(body)
        if _challenge(title):
            return f"（{url}：被網站的機器人驗證擋住，打不開）"
    elif content_type.startswith("text/") or "json" in content_type or "xml" in content_type:
        title, text = "", body.strip()
    else:
        return f"（{url}：不是文字內容（{content_type.split(';')[0] or '未知'}），略過）"
    if not text:
        return f"（{url}：頁面沒有可讀文字）"
    limit = config.link_max_chars
    clipped = text if len(text) <= limit else f"{text[:limit]}\n[已截斷至 {limit} 字]"
    head = f"標題：{title}\n" if title else ""
    return f"{head}{clipped}"


async def link_blocks(
    urls: list[str], config: Config, out_dir: Path | None = None
) -> tuple[str, list[Path]]:
    """Fetch `urls` (plain first, Chromium fallback) into untrusted <LINK> blocks plus any page
    screenshots the fallback produced, to be attached as images."""
    if not urls:
        return "", []
    results = await asyncio.gather(
        *(fetch_or_render(url, config, out_dir / f"link{i}" if out_dir else None)
          for i, url in enumerate(urls))
    )
    pairs = zip(urls, results, strict=True)
    blocks = [f'<LINK url="{url}">\n{text}\n</LINK>' for url, (text, _) in pairs]
    shots = [shot for _, shot in results if shot is not None]
    return "\n\n".join(blocks), shots


def _challenge(title: str) -> bool:
    return "just a moment" in title.lower() or "請稍候" in title


async def _guard_route(route, request, hosts: dict[str, bool]) -> None:
    """Chromium request hook: every request the page makes — navigation, redirect hop, script,
    image, fetch() from page JS — is allowed only towards a public address."""
    parts = urlsplit(request.url)
    host = parts.hostname
    if parts.scheme not in ("http", "https") or not host:
        await route.abort("blockedbyclient")
        return
    if host not in hosts:
        try:
            await _resolve_public(host)
            hosts[host] = True
        except (ValueError, socket.gaierror):
            hosts[host] = False
    await (route.continue_() if hosts[host] else route.abort("blockedbyclient"))


async def _render(url: str, config: Config, out_dir: Path | None) -> tuple[str, str, Path | None]:
    """(title, text, screenshot) of `url` rendered in headless Chromium; the caller bounds time."""
    from playwright.async_api import async_playwright

    deadline = config.link_render_timeout_seconds
    async with async_playwright() as pw:
        # Full Chromium (not the headless shell) passes bot challenges the shell fails; it needs
        # a writable HOME and no zygote inside the read-only, cap-dropped container.
        scratch = str(out_dir.parent if out_dir else Path("/tmp"))
        browser = await pw.chromium.launch(
            headless=True,
            channel="chromium",
            env={"HOME": scratch, "XDG_CONFIG_HOME": f"{scratch}/.config",
                 "XDG_CACHE_HOME": f"{scratch}/.cache", "PATH": os.environ.get("PATH", "")},
            args=["--disable-blink-features=AutomationControlled", "--no-sandbox",
                  "--disable-setuid-sandbox", "--no-zygote", "--disable-dev-shm-usage",
                  "--disable-gpu", "--headless=new"],
        )
        try:
            # The browser's own User-Agent minus the "Headless" token: a foreign UA contradicts
            # the TLS/JS fingerprint (Cloudflare never clears its challenge), while the literal
            # "HeadlessChrome" is what X and Dcard refuse outright.
            major = browser.version.split(".")[0]
            user_agent = (
                "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) "
                f"Chrome/{major}.0.0.0 Safari/537.36"
            )
            context = await browser.new_context(
                user_agent=user_agent, locale="zh-TW", viewport={"width": 1280, "height": 900}
            )
            hosts: dict[str, bool] = {}
            await context.route("**/*", lambda route, request: _guard_route(route, request, hosts))
            await context.add_init_script(
                "Object.defineProperty(navigator,'webdriver',{get:()=>undefined});"
                "window.chrome={runtime:{}};"
                "Object.defineProperty(navigator,'languages',{get:()=>['zh-TW','zh','en']});"
                "Object.defineProperty(navigator,'plugins',{get:()=>[1,2,3]});"
            )
            page = await context.new_page()
            await page.goto(url, wait_until="domcontentloaded", timeout=deadline * 1000)
            for _ in range(max(1, deadline // 3)):  # the caller's wait_for bounds the total
                if not _challenge(await page.title()):
                    break
                await asyncio.sleep(3)
            try:
                await page.wait_for_load_state("networkidle", timeout=8000)
            except Exception:
                pass
            title = await page.title()
            text = await page.evaluate("() => document.body ? document.body.innerText : ''")
            shot = None
            if out_dir is not None:
                await asyncio.to_thread(out_dir.mkdir, parents=True, exist_ok=True)
                shot = out_dir / "page.jpg"
                height = min(
                    await page.evaluate("() => document.documentElement.scrollHeight"),
                    config.link_screenshot_max_height,
                )
                await page.screenshot(
                    path=str(shot), type="jpeg", quality=80,
                    clip={"x": 0, "y": 0, "width": 1280, "height": max(300, int(height))},
                    full_page=True,
                )
            return title, text or "", shot
        finally:
            await browser.close()


# One Chromium at a time: the container's pids_limit is sized for a single instance.
_RENDER_SLOT = asyncio.Semaphore(1)


async def render_link(url: str, config: Config, out_dir: Path | None) -> tuple[str, Path | None]:
    """Fetch through headless Chromium (anti-automation tweaks, waits out bot challenges) and
    return (text, screenshot path). Used as the fallback for pages plain HTTP cannot read and
    whenever the model asks for the rendered page. Same public-address guard as fetch_link,
    applied to every request the page makes."""
    parts = urlsplit(url)
    if parts.scheme not in ("http", "https") or not parts.hostname:
        return f"（{url}：只支援 http/https）", None
    try:
        await _resolve_public(parts.hostname)
    except (ValueError, socket.gaierror) as error:
        return f"（{url}：無法連線——{error}）", None
    try:
        async with _RENDER_SLOT:
            title, text, shot = await asyncio.wait_for(
                _render(url, config, out_dir), timeout=config.link_render_timeout_seconds
            )
    except ImportError:
        return f"（{url}：此部署沒有 Chromium，無法渲染）", None
    except TimeoutError:
        return f"（{url}：渲染逾時，打不開）", None
    except Exception as error:  # playwright raises many distinct types; all mean "not readable"
        LOGGER.warning("render_link %s failed: %s", url, type(error).__name__)
        return f"（{url}：渲染失敗——{type(error).__name__}）", None
    if _challenge(title):
        return f"（{url}：機器人驗證沒過，打不開）", None
    text = re.sub(r"[ \t\r\f\v]+", " ", text)
    text = re.sub(r"\n\s*\n+", "\n\n", text).strip()
    limit = config.link_max_chars
    clipped = text if len(text) <= limit else f"{text[:limit]}\n[已截斷至 {limit} 字]"
    head = f"標題：{title}\n" if title else ""
    note = "（整頁截圖已附上）\n" if shot else ""
    return f"{head}{note}{clipped or '（頁面沒有可讀文字，請看截圖）'}", shot

def blocked(result: str) -> bool:
    return any(marker in result for marker in BLOCKED_MARKERS)


async def fetch_or_render(
    url: str, config: Config, out_dir: Path | None, render: bool = False
) -> tuple[str, Path | None]:
    """Plain fetch first; Chromium when asked (render) or when plain fetch cannot read the page."""
    if not render:
        text = await fetch_link(url, config)
        if not blocked(text):
            return text, None
    return await render_link(url, config, out_dir)
