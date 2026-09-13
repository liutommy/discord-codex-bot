from __future__ import annotations

import json
from pathlib import Path

from discord_codex_bot import apis
from discord_codex_bot.apis import Api, call_api, extract_api_calls, load_registry, render_doc


def test_load_registry_expands_env_and_rejects_non_https(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("SECRET_KEY", "s3cret")
    (tmp_path / "apis.json").write_text(json.dumps({
        "good": {"base": "https://api.example/v1/", "headers": {"x-key": "${SECRET_KEY}"},
                 "doc": "用法"},
        "bad": {"base": "http://plain.example/"},
        "junk": "nope",
    }), "utf-8")
    registry = load_registry(tmp_path / "apis.json")
    assert list(registry) == ["good"]
    assert registry["good"].headers == {"x-key": "s3cret"} and registry["good"].doc == "用法"
    assert load_registry(None) == {} and load_registry(tmp_path / "missing.json") == {}


def test_extract_api_calls_and_doc() -> None:
    text = (
        '<api name="lolesports" path="getLeagues?hl=zh-TW"/>'
        '<api name="lolesports" path="getLeagues?hl=zh-TW"></api>'
        '<api name="x" path="a"/><api name="y" path="b"/>'
    )
    assert extract_api_calls(text) == [("lolesports", "getLeagues?hl=zh-TW"), ("x", "a")]
    doc = render_doc({"lol": Api("lol", "https://x/", {}, "先 getLeagues")})
    assert doc.startswith("可呼叫的資料 API") and "- lol：先 getLeagues" in doc
    assert render_doc({}) == ""


class Response:
    def __init__(self, body: bytes, status=200, content_type="application/json"):
        self.body, self.status, self.content_type = body, status, content_type
        self.content = self

    async def read(self, n):
        return self.body[:n]

    async def iter_chunked(self, n):
        yield self.body

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        return False


class Session:
    def __init__(self, response):
        self.response, self.calls = response, []

    def get(self, url):
        self.calls.append(url)
        return self.response

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        return False


async def test_call_api_builds_the_url_sends_headers_and_compacts_json(monkeypatch, config) -> None:
    registry = {"lol": Api("lol", "https://api.example/gw/", {"x-api-key": "k"}, "")}
    session = Session(Response(b'{"data": {"a": [1, 2]}}'))
    seen = {}

    def make_session(**kw):
        seen["headers"] = kw.get("headers")
        return session

    monkeypatch.setattr(apis.aiohttp, "ClientSession", make_session)
    body = await call_api("lol", "/getLeagues?hl=zh-TW&amp;leagueId=1", registry, config)
    assert body == '{"data":{"a":[1,2]}}' and seen["headers"] == {"x-api-key": "k"}
    assert session.calls == ["https://api.example/gw/getLeagues?hl=zh-TW&leagueId=1"]  # unescaped
    assert "只能是相對" in await call_api("lol", "https://evil/x", registry, config)
    assert "只能是相對" in await call_api("lol", "../x", registry, config)
    assert "沒有叫 nope" in await call_api("nope", "x", registry, config)
    limited = Session(Response(b"limited", 429, "text/html"))
    monkeypatch.setattr(apis.aiohttp, "ClientSession", lambda **kw: limited)
    assert (await call_api("lol", "x", registry, config)).startswith("（lol 回 HTTP 429：limited")
