#!/usr/bin/python3
"""Relay from the dcb OrbStack machine to the Mac's local LLM server (launchd, KeepAlive).

The machine is isolated: it cannot connect to the Mac, and OrbStack's ssh proxy refuses `ssh -R`.
The Mac originates the path instead. Each worker runs

    orb -m <machine> -u <user> socat -T<idle> -t<idle> \
        TCP-LISTEN:<port>,bind=<addr>,reuseaddr,reuseport STDIO,shut-close

which keeps one listener open inside the machine (`reuseport` lets every worker listen on the same
address at once; the kernel picks one for a new connection). socat without `fork` serves one
connection and stops listening, so a worker busy with a connection has no listener: with every
worker busy, a new connection is refused, which the Bot retries. When the first bytes of a
connection arrive on the worker's pipe, and only then, the worker connects to the upstream server
and splices both ways with half-close: client EOF shuts down the upstream's write side, upstream
EOF closes socat's stdin. socat 1.7.4 (Debian 12) never passes the client's EOF on to a pipe by
itself; `shut-close` makes it close its stdout at that EOF, and `-t<idle>` keeps it forwarding the
response afterwards (its default gives up 0.5 s after the client's EOF). Once both directions
have ended socat exits at once, so a closed client does not hold the worker for `-t`.

A socat that exits before sending a byte with a non-zero status (the stack is down so the address
does not exist, orb fails, socat is missing) starts a backoff of 1 s doubling to 30 s, logged once
per streak and reset after a served connection; a zero status there is a client that connected
and closed without sending anything, and the worker starts a new listener at once.

The log has one line per served connection (bytes each way, duration) and never any request or
response content. SIGTERM / SIGINT stop the workers and kill their processes; listeners a killed
orb may leave behind in the machine are removed with pkill at start and stop.

Config: ~/.config/dcb-llm-relay/config (KEY=value, see dcb-llm-relay.config.example); an
environment variable of the same name overrides the file. Runs on the system python3 (3.9).
"""

from __future__ import annotations

import getpass
import os
import re
import select
import signal
import socket
import subprocess
import sys
import threading
import time

DEFAULTS = {
    "DCB_LLM_RELAY_MACHINE": "dcb",
    "DCB_LLM_RELAY_USER": "",  # empty: the Mac's user name (OrbStack's default machine user)
    "DCB_LLM_RELAY_LISTEN_ADDR": "172.31.254.1",
    "DCB_LLM_RELAY_LISTEN_PORT": "",
    "DCB_LLM_RELAY_UPSTREAM_HOST": "127.0.0.1",
    "DCB_LLM_RELAY_UPSTREAM_PORT": "",
    "DCB_LLM_RELAY_WORKERS": "2",
    "DCB_LLM_RELAY_IDLE": "900",
    "DCB_LLM_RELAY_LOG": "~/Library/Logs/dcb-llm-relay.log",
}
BACKOFF_MIN, BACKOFF_MAX = 1.0, 30.0
GRACE = 2.0  # seconds a finished socat gets to exit before it is killed
CHUNK = 65536


class ConfigError(Exception):
    pass


def load_config() -> dict[str, str]:
    cfg = dict(DEFAULTS)
    path = os.environ.get(
        "DCB_LLM_RELAY_CONFIG", os.path.expanduser("~/.config/dcb-llm-relay/config")
    )
    if os.path.exists(path):
        with open(path, encoding="utf-8") as f:
            for n, raw in enumerate(f, 1):
                line = raw.strip()
                if not line or line.startswith("#"):
                    continue
                key, sep, value = line.partition("=")
                key, value = key.strip(), value.strip()
                if not sep or key not in DEFAULTS:
                    raise ConfigError(f"{path}:{n}: unknown or malformed line")
                if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
                    value = value[1:-1]
                cfg[key] = value
    for key in DEFAULTS:
        if key in os.environ:
            cfg[key] = os.environ[key]
    return cfg


def _int(cfg: dict[str, str], key: str, low: int, high: int) -> int:
    value = cfg[key]
    if not re.fullmatch(r"[0-9]+", value) or not low <= int(value) <= high:
        raise ConfigError(f"{key} must be an integer {low}-{high} (got {value!r})")
    return int(value)


class Relay:
    def __init__(self, cfg: dict[str, str]) -> None:
        self.machine = cfg["DCB_LLM_RELAY_MACHINE"]
        self.user = cfg["DCB_LLM_RELAY_USER"] or getpass.getuser()
        self.addr = cfg["DCB_LLM_RELAY_LISTEN_ADDR"]
        self.port = _int(cfg, "DCB_LLM_RELAY_LISTEN_PORT", 1024, 65535)
        self.up_host = cfg["DCB_LLM_RELAY_UPSTREAM_HOST"]
        self.up_port = _int(cfg, "DCB_LLM_RELAY_UPSTREAM_PORT", 1, 65535)
        self.workers = _int(cfg, "DCB_LLM_RELAY_WORKERS", 1, 64)
        self.idle = _int(cfg, "DCB_LLM_RELAY_IDLE", 1, 86400)
        for key, value in (("machine", self.machine), ("user", self.user), ("addr", self.addr)):
            if not re.fullmatch(r"[A-Za-z0-9._-]+", value):
                raise ConfigError(f"{key} {value!r} has characters other than A-Z a-z 0-9 . _ -")
        self.log_path = os.path.expanduser(cfg["DCB_LLM_RELAY_LOG"])
        self.stop = threading.Event()
        self.lock = threading.RLock()  # the signal handler may run while the main thread logs
        self.children: set[subprocess.Popen] = set()

    # --- logging: never request or response content ---
    def log(self, msg: str) -> None:
        line = f"{time.strftime('%Y-%m-%d %H:%M:%S')} dcb-llm-relay: {msg}\n"
        with self.lock:
            try:
                with open(self.log_path, "a", encoding="utf-8") as f:
                    f.write(line)
            except OSError:  # a full disk or an unwritable log must not end a worker
                sys.stderr.write(line)

    def orb(self, *cmd: str) -> list[str]:
        return ["orb", "-m", self.machine, "-u", self.user, *cmd]

    def listener_cmd(self) -> list[str]:
        listen = f"TCP-LISTEN:{self.port},bind={self.addr},reuseaddr,reuseport"
        return self.orb("socat", f"-T{self.idle}", f"-t{self.idle}", listen, "STDIO,shut-close")

    def remove_stale_listeners(self) -> None:
        """A killed orb client may leave its socat listening in the machine; remove those."""
        pattern = f"TCP-LISTEN:{self.port},bind={self.addr},"
        try:
            subprocess.run(
                self.orb("pkill", "-u", self.user, "-f", pattern),
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                timeout=15,
            )
        except (OSError, subprocess.TimeoutExpired) as e:
            self.log(f"could not remove stale listeners: {type(e).__name__}")

    # --- child processes ---
    def start(self) -> subprocess.Popen:
        proc = subprocess.Popen(
            self.listener_cmd(),
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            start_new_session=True,  # its own process group: kill() reaches what orb starts
        )
        with self.lock:
            self.children.add(proc)
        if self.stop.is_set():  # the signal handler may have run before it was registered
            self.kill(proc)
        return proc

    def kill(self, proc: subprocess.Popen, sig: int = signal.SIGKILL) -> None:
        try:
            os.killpg(proc.pid, sig)
        except (ProcessLookupError, PermissionError):
            pass

    def reap(self, proc: subprocess.Popen) -> tuple[int, str]:
        """Wait for the process (killing it after GRACE); return its status and last stderr line."""
        for pipe in (proc.stdin, proc.stdout):
            try:
                pipe.close()
            except OSError:
                pass
        try:
            rc = proc.wait(GRACE)
        except subprocess.TimeoutExpired:
            self.kill(proc, signal.SIGTERM)
            try:
                rc = proc.wait(1)
            except subprocess.TimeoutExpired:
                self.kill(proc)
                rc = proc.wait()
        err = b""
        if select.select([proc.stderr], [], [], 0)[0]:
            err = os.read(proc.stderr.fileno(), 4096)
        proc.stderr.close()
        with self.lock:
            self.children.discard(proc)
        lines = err.decode("utf-8", "replace").strip().splitlines()
        return rc, (lines[-1][:200] if lines else "")

    def kill_all(self) -> None:
        with self.lock:
            procs = list(self.children)
        for proc in procs:
            self.kill(proc)

    # --- one worker ---
    def worker(self, n: int) -> None:
        delay = 0.0
        while not self.stop.is_set():
            try:
                proc = self.start()
            except OSError as e:  # orb not found
                served, rc, err = False, -1, f"cannot start orb: {e.strerror}"
            else:
                served, rc, err = self.serve(n, proc)
            if served or (rc == 0 and not self.stop.is_set()):
                if delay:
                    self.log(f"worker {n}: serving again after backoff")
                delay = 0.0
                continue
            if self.stop.is_set():
                break
            if not delay:
                self.log(
                    f"worker {n}: listener ended before any connection (exit {rc}"
                    f"{': ' + err if err else ''}); retrying with backoff up to "
                    f"{BACKOFF_MAX:.0f}s"
                )
            delay = BACKOFF_MIN if not delay else min(delay * 2, BACKOFF_MAX)
            self.stop.wait(delay)

    def serve(self, n: int, proc: subprocess.Popen) -> tuple[bool, int, str]:
        """Serve the one connection this socat accepts. Returns (served, exit status, stderr)."""
        out = proc.stdout.fileno()
        try:
            first = os.read(out, CHUNK)  # blocks until a client connects and sends
        except OSError:
            first = b""
        if not first:
            rc, err = self.reap(proc)
            return False, rc, err
        started = time.monotonic()
        try:
            upstream = socket.create_connection((self.up_host, self.up_port), timeout=10)
        except OSError as e:
            self.reap(proc)  # closing its stdin ends the client's connection
            self.log(
                f"worker {n}: upstream {self.up_host}:{self.up_port} unreachable "
                f"({e.strerror or type(e).__name__}); closed the client's connection"
            )
            return True, 0, ""
        upstream.settimeout(self.idle)  # like socat -T: a silent upstream does not hold the worker
        counts = {"up": 0, "down": 0}
        failed: list[str] = []

        def teardown(why: str) -> None:
            failed.append(why)
            try:
                upstream.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass
            self.kill(proc)

        def client_to_upstream() -> None:
            data = first
            try:
                while data:
                    upstream.sendall(data)
                    counts["up"] += len(data)
                    data = os.read(out, CHUNK)
                upstream.shutdown(socket.SHUT_WR)  # client EOF
            except OSError as e:
                if not failed:
                    teardown(f"client->upstream: {type(e).__name__}")

        pump = threading.Thread(target=client_to_upstream, daemon=True)
        pump.start()
        stdin = proc.stdin
        try:
            while True:
                data = upstream.recv(CHUNK)
                if not data:
                    break
                stdin.write(data)
                stdin.flush()
                counts["down"] += len(data)
            stdin.close()  # upstream EOF: socat shuts down the client's write side
        except (OSError, ValueError) as e:
            if not failed:
                teardown(f"upstream->client: {type(e).__name__}")
        pump.join()
        upstream.close()
        rc, _ = self.reap(proc)
        outcome = "error " + failed[0] if failed else "ok"
        self.log(
            f"worker {n}: served {outcome}; up {counts['up']} B, down {counts['down']} B, "
            f"{time.monotonic() - started:.2f}s"
        )
        return True, rc, ""

    def run(self) -> int:
        def on_signal(signum: int, _frame: object) -> None:
            self.stop.set()
            self.kill_all()

        signal.signal(signal.SIGTERM, on_signal)
        signal.signal(signal.SIGINT, on_signal)
        self.log(
            f"start: {self.workers} worker(s), {self.machine} {self.addr}:{self.port} -> "
            f"{self.up_host}:{self.up_port}"
        )
        self.remove_stale_listeners()
        threads = [
            threading.Thread(target=self.worker, args=(n,), daemon=True)
            for n in range(1, self.workers + 1)
        ]
        for t in threads:
            t.start()
        failed = False
        while not self.stop.is_set():
            self.stop.wait(1)
            if not self.stop.is_set() and not all(t.is_alive() for t in threads):
                # A worker that died serves nothing; exit so launchd's KeepAlive restarts the relay.
                self.log("a worker ended unexpectedly; exiting")
                failed = True
                self.stop.set()
        self.kill_all()
        deadline = time.monotonic() + GRACE + 3
        for t in threads:
            t.join(max(0.0, deadline - time.monotonic()))
        self.kill_all()
        self.remove_stale_listeners()
        self.log("stopped")
        return 1 if failed else 0


def main() -> int:
    extra = [os.path.expanduser("~/.orbstack/bin"), "/opt/homebrew/bin", "/usr/local/bin"]
    os.environ["PATH"] = os.pathsep.join([os.environ.get("PATH", "/usr/bin:/bin"), *extra])
    try:
        relay = Relay(load_config())
    except ConfigError as e:
        print(f"dcb-llm-relay: {e}", file=sys.stderr)
        return 2
    os.makedirs(os.path.dirname(relay.log_path) or ".", exist_ok=True)
    return relay.run()


if __name__ == "__main__":
    sys.exit(main())
