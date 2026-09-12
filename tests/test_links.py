from __future__ import annotations

import socket
from dataclasses import replace
from pathlib import Path

import pytest

from discord_codex_bot import links
from discord_codex_bot.config import Config
from discord_codex_bot.links import (
    _public_address,
    blocked,
    extract_fetch_tags,
    fetch_link,
    fetch_or_render,
    find_urls,
    html_to_text,
    link_blocks,
    render_link,
)


def test_find_urls_dedupes_strips_punctuation_and_caps() -> None:
    text = "看 https://a.example/x。 和 https://a.example/x 還有 (https://b.example/y) http://c.example/"
    assert find_urls(text, 3) == ["https://a.example/x", "https://b.example/y", "http://c.example/"]
    assert find_urls(text, 1) == ["https://a.example/x"]
    assert find_urls("no links", 3) == []


def test_extract_fetch_tags_reads_render_attribute_and_merges_duplicates() -> None:
    answer = (
        '<fetch url="https://a.example/p"/> <fetch url="https://b.example/q" render="1"/>'
        ' <fetch url="https://a.example/p" render="true"></fetch> <fetch url="ftp://x"/>'
    )
    assert extract_fetch_tags(answer) == [
        ("https://a.example/p", True), ("https://b.example/q", True)
    ]
    assert extract_fetch_tags('<fetch url="https://c.example" render="0"/>') == [
        ("https://c.example", False)
    ]
    assert extract_fetch_tags("plain answer") == []


def test_html_to_text_drops_scripts_keeps_title_and_block_breaks() -> None:
    html = (
        "<html><head><title> T </title><style>p{}</style></head><body><script>x()</script>"
        "<p>one</p><div>two &amp; three</div><noscript>hidden</noscript></body></html>"
    )
    assert html_to_text(html) == ("T", "one\n\ntwo & three")


@pytest.mark.parametrize(
    ("ip", "public"),
    [("8.8.8.8", True), ("10.0.0.1", False), ("127.0.0.1", False), ("169.254.1.1", False),
     ("192.168.1.1", False), ("::1", False), ("fe80::1", False), ("2606:4700::1", True),
     ("0.0.0.0", False), ("224.0.0.1", False)],
)
def test_public_address_guard(ip: str, public: bool) -> None:
    assert _public_address(ip) is public


@pytest.mark.asyncio
async def test_fetch_link_refuses_private_hosts_and_bad_schemes(
    monkeypatch, config: Config
) -> None:
    async def resolve_private(host: str) -> str:
        raise ValueError("host resolves to a private or reserved address")

    monkeypatch.setattr(links, "_resolve_public", resolve_private)
    assert "無法連線" in await fetch_link("http://internal.example/", config)
    assert "只支援 http/https" in await fetch_link("ftp://a.example/", config)
    assert "只支援 http/https" in (await render_link("file:///etc/passwd", config, None))[0]

    async def resolve_fail(host: str) -> str:
        raise socket.gaierror("no such host")

    monkeypatch.setattr(links, "_resolve_public", resolve_fail)
    assert "無法連線" in await fetch_link("https://nope.invalid/", config)
    assert (await render_link("https://nope.invalid/", config, None))[1] is None


def test_blocked_matches_only_failure_markers() -> None:
    assert blocked("（u：被網站的機器人驗證擋住，打不開）")
    assert blocked("（u：HTTP 403，打不開）")
    assert blocked("（u：頁面沒有可讀文字）")
    assert not blocked("標題：ok\nbody")
    assert not blocked("（u：不是文字內容（image/png），略過）")


@pytest.mark.asyncio
async def test_fetch_or_render_falls_back_only_when_blocked_or_asked(
    monkeypatch, config: Config, tmp_path: Path
) -> None:
    calls: list[str] = []

    async def fake_fetch(url: str, config: Config) -> str:
        calls.append(f"fetch {url}")
        return "（u：HTTP 403，打不開）" if "blocked" in url else "text"

    async def fake_render(url: str, config: Config, out_dir):
        calls.append(f"render {url}")
        return "rendered", tmp_path / "page.jpg"

    monkeypatch.setattr(links, "fetch_link", fake_fetch)
    monkeypatch.setattr(links, "render_link", fake_render)
    assert await fetch_or_render("https://ok.example", config, tmp_path) == ("text", None)
    assert await fetch_or_render("https://blocked.example", config, tmp_path) == (
        "rendered", tmp_path / "page.jpg"
    )
    assert await fetch_or_render("https://ok.example", config, tmp_path, render=True) == (
        "rendered", tmp_path / "page.jpg"
    )
    assert calls == [
        "fetch https://ok.example", "fetch https://blocked.example",
        "render https://blocked.example", "render https://ok.example",
    ]


@pytest.mark.asyncio
async def test_link_blocks_wraps_each_page_and_collects_screenshots(
    monkeypatch, config: Config, tmp_path: Path
) -> None:
    async def fake(url: str, config: Config, out_dir, render: bool = False):
        shot = out_dir / "page.jpg" if "shot" in url else None
        return f"body of {url}", shot

    monkeypatch.setattr(links, "fetch_or_render", fake)
    assert await link_blocks([], config, tmp_path) == ("", [])
    text, shots = await link_blocks(["https://a.example", "https://shot.example"], config, tmp_path)
    assert text == (
        '<LINK url="https://a.example">\nbody of https://a.example\n</LINK>\n\n'
        '<LINK url="https://shot.example">\nbody of https://shot.example\n</LINK>'
    )
    assert shots == [tmp_path / "link1" / "page.jpg"]


@pytest.mark.asyncio
async def test_fetch_link_clips_text_and_reports_bot_challenge(monkeypatch, config: Config) -> None:
    class Response:
        def __init__(self, body: str, status: int = 200, ctype: str = "text/html") -> None:
            self.status, self.headers, self.charset = status, {"Content-Type": ctype}, "utf-8"
            self.content = self
            self._body = body.encode("utf-8")

        async def read(self, n: int) -> bytes:
            return self._body[:n]

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            return False

    class Session:
        response: Response

        def __init__(self, **kwargs) -> None:
            pass

        def get(self, url: str, **kwargs):
            return Session.response

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            return False

    async def resolve_ok(host: str) -> str:
        return "93.184.216.34"

    monkeypatch.setattr(links, "_resolve_public", resolve_ok)
    monkeypatch.setattr(links.aiohttp, "ClientSession", Session)
    config = replace(config, link_max_chars=10)
    Session.response = Response("<title>Just a moment...</title><p>checking</p>")
    assert blocked(await fetch_link("https://cf.example/", config))
    Session.response = Response("<title>T</title><p>" + "x" * 50 + "</p>")
    out = await fetch_link("https://ok.example/", config)
    assert out == "標題：T\n" + "x" * 10 + "\n[已截斷至 10 字]"
    Session.response = Response("", status=404)
    assert "HTTP 404" in await fetch_link("https://gone.example/", config)
    Session.response = Response("\x89PNG", ctype="image/png")
    assert "不是文字內容（image/png）" in await fetch_link("https://img.example/", config)
    Session.response = Response('{"a":1}', ctype="application/json")
    assert await fetch_link("https://api.example/", config) == '{"a":1}'
