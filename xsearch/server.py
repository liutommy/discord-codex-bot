"""The X lookup sidecar: three fixed questions about X, answered by Grok Build through xAI's
server-side X search on the operator's Grok subscription. stdlib only.

Grok Build is a coding agent. Out of the box it can run shell commands, read and write files,
fetch URLs and start background jobs, and the obvious flags do not all remove them (a
`--disallowed-tools` entry spelled as the tool list spells it leaves the shell in place, and a
`monitor` tool runs commands too). Nothing a member writes reaches the prompt here — the bot
sends a post id or a handle, both validated below — but the posts Grok reads are untrusted
text, so the agent must have nothing to act with. Two layers prevent, one detects:

1. Prevent: every client tool is removed on the command line (`LOCKED_ARGS`), and permission
   rules deny the rest.
2. Prevent: a root-owned enforced policy (/etc/grok/requirements.toml) denies every PreToolUse.
3. Detect: fail-closed verification of what actually happened (`verify_stream`) — the session
   must start with an empty client toolset, use nothing but the server-side X search and emit
   only event shapes we know. Anything else and the answer is thrown away and logged. This
   cannot undo a tool that already ran; it is what tells us 1 and 2 failed (Grok's hooks fail
   open on error, this check does not).

Each lookup also runs in a fresh, empty Grok home and working directory on tmpfs, holding only
a copy of the login: nothing a session writes (config, hooks, skills, memory, project files)
can reach the next one, so persistence through the writable login volume is not a question.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import signal
import socket
import subprocess
import tempfile
import threading
import time
from collections import OrderedDict
from datetime import UTC, datetime
from email.utils import parsedate_to_datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

PORT = int(os.environ.get("XSEARCH_PORT", "8090"))
MODEL = os.environ.get("XSEARCH_MODEL", "grok-4.7")
TIMEOUT = int(os.environ.get("XSEARCH_TIMEOUT_SECONDS", "180"))
GROK = os.environ.get("XSEARCH_GROK_BIN", "grok")
# The persistent volume holds only the login; every session gets a throwaway home under SCRATCH.
AUTH_DIR = os.environ.get("XSEARCH_AUTH_DIR", "/var/lib/grok")
SCRATCH = os.environ.get("XSEARCH_SCRATCH", "/tmp")
QUEUE_WAIT = int(os.environ.get("XSEARCH_QUEUE_WAIT_SECONDS", "30"))
POST_CACHE_SECONDS = int(os.environ.get("XSEARCH_POST_CACHE_SECONDS", "3600"))
# Paths Grok 1.0.46 writes outside its home whatever HOME/GROK_HOME/TMPDIR say (it keeps a
# per-session directory under /tmp/sessions). Removed after every session like the home itself.
STRAY_PATHS = ("/tmp/sessions",)
MAX_BODY = 4096
MAX_TEXT = 4000
# One Grok session at a time: each spends subscription quota. A request that cannot get a turn
# within QUEUE_WAIT is turned away (429) instead of queueing work its caller has given up on.
_LOCK = threading.Lock()
_POST_CACHE: OrderedDict[str, tuple[float, dict]] = OrderedDict()
TWITTER_EPOCH_MS = 1288834974657
SNOWFLAKE_MIN = 1 << 32  # ids below this predate snowflakes (2006-2010) and carry no time

POST_ID = re.compile(r"[0-9]{1,19}")  # fullmatch only: `$` would let a trailing newline through
HANDLE = re.compile(r"[A-Za-z0-9_]{1,15}")
MAX_ID = (1 << 63) - 1

# Every client tool Grok Build 1.0.x offers, under every spelling it accepts.
_TOOLS = (
    "run_terminal_cmd,run_terminal_command,read_file,list_dir,grep,search_replace,write,"
    "spawn_subagent,Agent,monitor,workflow,web_fetch,web_search,image_gen,image_edit,"
    "image_to_video,reference_to_video,use_tool,search_tool,scheduler_create,scheduler_delete,"
    "scheduler_list,kill_command_or_subagent,get_command_or_subagent_output,todo_write,"
    "enter_plan_mode,exit_plan_mode,ask_user_question,send_feedback"
)
LOCKED_ARGS = (
    "--disallowed-tools", _TOOLS,
    "--deny", "Bash", "--deny", "Read", "--deny", "Edit", "--deny", "Write", "--deny", "Grep",
    "--disable-web-search", "--no-subagents", "--no-plan",
    "--max-turns", "8",
    "--output-format", "streaming-messages-json",
)  # fmt: skip
# Grok's own `--sandbox` needs bubblewrap, i.e. unprivileged user namespaces, which the hardened
# container (no capabilities, no-new-privileges, Docker's seccomp profile) does not allow — and
# Grok refuses to start rather than run unsandboxed. In the container the container is the
# sandbox; set XSEARCH_SANDBOX=strict when running this on a host where bubblewrap works.
SANDBOX = os.environ.get("XSEARCH_SANDBOX", "")
ENV = {
    "GROK_DISABLE_AUTOUPDATER": "1",
    "GROK_MEMORY": "0",  # no cross-session memory: one request must not shape the next
}

_POST = {
    "type": "object",
    "properties": {
        "id": {"type": "string"},
        "author_handle": {"type": "string"},
        "author_name": {"type": "string"},
        "created_at": {"type": "string"},
        "text": {"type": "string"},
        "is_reply": {"type": "boolean"},
        "is_repost": {"type": "boolean"},
        "quoted_text": {"type": "string"},
        "like_count": {"type": "integer"},
        "repost_count": {"type": "integer"},
        "reply_count": {"type": "integer"},
        "media": {"type": "array", "items": {"type": "string"}},
    },
    "required": ["id", "author_handle", "created_at", "text"],
}
SCHEMAS = {
    "post": {
        "type": "object",
        "properties": {"found": {"type": "boolean"}, "post": _POST},
        "required": ["found"],
    },
    "recent": {
        "type": "object",
        "properties": {"posts": {"type": "array", "items": _POST}},
        "required": ["posts"],
    },
    "user": {
        "type": "object",
        "properties": {
            "exists": {"type": "boolean"},
            "handle": {"type": "string"},
            "name": {"type": "string"},
            "description": {"type": "string"},
        },
        "required": ["exists"],
    },
}


def _is_id(value: object) -> bool:
    return type(value) is str and bool(POST_ID.fullmatch(value)) and int(value) <= MAX_ID


def _is_handle(value: object) -> bool:
    return type(value) is str and bool(HANDLE.fullmatch(value))


class BadRequest(ValueError):
    pass


class LookupFailed(RuntimeError):
    pass


class Busy(RuntimeError):
    pass


class Gone(RuntimeError):
    pass


class Unsafe(RuntimeError):
    """The session was not the locked-down one this service relies on."""


def build_prompt(kind: str, request: dict) -> str:
    """The whole prompt, from validated fields only."""
    if kind == "post":
        post_id = request.get("id", "")
        if not _is_id(post_id):
            raise BadRequest("id must be a numeric X post id")
        return (
            f"Use X search to look up the X post whose status id is {post_id}. If it exists, "
            "return it with its author, UTC time, full text, whether it is a reply or a repost, "
            "the text of any quoted post, engagement counts and media types. If it cannot be "
            "found, return found=false. Report only what X search returned."
        )
    handle = request.get("handle", "")
    handle = handle.lstrip("@") if type(handle) is str else handle
    if not _is_handle(handle):
        raise BadRequest("handle must be an X username")
    if kind == "user":
        return (
            f"Use X search to check whether the X account @{handle} exists. Return its handle, "
            "display name and bio, or exists=false. Report only what X search returned."
        )
    if kind == "recent":
        since = request.get("since_id", "") or ""
        if since and not _is_id(since):
            raise BadRequest("since_id must be a numeric X post id")
        limit = request.get("limit", 10)
        if type(limit) is not int or not 1 <= limit <= 20:
            raise BadRequest("limit must be 1-20")
        newer = f" with a status id greater than {since}" if since else ""
        return (
            f"Use X search to list up to {limit} of the most recent posts authored by "
            f"@{handle}{newer}, newest first. Include the account's replies and reposts but "
            "mark them. For each post give its numeric status id, UTC time, full text and "
            "the fields of the schema. Report only what X search returned."
        )
    raise BadRequest(f"unknown lookup {kind!r}")


# Event and content-block shapes Grok 1.0.x emits in streaming-messages-json. Anything else
# means the format moved under us, and an unknown shape is refused, not skipped.
_EVENTS = {("system", "init"), ("system", "compact_boundary"), ("assistant", None),
           ("user", None), ("result", None)}  # fmt: skip
_BLOCKS = {"assistant": {"text", "thinking", "tool_use"}, "user": {"tool_result", "text"}}
X_SEARCH_NAME = "X search:"


def verify_stream(lines: list[str]) -> dict:
    """The answer, but only from a session that had no client tools, used nothing except
    server-side X search, and emitted only event shapes we know. Raises Unsafe or LookupFailed.

    The tool check keys on the tool *name*, which the CLI fills in; `input` is written by the
    model and is only checked in addition."""
    inits = 0
    seen_other = False
    result = None
    for line in lines:
        if not line.strip():
            continue
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            raise Unsafe("session emitted a line that is not JSON") from None
        if not isinstance(event, dict):
            raise Unsafe("session emitted a non-object event")
        kind = event.get("type")
        subtype = event.get("subtype") if kind == "system" else None
        if (kind, subtype) not in _EVENTS:
            raise Unsafe(f"unknown event {kind!r}/{subtype!r}")
        if kind == "system" and subtype == "init":
            inits += 1
            if inits > 1 or seen_other:
                raise Unsafe("the toolset was announced again mid-session")
            if event.get("tools") != []:
                raise Unsafe(f"session started with client tools: {event.get('tools')!r}")
        elif kind in _BLOCKS:
            for block in (event.get("message") or {}).get("content") or []:
                block_type = block.get("type") if isinstance(block, dict) else None
                if block_type not in _BLOCKS[kind]:
                    raise Unsafe(f"unknown {kind} block {block_type!r}")
                if block_type == "tool_use":
                    if block.get("name") != X_SEARCH_NAME or block.get("input") != {
                        "variant": "XSearch",
                        "backend": True,
                    }:
                        raise Unsafe(
                            f"session used a tool other than X search: {block.get('name')!r}"
                        )
        elif kind == "result":
            result = event
        if kind != "system" or subtype != "init":
            seen_other = True
    if not inits:
        raise Unsafe("session reported no toolset")
    if result is None or result.get("is_error") or result.get("subtype") != "success":
        raise LookupFailed(f"grok did not finish: {(result or {}).get('subtype')}")
    answer = last_json_object(str(result.get("result") or ""))
    if answer is None:
        raise LookupFailed("grok returned no JSON answer")
    return answer


def last_json_object(text: str) -> dict | None:
    """The last complete JSON object in `text` (the model may wrap it in a code fence)."""
    decoder = json.JSONDecoder()
    found = None
    start = text.find("{")
    while start != -1:
        try:
            value, end = decoder.raw_decode(text, start)
        except json.JSONDecodeError:
            start = text.find("{", start + 1)
            continue
        if isinstance(value, dict):
            found = value  # top-level only: resume after it, never inside it
        start = text.find("{", end)
    return found


def _utc_iso(value: str) -> str | None:
    """ISO 8601 or RFC 2822 (what X search returns) as ISO 8601 UTC; None if neither."""
    try:
        moment = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        try:
            moment = parsedate_to_datetime(value)
        except (TypeError, ValueError):
            return None
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=UTC)
    return moment.astimezone(UTC).isoformat().replace("+00:00", "Z")


def _id_matches_time(post_id: str, created: str) -> bool:
    """X post ids are snowflakes: the top bits are the creation time. A post whose id and time
    disagree — or that claims to be from the future — was invented, e.g. by a post that talked
    the model into reporting `id=99999999999999999999`, which would otherwise become the
    tracking cursor and hide every real post behind it."""
    value = int(post_id)
    if value < SNOWFLAKE_MIN:
        return True  # pre-2010 ids carry no time; too small to ever move a cursor forward
    id_ms = (value >> 22) + TWITTER_EPOCH_MS
    created_ms = datetime.fromisoformat(created.replace("Z", "+00:00")).timestamp() * 1000
    now_ms = time.time() * 1000
    return abs(id_ms - created_ms) <= 10 * 60_000 and id_ms <= now_ms + 5 * 60_000


def _clean_post(post: object) -> dict | None:
    if not isinstance(post, dict):
        return None
    post_id = str(post.get("id", ""))
    created = _utc_iso(str(post.get("created_at", "")))
    if created is None or not _is_id(post_id) or not _id_matches_time(post_id, created):
        return None
    clean = {
        "id": post_id,
        "author_handle": str(post.get("author_handle", "")).lstrip("@")[:15],
        "author_name": str(post.get("author_name", ""))[:100],
        "created_at": created,
        "text": str(post.get("text", ""))[:MAX_TEXT],
        "is_reply": bool(post.get("is_reply", False)),
        "is_repost": bool(post.get("is_repost", False)),
        "quoted_text": str(post.get("quoted_text", ""))[:MAX_TEXT],
        "media": [str(m)[:20] for m in (post.get("media") or [])][:10],
    }
    for count in ("like_count", "repost_count", "reply_count"):
        if isinstance(post.get(count), int):
            clean[count] = post[count]
    return clean


def shape(kind: str, request: dict, answer: dict) -> dict:
    """Re-check the model's answer against the request: ids, handles and ordering are facts
    the request fixes, so a post that contradicts them is dropped rather than trusted."""
    if kind == "post":
        post = _clean_post(answer.get("post")) if answer.get("found") is True else None
        if post and post["id"] != str(request["id"]):
            post = None
        return {"found": post is not None, "post": post}
    if kind == "user":
        handle = str(request["handle"]).lstrip("@")
        same = str(answer.get("handle", handle)).lstrip("@").lower() == handle.lower()
        exists = answer.get("exists") is True and same
        return {
            "exists": exists,
            "handle": handle,
            "name": str(answer.get("name", ""))[:100] if exists else "",
            "description": str(answer.get("description", ""))[:500] if exists else "",
        }
    handle = str(request["handle"]).lstrip("@").lower()
    since = int(request.get("since_id") or 0)
    posts = []
    for raw in answer.get("posts") or []:
        post = _clean_post(raw)
        if post and post["author_handle"].lower() == handle and int(post["id"]) > since:
            posts.append(post)
    posts.sort(key=lambda p: int(p["id"]), reverse=True)
    return {"posts": posts[: int(request.get("limit", 10))]}


def _fresh_home() -> tuple[str, dict]:
    """A throwaway Grok home and working directory holding only a copy of the login."""
    scratch = tempfile.mkdtemp(prefix="xsearch-", dir=SCRATCH)
    home, work = os.path.join(scratch, "home"), os.path.join(scratch, "work")
    os.makedirs(os.path.join(home, ".grok"), mode=0o700)
    os.makedirs(work, mode=0o700)
    os.makedirs(os.path.join(scratch, "tmp"), mode=0o700)
    source = os.path.join(AUTH_DIR, "auth.json")
    if os.path.exists(source):
        shutil.copyfile(source, os.path.join(home, ".grok", "auth.json"))
        os.chmod(os.path.join(home, ".grok", "auth.json"), 0o600)
    env = {
        "HOME": home,
        "GROK_HOME": os.path.join(home, ".grok"),
        "XDG_CONFIG_HOME": os.path.join(home, ".config"),
        "XDG_DATA_HOME": os.path.join(home, ".local", "share"),
        "XDG_CACHE_HOME": os.path.join(home, ".cache"),
        "XDG_STATE_HOME": os.path.join(home, ".local", "state"),
        "TMPDIR": os.path.join(scratch, "tmp"),  # Grok keeps per-session dirs under $TMPDIR
    }
    return scratch, env


def _keep_login(scratch: str) -> None:
    """Carry a refreshed login back to the volume — the only thing that outlives a session —
    and only if it is still a JSON object with the same keys as before: a refresh changes token
    values, never the shape, so anything else (say, an extra endpoint field) is not kept."""
    fresh = os.path.join(scratch, "home", ".grok", "auth.json")
    target = os.path.join(AUTH_DIR, "auth.json")
    try:
        with open(fresh, "rb") as handle:
            data = handle.read()
        with open(target, "rb") as handle:
            current = handle.read()
        new, old = json.loads(data), json.loads(current)
        if data == current or not isinstance(new, dict) or not isinstance(old, dict):
            return
        if set(new) != set(old):
            print("login file changed shape; not kept", flush=True)
            return
    except (OSError, ValueError):
        return
    staging = f"{target}.new"
    with open(os.open(staging, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600), "wb") as handle:
        handle.write(data)
    os.replace(staging, target)


def run_grok(prompt: str) -> list[str]:
    scratch, home_env = _fresh_home()
    try:
        return _run(prompt, home_env, os.path.join(scratch, "work"))
    finally:
        _keep_login(scratch)
        for path in (scratch, *STRAY_PATHS):
            shutil.rmtree(path, ignore_errors=True)


def _run(prompt: str, home_env: dict, cwd: str) -> list[str]:
    command = [
        GROK, "-m", MODEL, "-p", prompt, "--cwd", cwd, *LOCKED_ARGS,
        *(("--sandbox", SANDBOX) if SANDBOX else ()),
    ]  # fmt: skip
    process = subprocess.Popen(
        command,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        cwd=cwd,
        env={"PATH": os.environ.get("PATH", "/usr/local/bin:/usr/bin:/bin"), **home_env, **ENV},
        start_new_session=True,
    )
    try:
        stdout, stderr = process.communicate(timeout=TIMEOUT)
    except subprocess.TimeoutExpired:
        os.killpg(process.pid, signal.SIGKILL)
        process.communicate()
        raise LookupFailed("grok timed out") from None
    if process.returncode != 0:
        raise LookupFailed(f"grok exited {process.returncode}: {stderr.strip()[-200:]}")
    return stdout.splitlines()


def lookup(kind: str, request: dict, still_wanted=lambda: True) -> dict:
    prompt = (
        f"{build_prompt(kind, request)}\n\nSearch first. Then reply with exactly one JSON object "
        f"and nothing else, matching this JSON Schema: {json.dumps(SCHEMAS[kind])}"
    )
    key = f"{kind}:{request.get('id') or request.get('handle')}"
    if kind in ("post", "user"):
        cached = _POST_CACHE.get(key)
        if cached and time.monotonic() - cached[0] < POST_CACHE_SECONDS:
            return cached[1]
    if not _LOCK.acquire(timeout=QUEUE_WAIT):
        raise Busy("another lookup is running")
    try:
        if not still_wanted():  # the caller gave up while queued: do not spend a session on it
            raise Gone("client went away")
        lines = run_grok(prompt)
    finally:
        _LOCK.release()
    answer = shape(kind, request, verify_stream(lines))
    if kind in ("post", "user"):  # found or not: repeats of the same question cost nothing
        _POST_CACHE[key] = (time.monotonic(), answer)
        while len(_POST_CACHE) > 256:
            _POST_CACHE.popitem(last=False)
    return answer


MAX_CONNECTIONS = 16
_CONNECTIONS = threading.BoundedSemaphore(MAX_CONNECTIONS)


class Handler(BaseHTTPRequestHandler):
    timeout = 10  # a client that does not send its request promptly is dropped

    def handle(self) -> None:
        if not _CONNECTIONS.acquire(blocking=False):
            return  # too many open connections: close this one at once
        try:
            super().handle()
        finally:
            _CONNECTIONS.release()

    def _client_waiting(self) -> bool:
        """False once the client has closed its end (a read would return EOF at once)."""
        try:
            previous = self.connection.gettimeout()  # the handler's 10 s, kept for the reply
            self.connection.setblocking(False)
            try:
                return self.connection.recv(1, socket.MSG_PEEK) != b""
            except BlockingIOError:
                return True
            finally:
                self.connection.settimeout(previous)
        except OSError:
            return False

    def _json(self, status: int, body: dict) -> None:
        data = json.dumps(body, ensure_ascii=False).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self) -> None:
        self._json(200 if self.path == "/health" else 404, {"ok": self.path == "/health"})

    def do_POST(self) -> None:
        kind = self.path.removeprefix("/x/")
        if kind not in SCHEMAS:
            return self._json(404, {"error": "not found"})
        raw_length = self.headers.get("Content-Length", "")
        if not raw_length.isdigit():
            return self._json(411, {"error": "Content-Length required"})
        length = int(raw_length)
        if length > MAX_BODY:
            return self._json(413, {"error": "request too large"})
        try:
            request = json.loads(self.rfile.read(length) or b"{}")
            if not isinstance(request, dict):
                raise BadRequest("body must be a JSON object")
            return self._json(200, lookup(kind, request, self._client_waiting))
        except (json.JSONDecodeError, BadRequest) as error:
            return self._json(400, {"error": str(error)})
        except Busy:
            return self._json(429, {"error": "busy"})
        except Gone:
            return None
        except Unsafe as error:
            print(f"REFUSED unsafe grok session: {error}", flush=True)
            return self._json(503, {"error": "lookup refused"})
        except LookupFailed as error:
            print(f"lookup failed: {error}", flush=True)
            return self._json(502, {"error": "lookup failed"})

    def log_message(self, fmt: str, *args) -> None:
        print(f"{self.address_string()} {fmt % args}", flush=True)


if __name__ == "__main__":
    ThreadingHTTPServer(("0.0.0.0", PORT), Handler).serve_forever()
