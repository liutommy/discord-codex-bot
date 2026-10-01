"""The filtering proxy, and Chromium behind it.

The browser tests drive a real Chromium against a local server that plays both roles: reached
as `public.example` it is the public site, reached as `127.0.0.1` it is the internal service.
What they assert is that `/secret` was never *requested* — not merely that its content is
missing from the output.
"""

from __future__ import annotations

import asyncio
import ipaddress
import os
import socket
from dataclasses import replace
from pathlib import Path

import pytest
from aiohttp import web
from aiohttp.test_utils import TestServer

from discord_codex_bot import fetchproxy, links
from discord_codex_bot.config import Config
from discord_codex_bot.fetchproxy import FilterProxy

PROXY_NETS = (ipaddress.ip_network("198.18.0.0/15"),)


async def _resolve(host: str) -> str:
    if host in ("ok.example", "public.example"):
        return "127.0.0.1"
    if host == "proxied.example":
        return "198.18.0.1"
    if host == "nxdomain.example":
        raise socket.gaierror("no such host")
    raise ValueError("host resolves to a private or reserved address")


async def _ask(proxy: FilterProxy, raw: bytes, then: bytes = b"") -> bytes:
    reader, writer = await asyncio.open_connection("127.0.0.1", int(proxy.url.rsplit(":", 1)[1]))
    writer.write(raw)
    await writer.drain()
    if then:
        await reader.readuntil(b"\r\n\r\n")
        writer.write(then)
        await writer.drain()
    data = await asyncio.wait_for(reader.read(), 10)
    writer.close()
    return data


@pytest.fixture
async def origin():
    seen: list[dict] = []

    async def hello(request: web.Request) -> web.Response:
        seen.append({"path": request.path_qs, **{k.lower(): v for k, v in request.headers.items()}})
        return web.Response(text="hello from origin")

    app = web.Application()
    app.add_routes([web.get("/hello", hello)])
    async with TestServer(app) as server:
        server.seen = seen
        yield server


async def test_http_request_is_forwarded_in_origin_form_and_closed(origin) -> None:
    async with FilterProxy(_resolve) as proxy:
        reply = await _ask(
            proxy,
            f"GET http://ok.example:{origin.port}/hello?a=1 HTTP/1.1\r\n"
            f"Host: ok.example:{origin.port}\r\nProxy-Connection: keep-alive\r\n"
            "Proxy-Authorization: Basic eDp5\r\n\r\n".encode(),
        )
    assert reply.startswith(b"HTTP/1.1 200") and reply.endswith(b"hello from origin")
    (request,) = origin.seen
    assert request["path"] == "/hello?a=1"
    assert request["connection"] == "close"
    assert "proxy-connection" not in request and "proxy-authorization" not in request


async def test_http_connection_carries_exactly_one_request() -> None:
    received = bytearray()

    async def raw_origin(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        # An origin that ignores "Connection: close" and keeps reading: everything the proxy
        # lets through on this connection ends up in `received`.
        head = await reader.readuntil(b"\r\n\r\n")
        received.extend(head)
        writer.write(b"HTTP/1.1 200 OK\r\nContent-Length: 2\r\n\r\nok")
        await writer.drain()
        try:
            received.extend(await asyncio.wait_for(reader.read(65536), 1.5))
        except TimeoutError:
            pass
        writer.close()

    server = await asyncio.start_server(raw_origin, "127.0.0.1", 0)
    port = server.sockets[0].getsockname()[1]
    async with server, FilterProxy(_resolve) as proxy:
        reply = await _ask(
            proxy,
            f"POST http://ok.example:{port}/first HTTP/1.1\r\nHost: ok.example\r\n"
            "Content-Length: 4\r\n\r\nbody"
            # ...and a second request pipelined behind it, for a name that was never checked.
            "GET http://lan.example/second HTTP/1.1\r\nHost: lan.example\r\n\r\n".encode(),
        )
    del reply  # a pipelining client is cut off; what matters is what reached the origin
    assert received.startswith(b"POST /first HTTP/1.1") and received.endswith(b"\r\n\r\nbody")
    assert b"second" not in received and b"lan.example" not in received


async def test_keep_alive_origin_cannot_be_reused_for_a_second_request() -> None:
    seen: list[bytes] = []

    async def keep_alive_origin(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        try:
            while True:  # answers every request it is given and never closes
                seen.append(await reader.readuntil(b"\r\n\r\n"))
                writer.write(b"HTTP/1.1 200 OK\r\nContent-Length: 2\r\n\r\nok")
                await writer.drain()
        except asyncio.IncompleteReadError:
            writer.close()

    server = await asyncio.start_server(keep_alive_origin, "127.0.0.1", 0)
    port = server.sockets[0].getsockname()[1]
    async with server, FilterProxy(_resolve) as proxy:
        reader, writer = await asyncio.open_connection(
            "127.0.0.1", int(proxy.url.rsplit(":", 1)[1])
        )
        writer.write(f"GET http://ok.example:{port}/first HTTP/1.1\r\n\r\n".encode())
        assert (await asyncio.wait_for(reader.readexactly(40), 5)).endswith(b"ok")
        # The browser would now reuse the socket. The proxy closes instead of forwarding, so the
        # browser retries on a new connection, which is checked like any other.
        writer.write(f"GET http://ok.example:{port}/second HTTP/1.1\r\n\r\n".encode())
        assert await asyncio.wait_for(reader.read(), 5) == b""
        writer.close()
    assert len(seen) == 1 and b"/first" in seen[0]


@pytest.mark.parametrize(
    "framing",
    [
        "Transfer-Encoding: chunked",
        "Content-Length: 4\r\nContent-Length: 40",
        "Content-Length: 4, 40",
        "Content-Length: -1",
    ],
)
async def test_http_requests_with_ambiguous_framing_are_refused(framing: str) -> None:
    async with FilterProxy(_resolve) as proxy:
        reply = await _ask(
            proxy, f"POST http://ok.example:1/x HTTP/1.1\r\n{framing}\r\n\r\nbody".encode()
        )
    assert reply.startswith(b"HTTP/1.1 400")


async def test_connect_tunnels_to_the_checked_address(origin) -> None:
    async with FilterProxy(_resolve) as proxy:
        reply = await _ask(
            proxy,
            f"CONNECT ok.example:{origin.port} HTTP/1.1\r\n\r\n".encode(),
            then=b"GET /hello HTTP/1.1\r\nHost: ok.example\r\nConnection: close\r\n\r\n",
        )
    assert reply.endswith(b"hello from origin")
    assert len(origin.seen) == 1


@pytest.mark.parametrize(
    "request_line",
    [
        "GET http://lan.example/ HTTP/1.1",
        "GET http://lan.example:8080/x HTTP/1.1",
        "CONNECT lan.example:443 HTTP/1.1",
        "CONNECT 127.0.0.1:2375 HTTP/1.1",
        "GET http://nxdomain.example/ HTTP/1.1",
    ],
)
async def test_refused_destinations_get_403_and_are_never_connected(
    monkeypatch, request_line: str
) -> None:
    async def no_connection(*args, **kwargs):
        raise AssertionError(f"connected to {args}")

    monkeypatch.setattr(fetchproxy.asyncio, "open_connection", no_connection)
    async with FilterProxy(_resolve) as proxy:
        port = int(proxy.url.rsplit(":", 1)[1])
        reader, writer = await asyncio.get_running_loop().create_connection(
            lambda: _Collect(), "127.0.0.1", port
        )
        reader.write(f"{request_line}\r\n\r\n".encode())
        reply = await asyncio.wait_for(writer.done, 10)
        assert reply.startswith(b"HTTP/1.1 403")
        assert proxy.refused


class _Collect(asyncio.Protocol):
    """A raw client that does not go through asyncio.open_connection (patched in the test)."""

    def __init__(self) -> None:
        self.data = b""
        self.done: asyncio.Future = asyncio.get_running_loop().create_future()

    def data_received(self, data: bytes) -> None:
        self.data += data

    def connection_lost(self, exc) -> None:
        if not self.done.done():
            self.done.set_result(self.data)


@pytest.mark.parametrize(
    "request_line",
    [
        "nonsense",
        "GET /relative HTTP/1.1",
        "GET https://ok.example/ HTTP/1.1",
        "CONNECT :443 HTTP/1.1",
        "CONNECT [::1:1234 HTTP/1.1",
        "CONNECT ok.example:0 HTTP/1.1",
        "GET http://ok.example:99999/ HTTP/1.1",
        "GET http://ok.example/ HTTP/1.1\nTransfer-Encoding: chunked",
        "GET http://ok.example/ HTTP/1.1\r\nX: a\rb",
    ],
)
async def test_malformed_requests_get_400(request_line: str) -> None:
    async with FilterProxy(_resolve) as proxy:
        assert (await _ask(proxy, f"{request_line}\r\n\r\n".encode())).startswith(b"HTTP/1.1 400")


async def test_allow_net_addresses_take_only_connect_443(monkeypatch) -> None:
    connected: list[tuple[str, int]] = []
    real_open = asyncio.open_connection

    async def record(host, port, **kwargs):
        if host == "198.18.0.1":
            connected.append((host, port))
            raise OSError("not reachable in a test")
        return await real_open(host, port, **kwargs)

    monkeypatch.setattr(fetchproxy.asyncio, "open_connection", record)
    async with FilterProxy(_resolve, PROXY_NETS) as proxy:
        for line in (
            "GET http://proxied.example/ HTTP/1.1",  # plain http: the egress proxy is TLS-only
            "CONNECT proxied.example:53 HTTP/1.1",
            "CONNECT proxied.example:8443 HTTP/1.1",
        ):
            assert (await _ask(proxy, f"{line}\r\n\r\n".encode())).startswith(b"HTTP/1.1 403")
        assert connected == []
        reply = await _ask(proxy, b"CONNECT proxied.example:443 HTTP/1.1\r\n\r\n")
        assert reply.startswith(b"HTTP/1.1 502")  # allowed through, and the dial was attempted
        assert connected == [("198.18.0.1", 443)]
    # Without the allow-list the same address is an ordinary public-looking one to the proxy:
    # the caller's resolve() is what refuses it, so nothing extra is restricted here.


async def test_render_runs_chromium_behind_the_proxy_with_the_allow_nets(
    monkeypatch, config: Config
) -> None:
    from test_links import FakePage, install_fake_playwright

    built: list[tuple] = []
    launched: dict = {}

    class Recording(FilterProxy):
        def __init__(self, resolve, allow=()):
            built.append(allow)
            super().__init__(resolve, allow)

    async def resolve_ok(host: str, allow=()) -> str:
        return "93.184.216.34"

    monkeypatch.setattr(links, "FilterProxy", Recording)
    monkeypatch.setattr(links, "_resolve_public", resolve_ok)
    browser = install_fake_playwright(monkeypatch, FakePage(["T"], text="body"))
    fake = __import__("sys").modules["playwright.async_api"]
    real_context = fake.async_playwright

    class Capturing:
        async def __aenter__(self):
            pw = await real_context().__aenter__()
            launch = pw.chromium.launch

            async def recording_launch(**kwargs):
                launched.update(kwargs)
                return await launch(**kwargs)

            pw.chromium.launch = recording_launch
            return pw

        async def __aexit__(self, *args) -> bool:
            return False

    monkeypatch.setattr(fake, "async_playwright", Capturing)
    cfg = replace(config, link_allow_nets=PROXY_NETS, link_render_timeout_seconds=3)
    await links._render("https://ok.example/", cfg, None)
    assert built == [PROXY_NETS]
    assert launched["proxy"]["server"].startswith("http://127.0.0.1:")
    assert launched["proxy"]["bypass"] == "<-loopback>"
    assert "--host-resolver-rules=MAP * ~NOTFOUND, EXCLUDE 127.0.0.1" in launched["args"]
    assert browser.context_kwargs["service_workers"] == "block"


# --------------------------------------------------------------------------- real Chromium


def _chromium_installed() -> bool:
    root = Path(os.environ.get("PLAYWRIGHT_BROWSERS_PATH", "~/.cache/ms-playwright")).expanduser()
    return any(root.glob("chromium-*"))


# Locally the browser tests skip without Chromium; CI sets REQUIRE_CHROMIUM so a missing browser
# fails them instead of silently dropping the security acceptance tests.
chromium = pytest.mark.skipif(
    not _chromium_installed() and not os.environ.get("REQUIRE_CHROMIUM"),
    reason="Playwright Chromium not installed",
)


@pytest.fixture
async def site(monkeypatch, tmp_path):
    hits = {"secret": 0, "ws": 0, "redirects": 0}

    def to_secret(request: web.Request) -> str:
        return f"http://127.0.0.1:{request.url.port}/secret"

    async def page(request: web.Request) -> web.Response:
        return web.Response(
            text="<title>Public</title><p>public page body</p>", content_type="text/html"
        )

    async def r302(request: web.Request) -> web.Response:
        hits["redirects"] += 1
        raise web.HTTPFound(to_secret(request))

    async def r307(request: web.Request) -> web.Response:
        hits["redirects"] += 1
        raise web.HTTPTemporaryRedirect(to_secret(request))

    async def secret(request: web.Request) -> web.Response:
        hits["secret"] += 1
        return web.Response(text="INTERNAL-SECRET", headers={"Access-Control-Allow-Origin": "*"})

    async def ws(request: web.Request) -> web.WebSocketResponse:
        hits["ws"] += 1
        socket_ = web.WebSocketResponse()
        await socket_.prepare(request)
        return socket_

    async def attack(request: web.Request) -> web.Response:
        port = request.url.port
        return web.Response(
            content_type="text/html",
            text=f"""<title>Attack</title><p>attack page</p>
<img src="/r302?img"><iframe src="/r302?frame"></iframe>
<script>
fetch('/r302?fetch').catch(() => 0);
fetch('/r307', {{method: 'POST', body: 'x'}}).catch(() => 0);
fetch('http://127.0.0.1:{port}/secret', {{mode: 'no-cors'}}).catch(() => 0);
try {{ new WebSocket('ws://127.0.0.1:{port}/ws'); }} catch (e) {{}}
</script>""",
        )

    async def rtc(request: web.Request) -> web.Response:
        return web.Response(
            content_type="text/html",
            text=f"""<title>RTC</title><p>rtc page</p><script>
const pc = new RTCPeerConnection({{iceServers: [
  {{urls: 'stun:127.0.0.1:{request.query["udp"]}'}},
  {{urls: 'turn:127.0.0.1:{request.query["udp"]}', username: 'u', credential: 'p'}}]}});
pc.createDataChannel('x');
pc.createOffer().then(o => pc.setLocalDescription(o));
</script>""",
        )

    app = web.Application()
    app.add_routes(
        [
            web.get("/rtc", rtc),
            web.get("/page", page),
            web.get("/r302", r302),
            web.route("*", "/r307", r307),
            web.route("*", "/secret", secret),
            web.get("/ws", ws),
            web.get("/attack", attack),
        ]
    )
    real = links._resolve_public

    async def resolve(host: str, allow=()) -> str:
        if host == "public.example":
            return "127.0.0.1"  # the test server, reached as a public site
        return await real(host, allow)

    monkeypatch.setattr(links, "_resolve_public", resolve)
    async with TestServer(app) as server:
        server.hits = hits
        yield server


@chromium
async def test_chromium_renders_a_public_page_through_the_proxy(site, config: Config) -> None:
    title, text, _shot = await links._render(
        f"http://public.example:{site.port}/page", config, None
    )
    assert title == "Public" and "public page body" in text
    assert site.hits["secret"] == 0


@chromium
@pytest.mark.parametrize("path", ["/r302", "/r307"])
async def test_chromium_navigation_redirect_to_an_internal_address_never_arrives(
    site, config: Config, path: str
) -> None:
    # Chromium reports the proxy's 403 as a failed navigation; render_link turns any of these
    # into "渲染失敗".
    with pytest.raises(Exception):  # noqa: B017
        await links._render(f"http://public.example:{site.port}{path}", config, None)
    text, shot = await links.render_link(f"http://public.example:{site.port}{path}", config, None)
    assert "渲染失敗" in text and "INTERNAL" not in text and shot is None
    assert site.hits["redirects"] == 2  # the public hop was served both times...
    assert site.hits["secret"] == 0  # ...and the internal one was never requested


@chromium
async def test_chromium_subresource_fetch_and_websocket_redirects_never_arrive(
    site, config: Config
) -> None:
    with pytest.raises(links.RefusedAddress):  # the iframe ended on the internal address
        await links._render(f"http://public.example:{site.port}/attack", config, None)
    await asyncio.sleep(0.5)
    assert site.hits["redirects"] >= 4  # img, iframe, fetch and the POST all left the page
    assert site.hits["secret"] == 0
    assert site.hits["ws"] == 0


@chromium
async def test_chromium_loopback_goes_through_the_proxy_even_without_the_route_hook(
    site, monkeypatch, config: Config
) -> None:
    async def wave_through(route, request, hosts, allow=()):
        await route.continue_()

    monkeypatch.setattr(links, "_guard_route", wave_through)
    with pytest.raises(Exception):  # noqa: B017 - refused navigation or RefusedAddress
        await links._render(f"http://127.0.0.1:{site.port}/secret", config, None)
    with pytest.raises(links.RefusedAddress):
        await links._render(f"http://public.example:{site.port}/attack", config, None)
    assert site.hits["secret"] == 0 and site.hits["ws"] == 0


class _Datagrams(asyncio.DatagramProtocol):
    def __init__(self) -> None:
        self.received: list[bytes] = []

    def datagram_received(self, data: bytes, addr) -> None:
        self.received.append(data)


@chromium
async def test_chromium_webrtc_cannot_send_udp_past_the_proxy(site, config: Config) -> None:
    transport, datagrams = await asyncio.get_running_loop().create_datagram_endpoint(
        _Datagrams, local_addr=("127.0.0.1", 0)
    )
    try:
        udp = transport.get_extra_info("sockname")[1]
        title, _text, _shot = await links._render(
            f"http://public.example:{site.port}/rtc?udp={udp}", config, None
        )
        assert title == "RTC"
        await asyncio.sleep(2)  # ICE gathering sends its STUN/TURN requests within a second
        assert datagrams.received == []
    finally:
        transport.close()


def test_yt_dlp_is_given_the_proxy_and_never_delegates_to_ffmpeg(monkeypatch, tmp_path) -> None:
    import sys
    import types

    captured: dict = {}

    class FakeYDL:
        def __init__(self, options) -> None:
            captured.update(options)

        def __enter__(self):
            return self

        def __exit__(self, *args) -> bool:
            return False

        def extract_info(self, url, download) -> None:
            pass

    monkeypatch.setitem(sys.modules, "yt_dlp", types.SimpleNamespace(YoutubeDL=FakeYDL))
    links._yt_dlp_download("https://v.example/1", tmp_path / "o", 1000, "http://127.0.0.1:9")
    assert captured["proxy"] == "http://127.0.0.1:9"
    assert captured["hls_prefer_native"] is True
