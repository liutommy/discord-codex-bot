"""Loopback forward proxy that Chromium and yt-dlp are sent through.

Both open their own sockets, resolve their own names and follow their own redirects, so a check
made before handing them a URL says nothing about where they end up: a public page can redirect
a navigation, an image or a fetch() to an internal address, or open a WebSocket to one. Here
every connection they make arrives as one proxy request, is resolved once through the caller's
public-address check, and is then connected to the address that was checked. One client
connection maps to exactly one upstream, so nothing sent later on it can reach anywhere else.
"""

from __future__ import annotations

import asyncio
import ipaddress
import logging
import socket
from collections.abc import Awaitable, Callable, Sequence
from urllib.parse import urlsplit

LOGGER = logging.getLogger(__name__)
_HEAD_TIMEOUT = 15
_CONNECT_TIMEOUT = 15
_HOP_HEADERS = (b"proxy-connection", b"proxy-authorization", b"connection", b"keep-alive")


class FilterProxy:
    """`resolve(host)` returns the one address to connect to, or raises ValueError / gaierror.
    `allow` is LINK_ALLOW_NETS: an address admitted only by it is the host's egress proxy, which
    listens for TLS on 443 and nothing else, so only `CONNECT host:443` may go there."""

    def __init__(
        self,
        resolve: Callable[[str], Awaitable[str]],
        allow: Sequence[ipaddress.IPv4Network] = (),
    ) -> None:
        self._resolve = resolve
        self._allow = allow
        self._server: asyncio.Server | None = None
        self._tasks: set[asyncio.Task] = set()
        self.url = ""
        # Hosts turned away, so the caller can tell "the proxy refused this" from "this never
        # came through the proxy at all".
        self.refused: set[str] = set()

    async def __aenter__(self) -> FilterProxy:
        self._server = await asyncio.start_server(self._accept, "127.0.0.1", 0)
        port = self._server.sockets[0].getsockname()[1]
        self.url = f"http://127.0.0.1:{port}"
        return self

    async def __aexit__(self, *_exc) -> None:
        assert self._server is not None
        self._server.close()
        for task in list(self._tasks):
            task.cancel()
        await asyncio.gather(*self._tasks, return_exceptions=True)
        await self._server.wait_closed()

    async def _accept(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        task = asyncio.current_task()
        assert task is not None
        self._tasks.add(task)
        try:
            await self._serve(reader, writer)
        except (OSError, asyncio.IncompleteReadError, asyncio.LimitOverrunError, TimeoutError):
            pass
        finally:
            self._tasks.discard(task)
            writer.close()

    async def _serve(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        head = await asyncio.wait_for(reader.readuntil(b"\r\n\r\n"), _HEAD_TIMEOUT)
        request_line, _, rest = head.partition(b"\r\n")
        try:
            method, target, version = request_line.decode("latin-1").split(" ")
        except ValueError:
            return await _reply(writer, 400, "Bad Request")
        tunnel = method.upper() == "CONNECT"
        parts = urlsplit(f"//{target}" if tunnel else target)
        try:
            host, port = parts.hostname or "", parts.port or (443 if tunnel else 80)
        except ValueError:  # port out of range
            return await _reply(writer, 400, "Bad Request")
        if not host or (not tunnel and parts.scheme != "http"):
            return await _reply(writer, 400, "Bad Request")
        body = 0 if tunnel else _body_length(rest)
        if body is None:
            return await _reply(writer, 400, "Bad Request")
        try:
            address = await self._resolve(host)
        except (ValueError, socket.gaierror):
            address = ""
        if address and self._via_allow(address) and not (tunnel and port == 443):
            address = ""
        if not address:
            self.refused.add(host)
            LOGGER.info("fetch proxy refused %s:%s", host, port)
            return await _reply(writer, 403, "Forbidden")
        try:
            upstream_reader, upstream = await asyncio.wait_for(
                asyncio.open_connection(address, port), _CONNECT_TIMEOUT
            )
        except (OSError, TimeoutError):
            return await _reply(writer, 502, "Bad Gateway")
        try:
            if tunnel:
                writer.write(b"HTTP/1.1 200 Connection established\r\n\r\n")
                await writer.drain()
            else:
                path = parts.path or "/"
                if parts.query:
                    path = f"{path}?{parts.query}"
                upstream.write(f"{method} {path} {version}\r\n".encode("latin-1"))
                upstream.write(_origin_headers(rest))
                await upstream.drain()
            if tunnel:
                await _relay(reader, writer, upstream_reader, upstream)
            else:
                # Exactly one request: its body, then only the response. Anything else the
                # client sends on this connection was never checked and is not forwarded.
                await _copy(reader, upstream, body)
                await _pipe(upstream_reader, writer)
        finally:
            upstream.close()

    def _via_allow(self, address: str) -> bool:
        ip = ipaddress.ip_address(address)
        return any(ip in net for net in self._allow)


def _origin_headers(block: bytes) -> bytes:
    """The request's headers for the origin: hop-by-hop ones dropped, and the connection closed
    after this exchange so the browser cannot reuse it for a request to another host."""
    lines = [
        line
        for line in block.split(b"\r\n")
        if line and line.split(b":", 1)[0].strip().lower() not in _HOP_HEADERS
    ]
    return b"\r\n".join([*lines, b"Connection: close"]) + b"\r\n\r\n"


def _body_length(block: bytes) -> int | None:
    """Content-Length of the one request this connection carries; None when its framing is
    anything a second request could hide behind (chunked, repeated or non-numeric lengths)."""
    lengths = []
    for line in block.split(b"\r\n"):
        name, _, value = line.partition(b":")
        name = name.strip().lower()
        if name == b"transfer-encoding":
            return None
        if name == b"content-length":
            lengths.append(value.strip())
    if not lengths:
        return 0
    if len(lengths) > 1 or not lengths[0].isdigit():
        return None
    return int(lengths[0])


async def _copy(reader: asyncio.StreamReader, writer: asyncio.StreamWriter, count: int) -> None:
    while count > 0:
        chunk = await reader.read(min(count, 65536))
        if not chunk:
            return
        writer.write(chunk)
        await writer.drain()
        count -= len(chunk)


async def _reply(writer: asyncio.StreamWriter, status: int, reason: str) -> None:
    writer.write(
        f"HTTP/1.1 {status} {reason}\r\nContent-Length: 0\r\nConnection: close\r\n\r\n".encode()
    )
    await writer.drain()


async def _pipe(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
    while chunk := await reader.read(65536):
        writer.write(chunk)
        await writer.drain()


async def _relay(
    client_reader: asyncio.StreamReader,
    client: asyncio.StreamWriter,
    upstream_reader: asyncio.StreamReader,
    upstream: asyncio.StreamWriter,
) -> None:
    """Copy both ways until either side closes."""
    pipes = [
        asyncio.ensure_future(_pipe(client_reader, upstream)),
        asyncio.ensure_future(_pipe(upstream_reader, client)),
    ]
    try:
        await asyncio.wait(pipes, return_when=asyncio.FIRST_COMPLETED)
    finally:
        for pipe in pipes:
            pipe.cancel()
        await asyncio.gather(*pipes, return_exceptions=True)
