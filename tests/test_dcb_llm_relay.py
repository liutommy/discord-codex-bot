"""deploy/orbstack/mac/dcb-llm-relay.py, run on the system python3 against a fake `orb` that plays
socat 1.7.4 as measured on Debian 12: one TCP listener per invocation (reuseport), closed once it
has accepted; the client's EOF reaches the stdout pipe only with `shut-close` (otherwise only when
socat exits); after one side's EOF socat waits at most -t (default 0.5 s) for the other. A fake
upstream server on 127.0.0.1 stands in for the LLM server."""

from __future__ import annotations

import hashlib
import json
import os
import signal
import socket
import struct
import subprocess
import sys
import textwrap
import threading
import time
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
RELAY = ROOT / "deploy" / "orbstack" / "mac" / "dcb-llm-relay.py"
PYTHON = "/usr/bin/python3" if Path("/usr/bin/python3").exists() else sys.executable

pytestmark = pytest.mark.skipif(
    not hasattr(socket, "SO_REUSEPORT"), reason="the fake socat needs SO_REUSEPORT"
)

FAKE_ORB = textwrap.dedent(
    """\
    import os, socket, sys, threading, time
    argv = sys.argv[1:]
    with open(os.environ["STUB_CALLS"], "a") as f:
        f.write("%d %s\\n" % (os.getpid(), " ".join(argv)))
    cmd = argv[argv.index("-u") + 2:]
    if cmd[0] != "socat":
        sys.exit(0)
    if os.environ.get("STUB_FAIL"):
        sys.stderr.write("socat E bind(5, {AF=2 x:1}, 16): Cannot assign requested address\\n")
        sys.exit(1)
    linger = 0.5  # socat -t default
    for a in cmd[1:]:
        if a.startswith("-t"):
            linger = float(a[2:])
    shut_close = "shut-close" in cmd[-1].split(",")[1:]
    spec = next(a for a in cmd if a.startswith("TCP-LISTEN:"))[len("TCP-LISTEN:"):].split(",")
    opts = dict(o.split("=", 1) if "=" in o else (o, "") for o in spec[1:])
    assert "reuseaddr" in opts and "reuseport" in opts, spec
    s = socket.socket()
    s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEPORT, 1)
    s.bind((opts["bind"], int(spec[0])))
    s.listen(8)
    conn, _ = s.accept()
    s.close()  # socat without fork stops listening once it has a connection
    done = threading.Event()
    def sock_to_stdout():
        while True:
            data = conn.recv(65536)
            if not data:
                break
            os.write(1, data)
        if shut_close:  # without it the pipe stays open until socat exits
            os.close(1)
        done.set()
    def stdin_to_sock():
        while True:
            data = os.read(0, 65536)
            if not data:
                break
            conn.sendall(data)
        conn.shutdown(socket.SHUT_WR)
        done.set()
    a = threading.Thread(target=sock_to_stdout, daemon=True)
    b = threading.Thread(target=stdin_to_sock, daemon=True)
    a.start(); b.start()
    done.wait()
    # Like socat: after one side's EOF, wait at most -t seconds for the other.
    end = time.monotonic() + linger
    while (a.is_alive() or b.is_alive()) and time.monotonic() < end:
        time.sleep(0.01)
    os._exit(0)
    """
)


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


class Upstream:
    """Fake LLM server. handler(conn, index) runs per connection in its own thread."""

    def __init__(self, handler) -> None:
        self.handler = handler
        self.sock = socket.socket()
        self.sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self.sock.bind(("127.0.0.1", 0))
        self.sock.listen(16)
        self.port = self.sock.getsockname()[1]
        self.accepted = 0
        threading.Thread(target=self._loop, daemon=True).start()

    def _loop(self) -> None:
        while True:
            try:
                conn, _ = self.sock.accept()
            except OSError:
                return
            self.accepted += 1
            threading.Thread(target=self.handler, args=(conn, self.accepted), daemon=True).start()

    def close(self) -> None:
        try:  # Linux: close() alone leaves a listener that a thread is blocked in accept() on
            self.sock.shutdown(socket.SHUT_RDWR)
        except OSError:
            pass
        self.sock.close()


def _recv_exact(conn: socket.socket, n: int) -> bytes:
    buf = bytearray()
    while len(buf) < n:
        data = conn.recv(min(65536, n - len(buf)))
        if not data:
            raise EOFError(f"got {len(buf)} of {n} bytes")
        buf += data
    return bytes(buf)


def _read_framed(conn: socket.socket) -> bytes:
    (n,) = struct.unpack(">Q", _recv_exact(conn, 8))
    return _recv_exact(conn, n)


def _framed(data: bytes) -> bytes:
    return struct.pack(">Q", len(data)) + data


def echo(conn: socket.socket, _index: int) -> None:
    with conn:
        conn.sendall(_framed(_read_framed(conn)))


class Harness:
    def __init__(self, tmp_path: Path, upstream_port: int, workers: int = 2, **env) -> None:
        self.tmp = tmp_path
        self.port = _free_port()
        self.calls = tmp_path / "calls"
        self.calls.touch()
        self.log = tmp_path / "relay.log"
        bindir = tmp_path / "bin"
        bindir.mkdir()
        (bindir / "orb").write_text(f"#!{PYTHON}\n" + FAKE_ORB)
        (bindir / "orb").chmod(0o755)
        self.env = {
            "PATH": f"{bindir}:/usr/bin:/bin",
            "HOME": str(tmp_path),
            "STUB_CALLS": str(self.calls),
            "DCB_LLM_RELAY_CONFIG": str(tmp_path / "no-config"),
            "DCB_LLM_RELAY_USER": "u",
            "DCB_LLM_RELAY_LISTEN_ADDR": "127.0.0.1",
            "DCB_LLM_RELAY_LISTEN_PORT": str(self.port),
            "DCB_LLM_RELAY_UPSTREAM_PORT": str(upstream_port),
            "DCB_LLM_RELAY_WORKERS": str(workers),
            "DCB_LLM_RELAY_IDLE": "30",
            "DCB_LLM_RELAY_LOG": str(self.log),
            **env,
        }
        self.proc = subprocess.Popen(
            [PYTHON, str(RELAY)], env=self.env, stdout=subprocess.PIPE, stderr=subprocess.STDOUT
        )

    def connect(self, deadline: float = 5.0) -> socket.socket:
        """Connect like the Bot does: a refused connection is retried."""
        end = time.monotonic() + deadline
        while True:
            try:
                return socket.create_connection(("127.0.0.1", self.port), timeout=10)
            except ConnectionRefusedError:
                if time.monotonic() > end:
                    raise
                time.sleep(0.05)

    def socat_calls(self) -> list[tuple[int, str]]:
        out = []
        for line in self.calls.read_text().splitlines():
            pid, _, args = line.partition(" ")
            if " socat " in f" {args} ":
                out.append((int(pid), args))
        return out

    def log_text(self) -> str:
        return self.log.read_text() if self.log.exists() else ""

    def stop(self) -> None:
        if self.proc.poll() is None:
            self.proc.send_signal(signal.SIGTERM)
            try:
                self.proc.wait(10)
            except subprocess.TimeoutExpired:
                self.proc.kill()
                self.proc.wait()
        for pid, _ in self.socat_calls():
            try:
                os.kill(pid, signal.SIGKILL)
            except ProcessLookupError:
                pass


@pytest.fixture
def make(tmp_path):
    made: list[Harness] = []
    ups: list[Upstream] = []

    def _make(handler=echo, **kw) -> tuple[Harness, Upstream]:
        up = Upstream(handler)
        ups.append(up)
        h = Harness(tmp_path, up.port, **kw)
        made.append(h)
        return h, up

    yield _make
    for h in made:
        h.stop()
    for up in ups:
        up.close()


def _wait(cond, timeout: float = 5.0) -> bool:
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        if cond():
            return True
        time.sleep(0.02)
    return cond()


def test_a_large_binary_request_and_response_pass_intact(make) -> None:
    h, _ = make()
    body = os.urandom(8 * 1024 * 1024) + bytes(range(256)) * 16
    with h.connect() as c:
        c.sendall(_framed(body))
        got = _read_framed(c)
    assert hashlib.sha256(got).digest() == hashlib.sha256(body).digest()
    assert _wait(lambda: "served ok" in h.log_text()), h.log_text()
    log = h.log_text()
    assert f"up {len(body) + 8} B, down {len(body) + 8} B" in log, log
    # The listener command as the machine will run it.
    args = h.socat_calls()[0][1]
    assert args == (
        f"-m dcb -u u socat -T30 -t30 TCP-LISTEN:{h.port},bind=127.0.0.1,reuseaddr,reuseport "
        "STDIO,shut-close"
    ), args


def test_no_upstream_connection_before_the_client_sends(make) -> None:
    h, up = make()
    with h.connect() as c:
        time.sleep(0.5)
        assert up.accepted == 0
        c.sendall(_framed(b"hello"))
        assert _read_framed(c) == b"hello"
    assert up.accepted == 1


def test_two_clients_are_served_in_parallel(make) -> None:
    barrier = threading.Barrier(2, timeout=5)

    def both_at_once(conn, _index):
        with conn:
            data = _read_framed(conn)
            try:
                barrier.wait()  # the first response waits until the second request has arrived
            except threading.BrokenBarrierError:
                data = b"not in parallel"
            conn.sendall(_framed(data))

    h, up = make(both_at_once, workers=2)
    c1 = h.connect()
    c1.sendall(_framed(b"one"))
    assert _wait(lambda: up.accepted == 1)
    c2 = h.connect()
    c2.sendall(_framed(b"two"))
    assert _read_framed(c1) == b"one"
    assert _read_framed(c2) == b"two"
    c1.close()
    c2.close()


def test_a_third_client_is_not_served_while_both_workers_are_busy(make) -> None:
    release = threading.Event()

    def held(conn, _index):
        with conn:
            data = _read_framed(conn)
            release.wait(10)
            conn.sendall(_framed(data))

    h, up = make(held, workers=2)
    c1 = h.connect()
    c1.sendall(_framed(b"one"))
    assert _wait(lambda: up.accepted == 1)
    c2 = h.connect()
    c2.sendall(_framed(b"two"))
    assert _wait(lambda: up.accepted == 2)
    try:
        c3 = socket.create_connection(("127.0.0.1", h.port), timeout=2)
    except ConnectionRefusedError:
        c3 = None  # no worker listening: refused, which the Bot retries
    else:
        try:
            c3.sendall(_framed(b"three"))
        except OSError:
            pass
        time.sleep(1)
        assert up.accepted == 2, "a busy worker took a third connection"
        c3.close()
    release.set()
    assert _read_framed(c1) == b"one"
    assert _read_framed(c2) == b"two"
    c1.close()
    c2.close()
    with h.connect() as c4:  # a worker that finished listens again
        c4.sendall(_framed(b"four"))
        assert _read_framed(c4) == b"four"


def test_a_listener_that_fails_at_once_backs_off(make) -> None:
    h, _ = make(workers=1, STUB_FAIL="1")
    time.sleep(3.6)
    n = len(h.socat_calls())
    # Starts at 0, 1, 3 s (1 s doubling); without backoff it would be hundreds.
    assert 2 <= n <= 4, n
    log = h.log_text()
    assert log.count("listener ended before any connection") == 1, log  # once per streak
    assert "Cannot assign requested address" in log, log


def test_a_client_that_sends_nothing_does_not_start_a_backoff(make) -> None:
    h, _ = make(workers=1)
    h.connect().close()
    # Wait for the next listener: a connection made while socat is still between accept() and
    # closing its listening socket is reset, not refused (measured with socat 1.7.4).
    assert _wait(lambda: len(h.socat_calls()) == 2)
    time.sleep(0.2)
    with h.connect() as c:
        c.sendall(_framed(b"next"))
        assert _read_framed(c) == b"next"
    assert "listener ended before any connection" not in h.log_text(), h.log_text()


def test_a_client_that_half_closes_still_gets_the_whole_response(make) -> None:
    def slow_until_eof(conn, _index):
        with conn:
            buf = bytearray()
            while data := conn.recv(65536):
                buf += data
            time.sleep(1.0)  # longer than socat's default -t of 0.5 s
            conn.sendall(bytes(buf[::-1]))

    h, _ = make(slow_until_eof)
    body = os.urandom(300_000)
    with h.connect() as c:
        c.sendall(body)
        c.shutdown(socket.SHUT_WR)
        got = bytearray()
        while data := c.recv(65536):
            got += data
    assert bytes(got) == body[::-1]


def test_an_unreachable_upstream_closes_the_client(make) -> None:
    h, up = make()
    up.close()  # nothing listens on the upstream port any more
    with h.connect() as c:
        c.sendall(_framed(b"x"))
        c.settimeout(5)
        assert c.recv(10) == b""
    assert _wait(lambda: "unreachable" in h.log_text()), h.log_text()


def test_sigterm_stops_the_relay_and_kills_its_listeners(make) -> None:
    h, _ = make(workers=2)
    assert _wait(lambda: len(h.socat_calls()) == 2)
    with h.connect() as c:  # one busy, one listening
        c.sendall(_framed(b"a"))
        assert _read_framed(c) == b"a"
        pids = [pid for pid, _ in h.socat_calls()]
        h.proc.send_signal(signal.SIGTERM)
        assert h.proc.wait(10) == 0

    def gone(pid: int) -> bool:
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            return True
        # A zombie whose parent has gone is reaped by init shortly after.
        return False

    assert _wait(lambda: all(gone(p) for p in pids)), pids
    assert "stopped" in h.log_text()
    # Stale listeners in the machine are removed at start and at stop.
    pkills = [line for line in h.calls.read_text().splitlines() if " pkill " in line]
    assert len(pkills) == 2 and f"TCP-LISTEN:{h.port},bind=127.0.0.1," in pkills[0], pkills


@pytest.mark.parametrize(
    ("key", "value"),
    [
        ("DCB_LLM_RELAY_LISTEN_PORT", "80"),
        ("DCB_LLM_RELAY_LISTEN_PORT", ""),
        ("DCB_LLM_RELAY_WORKERS", "0"),
        ("DCB_LLM_RELAY_LISTEN_ADDR", "1.2.3.4 -x"),
    ],
)
def test_a_bad_setting_refuses_to_start(tmp_path, key, value) -> None:
    env = {
        "PATH": "/usr/bin:/bin",
        "HOME": str(tmp_path),
        "DCB_LLM_RELAY_CONFIG": str(tmp_path / "none"),
        "DCB_LLM_RELAY_LISTEN_PORT": "18080",
        "DCB_LLM_RELAY_UPSTREAM_PORT": "8080",
        key: value,
    }
    out = subprocess.run([PYTHON, str(RELAY)], env=env, capture_output=True, text=True, timeout=10)
    assert out.returncode == 2 and key.split("_")[-1].lower() in out.stderr.lower(), out.stderr


def test_the_config_file_is_read_and_the_environment_overrides_it(tmp_path) -> None:
    cfg = tmp_path / "config"
    cfg.write_text('# comment\nDCB_LLM_RELAY_LISTEN_PORT=18080\nDCB_LLM_RELAY_WORKERS="3"\n')
    code = (
        "import importlib.util, json, sys\n"
        f"spec = importlib.util.spec_from_file_location('r', {str(RELAY)!r})\n"
        "m = importlib.util.module_from_spec(spec); spec.loader.exec_module(m)\n"
        "print(json.dumps(m.load_config()))\n"
    )
    env = {
        "PATH": "/usr/bin:/bin",
        "HOME": str(tmp_path),
        "DCB_LLM_RELAY_CONFIG": str(cfg),
        "DCB_LLM_RELAY_WORKERS": "4",
    }
    out = subprocess.run([PYTHON, "-c", code], env=env, capture_output=True, text=True, check=True)
    got = json.loads(out.stdout)
    assert got["DCB_LLM_RELAY_LISTEN_PORT"] == "18080"
    assert got["DCB_LLM_RELAY_WORKERS"] == "4"
    assert got["DCB_LLM_RELAY_UPSTREAM_HOST"] == "127.0.0.1"
