from __future__ import annotations

import json
from pathlib import Path

from discord_codex_bot import apis
from discord_codex_bot.apis import Api, call_api, extract_api_calls, load_registry, render_doc


def test_load_registry_expands_env_and_rejects_non_https(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("SECRET_KEY", "s3cret")
    (tmp_path / "apis.json").write_text(
        json.dumps(
            {
                "good": {
                    "base": "https://api.example/v1/",
                    "headers": {"x-key": "${SECRET_KEY}"},
                    "doc": "用法",
                },
                "bad": {"base": "http://plain.example/"},
                "junk": "nope",
            }
        ),
        "utf-8",
    )
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

    # HTTP 429 is throttling as well: retried once, then reported as throttling, not as an answer
    async def no_sleep(seconds):
        pass

    limited = Session(Response(b"limited", 429, "text/html"))
    monkeypatch.setattr(apis.aiohttp, "ClientSession", lambda **kw: limited)
    monkeypatch.setattr(apis.asyncio, "sleep", no_sleep)
    body = await call_api("lol", "x", registry, config)
    assert len(limited.calls) == 2 and "被限流" in body


async def test_call_api_retries_a_throttled_reply_and_never_passes_it_off_as_data(
    monkeypatch, config
) -> None:
    """MediaWiki answers a rate-limited query with HTTP 200 and an error body. Handing that to
    the model as if it were data is how "come back in a minute" became "there is no record"."""
    registry = {"lp": Api("lp", "https://api.example/", {}, "")}
    throttled = Session(Response(b'{"error":{"code":"ratelimited","info":"slow down"}}'))
    slept = []

    async def fake_sleep(seconds):
        slept.append(seconds)

    monkeypatch.setattr(apis.aiohttp, "ClientSession", lambda **kw: throttled)
    monkeypatch.setattr(apis.asyncio, "sleep", fake_sleep)
    body = await call_api("lp", "action=cargoquery", registry, config)
    # one retry, then give up rather than keep hammering a throttled source
    assert len(throttled.calls) == 2 and slept == [apis.RETRY_AFTER_SECONDS]
    assert "被限流" in body and "這不是查無資料" in body and "slow down" in body
    # an error that is not throttling is reported as an error, and is not worth a retry
    broken = Session(Response(b'{"error":{"code":"badvalue","info":"bad where clause"}}'))
    monkeypatch.setattr(apis.aiohttp, "ClientSession", lambda **kw: broken)
    body = await call_api("lp", "x", registry, config)
    assert len(broken.calls) == 1 and "badvalue" in body and "bad where clause" in body


def test_load_registry_reads_a_bot_password_login_and_stays_anonymous_without_one(
    tmp_path: Path, monkeypatch
) -> None:
    monkeypatch.setenv("WIKI_USER", "bot@app")
    monkeypatch.setenv("WIKI_PASS", "secret")
    spec = {
        "wiki": {
            "base": "https://w.example/api.php?",
            "login": {
                "api": "https://w.example/api.php",
                "user": "${WIKI_USER}",
                "password": "${WIKI_PASS}",
            },
        }
    }
    (tmp_path / "a.json").write_text(json.dumps(spec), "utf-8")
    assert load_registry(tmp_path / "a.json")["wiki"].login["user"] == "bot@app"
    monkeypatch.delenv("WIKI_PASS")
    # credentials not configured: query anonymously (and wear the rate limit) rather than fail
    assert load_registry(tmp_path / "a.json")["wiki"].login == {}


async def test_call_api_logs_in_once_and_reuses_the_session(monkeypatch, config) -> None:
    registry = {
        "wiki": Api(
            "wiki",
            "https://w.example/api.php?",
            {},
            "",
            {"api": "https://w.example/api.php", "user": "u", "password": "p"},
        )
    }
    apis._LOGGED_IN.discard("wiki")
    logins = []

    async def fake_login(api, cfg):
        logins.append(api.name)
        apis._LOGGED_IN.add(api.name)
        return True

    session = Session(Response(b'{"cargoquery":[]}'))
    monkeypatch.setattr(apis, "_login", fake_login)
    monkeypatch.setattr(apis.aiohttp, "ClientSession", lambda **kw: session)
    try:
        assert await call_api("wiki", "action=cargoquery", registry, config) == '{"cargoquery":[]}'
        await call_api("wiki", "action=cargoquery&x=2", registry, config)
        assert logins == ["wiki"]  # the cookie jar outlives the call; no login per query
    finally:
        apis._LOGGED_IN.discard("wiki")


def test_localize_rewrites_longest_names_first_in_one_pass() -> None:
    from discord_codex_bot.apis import localize

    names = {"凯尔": "凱爾", "凯尔特": "凱爾特", "回响施放": "共鳴施放"}
    out = localize("推荐 回响施放 给 凯尔 和 凯尔特", names)
    assert out == "推荐 共鳴施放（回响施放） 给 凱爾（凯尔） 和 凱爾特（凯尔特）"
    assert localize("nothing here", names) == "nothing here" and localize("x", {}) == "x"


async def test_call_api_applies_the_names_map_from_next_to_the_registry(
    tmp_path: Path, monkeypatch, config
) -> None:
    (tmp_path / "names.json").write_text(json.dumps({"回响施放": "共鳴施放", "同": "同"}), "utf-8")
    spec = {"hx": {"base": "https://hx.example/", "names": "names.json"}}
    (tmp_path / "apis.json").write_text(json.dumps(spec), "utf-8")
    registry = load_registry(tmp_path / "apis.json")
    assert registry["hx"].names == {"回响施放": "共鳴施放"}  # identical pairs dropped
    page = "<html><body>推荐 回响施放 98.0</body></html>".encode()
    monkeypatch.setattr(
        apis.aiohttp, "ClientSession", lambda **kw: Session(Response(page, 200, "text/html"))
    )
    assert "共鳴施放（回响施放） 98.0" in await call_api("hx", "hero/4", registry, config)
    # a missing map is a warning, not a broken API
    spec["hx"]["names"] = "gone.json"
    (tmp_path / "apis.json").write_text(json.dumps(spec), "utf-8")
    assert load_registry(tmp_path / "apis.json")["hx"].names == {}


async def test_call_api_hands_a_page_over_as_text_not_markup(monkeypatch, config) -> None:
    # hexdata's hero pages are plain HTML. Given the raw page (styles, JSON-LD, tags) the model
    # skipped it and web-searched another site for the same numbers; as text it reads it.
    registry = {"hx": Api("hx", "https://hx.example/", {}, "")}
    page = (
        b"<html><head><title>Yasuo - Hexdata</title><style>body{margin:0}</style></head>"
        b"<body><h1>Patch 16.18</h1><table><tr><td>\xe8\x83\x9c\xe7\x8e\x87 57.5%</td></tr></table>"
        b'<a href="/augment/1030-eureka">Eureka</a></body></html>'
    )
    monkeypatch.setattr(
        apis.aiohttp, "ClientSession", lambda **kw: Session(Response(page, 200, "text/html"))
    )
    body = await call_api("hx", "hero/157-yasuo", registry, config)
    assert body.startswith("Yasuo - Hexdata\n\n") and "胜率 57.5%" in body
    assert "<style>" not in body and "margin:0" not in body and "<td>" not in body
    assert "https://hx.example/augment/1030-eureka" in body  # links survive, absolute
