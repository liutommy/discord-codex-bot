"""The sandbox sidecar: a tiny HTTP service that runs one snippet at a time under hard limits.
It lives in its own container (non-root, read-only rootfs, no capabilities, no egress network)
and is reachable only from the bot over an internal compose network. stdlib only."""

from __future__ import annotations

import base64
import json
import os
import resource
import shutil
import subprocess
import tempfile
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

WORK = os.environ.get("SANDBOX_WORK", "/work")
MAX_TIMEOUT = int(os.environ.get("SANDBOX_MAX_TIMEOUT", "30"))
MAX_OUTPUT = 20_000
MAX_FILES = 5
MAX_FILE_BYTES = 4_000_000
# Virtual address space cap (RLIMIT_AS). Real memory is bounded by the container cgroup;
# this only needs to be roomy enough for python + numpy/OpenBLAS to map their arenas.
MEMORY_BYTES = int(os.environ.get("SANDBOX_MEMORY_BYTES", str(2 * 1024 * 1024 * 1024)))
LANGS = {"python": ["python3", "main.py"], "sh": ["sh", "main.sh"]}
_LOCK = threading.Lock()


def _limits() -> None:
    resource.setrlimit(resource.RLIMIT_AS, (MEMORY_BYTES, MEMORY_BYTES))
    resource.setrlimit(resource.RLIMIT_NPROC, (64, 64))
    resource.setrlimit(resource.RLIMIT_FSIZE, (MAX_FILE_BYTES * 2, MAX_FILE_BYTES * 2))
    resource.setrlimit(resource.RLIMIT_CORE, (0, 0))


def execute(lang: str, code: str, timeout: int) -> dict:
    """Run `code` as `lang` in a fresh directory; return exit code, bounded output and the
    files the snippet left under ./out/ (base64, capped)."""
    if lang not in LANGS:
        return {"error": f"unsupported lang {lang!r}; use python or sh"}
    timeout = max(1, min(int(timeout or MAX_TIMEOUT), MAX_TIMEOUT))
    os.makedirs(WORK, exist_ok=True)
    workdir = tempfile.mkdtemp(prefix="run-", dir=WORK)
    try:
        os.makedirs(os.path.join(workdir, "out"))
        with open(os.path.join(workdir, LANGS[lang][1]), "w", encoding="utf-8") as handle:
            handle.write(code)
        env = {"PATH": "/usr/local/bin:/usr/bin:/bin", "HOME": workdir, "LANG": "C.UTF-8",
               "PYTHONIOENCODING": "utf-8", "PYTHONDONTWRITEBYTECODE": "1", "TMPDIR": workdir,
               # BLAS thread pools deadlock under RLIMIT_NPROC/AS (numpy import hung 15 s)
               "OPENBLAS_NUM_THREADS": "1", "OMP_NUM_THREADS": "1", "MKL_NUM_THREADS": "1"}
        try:
            proc = subprocess.run(
                LANGS[lang], cwd=workdir, env=env, capture_output=True, timeout=timeout,
                preexec_fn=_limits, stdin=subprocess.DEVNULL,
            )
            exit_code, stdout, stderr = proc.returncode, proc.stdout, proc.stderr
            timed_out = False
        except subprocess.TimeoutExpired as expired:
            exit_code, timed_out = -1, True
            stdout, stderr = expired.stdout or b"", expired.stderr or b""
        files = []
        total = 0
        out_dir = os.path.join(workdir, "out")
        for name in sorted(os.listdir(out_dir))[:MAX_FILES]:
            path = os.path.join(out_dir, name)
            if not os.path.isfile(path):
                continue
            size = os.path.getsize(path)
            if size > MAX_FILE_BYTES or total + size > MAX_FILE_BYTES:
                files.append({"name": name, "skipped": f"{size} bytes over the cap"})
                continue
            with open(path, "rb") as handle:
                files.append({"name": name, "b64": base64.b64encode(handle.read()).decode()})
            total += size
        return {
            "exit": exit_code,
            "timed_out": timed_out,
            "stdout": stdout.decode("utf-8", "replace")[:MAX_OUTPUT],
            "stderr": stderr.decode("utf-8", "replace")[-5000:],
            "files": files,
        }
    finally:
        shutil.rmtree(workdir, ignore_errors=True)


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *args) -> None:  # quiet
        pass

    def _json(self, status: int, payload: dict) -> None:
        body = json.dumps(payload).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self) -> None:
        self._json(200 if self.path == "/health" else 404, {"ok": self.path == "/health"})

    def do_POST(self) -> None:
        if self.path != "/run":
            self._json(404, {"error": "not found"})
            return
        length = int(self.headers.get("Content-Length") or 0)
        if length > 200_000:
            self._json(413, {"error": "code too large"})
            return
        try:
            request = json.loads(self.rfile.read(length) or b"{}")
        except json.JSONDecodeError:
            self._json(400, {"error": "bad json"})
            return
        with _LOCK:  # one snippet at a time: the limits above are per process, not shared
            result = execute(
                str(request.get("lang") or "python"), str(request.get("code") or ""),
                int(request.get("timeout") or MAX_TIMEOUT),
            )
        self._json(200 if "error" not in result else 400, result)


if __name__ == "__main__":
    port = int(os.environ.get("SANDBOX_PORT", "8070"))
    ThreadingHTTPServer(("0.0.0.0", port), Handler).serve_forever()
