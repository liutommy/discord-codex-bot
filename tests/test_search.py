from __future__ import annotations

from dataclasses import replace

from discord_codex_bot import search
from discord_codex_bot.search import Hit, extract_web_queries, render_results, search_web


class Response:
    def __init__(self, payload, status=200):
        self.payload, self.status = payload, status

    def raise_for_status(self):
        if self.status >= 400:
            raise search.aiohttp.ClientResponseError(None, (), status=self.status)

    async def json(self, content_type=None):
        return self.payload

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        return False


class Session:
    def __init__(self, by_host: dict):
        self.by_host, self.calls = by_host, []

    def get(self, url, params=None, headers=None):
        self.calls.append((url, params, headers))
        for host, response in self.by_host.items():
            if host in url:
                return response
        raise AssertionError(url)

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        return False


def test_extract_web_queries_dedupes_and_ignores_prose() -> None:
    text = '<web query="台北 夜市 推薦"/> <web query="台北 夜市 推薦"></web> <web query="x"/>'
    assert extract_web_queries(text) == ["台北 夜市 推薦", "x"]
    assert extract_web_queries("沒有標籤") == []


async def test_search_web_tries_the_api_then_searxng(monkeypatch, config) -> None:
    searx = {
        "results": [
            {"title": "SearX hit", "url": "https://a.example", "content": "a  b\n c"},
            {"title": "skip", "url": "javascript:void(0)"},
        ]
    }
    brave = {
        "web": {"results": [{"title": "Brave hit", "url": "https://b.example", "description": "d"}]}
    }
    session = Session({"searxng": Response(searx), "brave": Response(brave)})
    monkeypatch.setattr(search.aiohttp, "ClientSession", lambda **kw: session)
    # no key: SearXNG only
    provider, hits = await search_web("q", config)
    assert provider == "SearXNG" and hits == [Hit("SearX hit", "https://a.example", "a b c")]
    assert session.calls[0][1]["format"] == "json"
    # key present: Brave first
    keyed = replace(config, search_api="brave", search_api_key="k")
    provider, hits = await search_web("q", keyed)
    assert provider == "Brave" and hits[0].url == "https://b.example"
    assert session.calls[-1][2]["X-Subscription-Token"] == "k"
    # Brave failing falls back to SearXNG; both failing yields nothing
    session = Session({"searxng": Response(searx), "brave": Response({}, status=429)})
    monkeypatch.setattr(search.aiohttp, "ClientSession", lambda **kw: session)
    assert (await search_web("q", keyed))[0] == "SearXNG"
    session = Session({"searxng": Response({}, status=503), "brave": Response({}, status=429)})
    monkeypatch.setattr(search.aiohttp, "ClientSession", lambda **kw: session)
    assert await search_web("q", keyed) == ("", [])
    assert not search.available(replace(config, search_url=""))


def test_render_results_lists_hits_or_says_none() -> None:
    block = render_results("q", "SearXNG", [Hit("T", "https://t", "s"), Hit("U", "https://u", "")])
    assert block.startswith('<RESULT kind="web" query="q" via="SearXNG">')
    assert "1. T — https://t\n   s\n2. U — https://u\n</RESULT>" in block
    assert "沒有搜尋結果" in render_results("q", "", [])
