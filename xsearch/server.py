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

One exception, only on a chat turn that carries images: the CLI takes an image only inline on
its command line (capped at 128 KiB per argument by Linux) or as a tool result, so such a session
gets exactly one tool, `images__get_image` from image_mcp.py, which returns attached image N and
can reach nothing else. Each layer admits that one name and nothing more: layer 1 keeps every
other tool removed and adds an `--allow` rule for it, layer 2's hook stays out of its way, and
layer 3 accepts calls to it alone.

Each lookup also runs in a fresh, empty Grok home and working directory on tmpfs, holding only
a copy of the login: nothing a session writes (config, hooks, skills, memory, project files)
can reach the next one, so persistence through the writable login volume is not a question.
"""

from __future__ import annotations

import base64
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
import urllib.error
import urllib.request
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
# Chat: one member turn per session, its prompt (persona, history, the message) from the Bot.
CHAT_PARALLEL = int(os.environ.get("XSEARCH_CHAT_PARALLEL", "2"))
CHAT_TIMEOUT = int(os.environ.get("XSEARCH_CHAT_TIMEOUT_SECONDS", "300"))
# Images a chat turn may carry (sent base64 in the body, written to the session's tmpfs scratch;
# CHAT_PARALLEL of them at once must fit there). xAI takes PNG and JPEG, up to 20 MiB each. An
# image turn costs about 7x its images in memory (measured: two chats at 23 MiB each plus a lookup
# peaked at 533 of 768 MiB, against 212 without images), hence a total well under xAI's.
MAX_IMAGES = 8
MAX_IMAGE_BYTES = 10 * 1024 * 1024
MAX_IMAGES_BYTES = 16 * 1024 * 1024
IMAGE_TYPES = {b"\x89PNG\r\n\x1a\n": ".png", b"\xff\xd8\xff": ".jpg"}  # by content, not by name
# 200k characters of CJK are 600 KB of UTF-8; images grow by a third in base64
MAX_CHAT_BODY = 1024 * 1024 + MAX_IMAGES_BYTES * 4 // 3 + 64 * 1024
MAX_PROMPT_CHARS = 200_000
MAX_SYSTEM_BYTES = 100_000  # passed as one argv value: Linux caps a single argument at 128 KiB
MODELS_CACHE_SECONDS = 6 * 3600
USAGE_CACHE_SECONDS = 60
BILLING_URL = "https://cli-chat-proxy.grok.com/v1/billing?format=credits"
LIVE_CACHE_STATUS = {"DYNAMIC", "MISS", "EXPIRED", "BYPASS", "REVALIDATED"}
# A session that might refresh the login runs alone (see _SessionGate): this far from expiry.
REFRESH_MARGIN_SECONDS = max(TIMEOUT, CHAT_TIMEOUT) + 600
EFFORT = re.compile(r"[a-z]{1,16}")
MODEL_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,63}")
# --verbatim does not stop file mentions: `@ /x`, `@\t/x`, `@\n/x` (any whitespace after the
# `@`) and --prompt-json text blocks were all still expanded into the file. So the CLI never
# sees an ASCII `@` at all (see defuse_mentions). A login string in the output is refused too.
FULLWIDTH_AT = "\uff20"
AT_SIGNS = "@" + FULLWIDTH_AT  # a handle as written back by the model
WORD_JOINER = "\u2060"
MIN_SECRET_CHARS = 20
# Lookups run one at a time and chat turns CHAT_PARALLEL at a time (each session spends plan
# quota). A request that cannot get a turn within QUEUE_WAIT is turned away (429) instead of
# queueing work its caller has given up on.
_LOCK = threading.Lock()
_CHAT_SLOTS = threading.BoundedSemaphore(CHAT_PARALLEL)
_AUTH_LOCK = threading.Lock()  # every read and write of the login file
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
    # The prompt goes to the model exactly as written. Without this the CLI expands `@<path>`
    # mentions into file contents (the login, /proc/self/environ, other sessions' prompts) and
    # runs leading slash commands — on the client, with no tool call for anything below to see.
    "--verbatim",
    "--disallowed-tools", _TOOLS,
    "--deny", "Bash", "--deny", "Read", "--deny", "Edit", "--deny", "Write", "--deny", "Grep",
    "--disable-web-search", "--no-subagents", "--no-plan",
    "--max-turns", "8",
    "--output-format", "streaming-messages-json",
)  # fmt: skip
# The image tool (image_mcp.py, server `images`, tool `get_image`). The CLI offers MCP tools
# through its `use_tool` dispatcher, so an image session gets `use_tool` back; the hook sees the
# dispatched name, and the permission rules still deny every built-in tool behind it.
IMAGE_TOOL = "images__get_image"
IMAGE_SERVER = ("/usr/local/bin/python3", "-I", "/srv/image_mcp.py")  # pinned in requirements.toml
IMAGE_ARGS = (
    *(",".join(t for t in _TOOLS.split(",") if t != "use_tool") if a == _TOOLS else a
      for a in LOCKED_ARGS),
    "--allow", IMAGE_TOOL,
)  # fmt: skip
IMAGE_NOTE = (
    "The member's message comes with {count} attached image(s), numbered 1 to {count}. You "
    "cannot see them until you fetch them: before answering, call the tool `use_tool` with "
    '{{"tool_name": "' + IMAGE_TOOL + '", "tool_input": {{"index": N}}}} once for each N from 1 '
    "to {count}. That is the only tool you may call; any other tool call ends the session "
    "without an answer."
)
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


class QuotaExhausted(LookupFailed):
    pass


class LoginFailed(LookupFailed):
    pass


# Matched against what the CLI itself says (its stderr) and the result's subtype — never against
# the result text, which can be the model's own words: a member can talk the model into writing
# "quota exceeded, please sign in again", and that must not read as a spent plan or a lost login.
_QUOTA = re.compile(
    r"\b429\b|rate.?limit|quota|usage.?limit|insufficient.?credit|credits? (exhausted|depleted)",
    re.I,
)
_LOGIN = re.compile(
    r"\b401\b|unauthori[sz]ed|(token|session|login) (has )?(expired|is invalid|revoked)"
    r"|not (logged|signed) in|please (log|sign) ?in",
    re.I,
)


def classify_failure(summary: str, cli_output: str = "") -> LookupFailed:
    """What a failed session means for the caller: the plan's quota is spent, the login is gone
    (both: try another backend), or anything else. `cli_output` is the CLI's stderr only."""
    text = f"{summary} {cli_output}"
    if _QUOTA.search(text):
        return QuotaExhausted(summary)
    if _LOGIN.search(text):
        return LoginFailed(summary)
    return LookupFailed(summary)


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
    handle = handle.lstrip(AT_SIGNS) if type(handle) is str else handle
    if not _is_handle(handle):
        raise BadRequest("handle must be an X username")
    if kind == "user":
        return (
            f"Use X search to check whether the X account with username {handle} exists. "
            "Return its handle, display name and bio, or exists=false. Report only what X "
            "search returned."
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
            f"Use X search to list up to {limit} of the most recent posts authored by the "
            f"account with username {handle}{newer}, newest first. Include the account's "
            "replies and reposts but mark them. For each post give its numeric status id, UTC "
            "time, full text and the fields of the schema. Report only what X search returned."
        )
    raise BadRequest(f"unknown lookup {kind!r}")


# Event and content-block shapes Grok 1.0.x emits in streaming-messages-json. Anything else
# means the format moved under us, and an unknown shape is refused, not skipped.
_EVENTS = {("system", "init"), ("system", "compact_boundary"), ("assistant", None),
           ("user", None), ("result", None)}  # fmt: skip
_BLOCKS = {"assistant": {"text", "thinking", "tool_use"}, "user": {"tool_result", "text"}}
X_SEARCH_NAME = "X search:"


def _image_call(block: dict) -> bool:
    """A call of the image tool and nothing else: through the `use_tool` dispatcher, or by its
    own name should the CLI list it directly. The index itself is image_mcp.py's to check."""
    name, given = block.get("name"), block.get("input")
    if name == "use_tool" and isinstance(given, dict) and set(given) == {"tool_name", "tool_input"}:
        name, given = given["tool_name"], given["tool_input"]
    return name == IMAGE_TOOL and isinstance(given, dict) and set(given) <= {"index"}


def verify_stream(lines: list[str], mode: str = "lookup", images: int = 0) -> dict | str:
    """The answer, but only from a session that had no client tools, used nothing except
    server-side X search, and emitted only event shapes we know. Raises Unsafe or LookupFailed.
    A chat turn with `images` may also have had, and called, the image tool — that one only.

    The tool check keys on the tool *name*, which the CLI fills in; `input` is written by the
    model and is only checked in addition."""
    toolset = {"use_tool", IMAGE_TOOL} if images else set()
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
            tools = event.get("tools")
            if not isinstance(tools, list) or not set(map(str, tools)) <= toolset:
                raise Unsafe(f"session started with client tools: {tools!r}")
        elif kind in _BLOCKS:
            for block in (event.get("message") or {}).get("content") or []:
                block_type = block.get("type") if isinstance(block, dict) else None
                if block_type not in _BLOCKS[kind]:
                    raise Unsafe(f"unknown {kind} block {block_type!r}")
                if block_type == "tool_use" and not (images and _image_call(block)):
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
        # The subtype is the CLI's; the result text may be the model's and is not looked at.
        raise classify_failure(f"grok did not finish: {(result or {}).get('subtype')}")
    if mode == "chat":
        text = str(result.get("result") or "").strip()
        if not text:
            raise LookupFailed("grok returned no text")
        return text
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
        "author_handle": str(post.get("author_handle", "")).lstrip(AT_SIGNS)[:15],
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
        handle = str(request["handle"]).lstrip(AT_SIGNS)
        same = str(answer.get("handle", handle)).lstrip(AT_SIGNS).lower() == handle.lower()
        exists = answer.get("exists") is True and same
        return {
            "exists": exists,
            "handle": handle,
            "name": str(answer.get("name", ""))[:100] if exists else "",
            "description": str(answer.get("description", ""))[:500] if exists else "",
        }
    handle = str(request["handle"]).lstrip(AT_SIGNS).lower()
    since = int(request.get("since_id") or 0)
    posts = []
    for raw in answer.get("posts") or []:
        post = _clean_post(raw)
        if post and post["author_handle"].lower() == handle and int(post["id"]) > since:
            posts.append(post)
    posts.sort(key=lambda p: int(p["id"]), reverse=True)
    return {"posts": posts[: int(request.get("limit", 10))]}


class _SessionGate:
    """How many Grok sessions run at once, and when one must run alone.

    Sessions start from the same copy of the login. If one refreshes its token while another
    still holds the old refresh token, that one may present a used refresh token — which an
    OAuth server may answer by revoking the whole login. So a session that might refresh (the
    token is within REFRESH_MARGIN_SECONDS of expiry) waits until it is the only one, runs
    alone, and its refreshed login is written back before anyone else starts.

    It also owns STRAY_PATHS: Grok writes /tmp/sessions whatever its environment says, and that
    directory is shared, so it is removed only when the last running session ends."""

    def __init__(self, limit: int) -> None:
        self._cond = threading.Condition()
        self._limit = limit
        self._active = 0
        self._exclusive = False

    def enter(self, exclusive: bool, timeout: float) -> bool:
        deadline = time.monotonic() + timeout
        with self._cond:
            while self._exclusive or self._active >= self._limit or (exclusive and self._active):
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    return False
                self._cond.wait(remaining)
            self._active += 1
            self._exclusive = exclusive
            return True

    def leave(self) -> None:
        with self._cond:
            self._active -= 1
            self._exclusive = False
            if self._active == 0:
                for path in STRAY_PATHS:
                    shutil.rmtree(path, ignore_errors=True)
            self._cond.notify_all()


_GATE = _SessionGate(CHAT_PARALLEL + 1)


def _login_entry() -> dict:
    with _AUTH_LOCK:
        with open(os.path.join(AUTH_DIR, "auth.json"), encoding="utf-8") as handle:
            data = json.load(handle)
    entries = [v for v in data.values() if isinstance(v, dict)] if isinstance(data, dict) else []
    return entries[0] if len(entries) == 1 else {}


def login_expires_in() -> float:
    """Seconds until the access token expires; 0 when that cannot be read (treat as due)."""
    try:
        raw = str(_login_entry().get("expires_at", ""))
        head, _, tail = raw.rstrip("Z").partition(".")
        moment = datetime.fromisoformat(head + (f".{tail[:6]}" if tail else "") + "+00:00")
    except (OSError, ValueError):
        return 0.0
    return moment.timestamp() - time.time()


_ORIGINAL_LOGIN = "original-auth.json"  # in the scratch, outside the session's home


def _fresh_home() -> tuple[str, dict]:
    """A throwaway Grok home and working directory holding only a copy of the login."""
    scratch = tempfile.mkdtemp(prefix="xsearch-", dir=SCRATCH)
    home, work = os.path.join(scratch, "home"), os.path.join(scratch, "work")
    os.makedirs(os.path.join(home, ".grok"), mode=0o700)
    os.makedirs(work, mode=0o700)
    os.makedirs(os.path.join(scratch, "tmp"), mode=0o700)
    os.makedirs(os.path.join(scratch, "in"), mode=0o700)  # the prompt: outside cwd, never config
    source = os.path.join(AUTH_DIR, "auth.json")
    with _AUTH_LOCK:
        if os.path.exists(source):
            shutil.copyfile(source, os.path.join(home, ".grok", "auth.json"))
            os.chmod(os.path.join(home, ".grok", "auth.json"), 0o600)
            shutil.copyfile(source, os.path.join(scratch, _ORIGINAL_LOGIN))
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
    """Carry a refreshed login back to the volume — the only thing that outlives a session.

    Compare-and-swap: only if this session changed its copy, and the stored login is still the
    one this session started from. A session that refreshed nothing must not overwrite one that
    another session just refreshed (that would bring back a used refresh token). And only a JSON
    object with the same keys: a refresh changes values, never the shape."""
    fresh = os.path.join(scratch, "home", ".grok", "auth.json")
    original = os.path.join(scratch, _ORIGINAL_LOGIN)
    target = os.path.join(AUTH_DIR, "auth.json")
    with _AUTH_LOCK:
        try:
            with open(fresh, "rb") as handle:
                data = handle.read()
            with open(original, "rb") as handle:
                started_from = handle.read()
            with open(target, "rb") as handle:
                current = handle.read()
            if data == started_from:
                return  # this session did not refresh
            if current != started_from:
                print("login changed under this session; its refresh is not kept", flush=True)
                return
            new, old = json.loads(data), json.loads(current)
            if not isinstance(new, dict) or not isinstance(old, dict):
                return
            if set(new) != set(old):
                print("login file changed shape; not kept", flush=True)
                return
        except (OSError, ValueError):
            return
        staging = f"{target}.new"
        with open(os.open(staging, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600), "wb") as out:
            out.write(data)
        os.replace(staging, target)


def defuse_mentions(text: str) -> str:
    """Text the CLI's parser cannot act on, whatever it holds. Every `@` becomes a fullwidth `＠`
    (the model reads it the same; the mention parser only knows ASCII `@`, and a blocklist of
    what may follow it already missed whitespace). A word joiner goes first: even with
    --verbatim a slash command at the very start runs (`/always-approve`, `/compact` did)."""
    return WORD_JOINER + text.replace("@", FULLWIDTH_AT)


def _strings(value: object):
    """Every string inside a JSON value."""
    if isinstance(value, dict):
        for item in value.values():
            yield from _strings(item)
    elif isinstance(value, list):
        for item in value:
            yield from _strings(item)
    elif isinstance(value, str):
        yield value


def _login_secrets(*paths: str) -> set[str]:
    """Every long string in these login files: the tokens, whatever the file calls them."""
    found: set[str] = set()
    for path in paths:
        try:
            with open(path, encoding="utf-8") as handle:
                data = json.load(handle)
        except (OSError, ValueError):
            continue
        found.update(s for s in _strings(data) if len(s) >= MIN_SECRET_CHARS)
    return found


def refuse_login_echo(lines: list[str], scratch: str) -> None:
    """Raise Unsafe if the session's stream (answer or reasoning) carries the login: whatever
    got it into the context, it must not reach a Discord channel. Compared against the raw
    lines and against the strings they decode to, with whitespace dropped as well."""
    secrets = _login_secrets(
        os.path.join(scratch, _ORIGINAL_LOGIN), os.path.join(scratch, "home", ".grok", "auth.json")
    )
    if not secrets:
        return
    decoded = []
    for line in lines:
        try:
            decoded.extend(_strings(json.loads(line)))
        except ValueError:
            continue
    texts = ["\n".join(lines), "\n".join(decoded)]
    texts += [re.sub(r"\s+", "", text) for text in texts]
    if any(secret in text for secret in secrets for text in texts):
        raise Unsafe("the session's output contains the login")


def run_grok(
    prompt: str | None,
    *,
    model: str = MODEL,
    effort: str = "",
    system: str = "",
    timeout: int = TIMEOUT,
    collect: str = "",
    images: list[tuple[str, bytes]] | tuple = (),
) -> list[str]:
    """One locked-down Grok session in a fresh home; its stream as lines. `prompt=None` runs
    `grok models` instead of a turn; `collect` names a file in the session's Grok home whose
    content is appended as the last line (the model catalog that command writes). `images`
    (suffix, bytes) are what the image tool hands the model, the only tool it then has."""
    scratch, home_env = _fresh_home()
    try:
        if prompt is None:
            lines = _run_command([GROK, "models"], home_env, os.path.join(scratch, "work"), 60)
            if collect:
                with open(os.path.join(home_env["GROK_HOME"], collect), encoding="utf-8") as f:
                    lines.append(f.read())
            return lines
        prompt_file = os.path.join(scratch, "in", "prompt.txt")
        with open(prompt_file, "w", encoding="utf-8") as handle:
            handle.write(defuse_mentions(prompt))
        tail = ["--prompt-file", prompt_file]
        if effort:
            tail += ["--reasoning-effort", effort]
        if system:  # `=` form: a value can never be read as another flag
            tail += [f"--system-prompt-override={defuse_mentions(system)}"]
        if images:
            try:
                _offer_images(images, scratch, home_env)
            except OSError as error:  # the tmpfs full (ENOSPC): a failed turn, not a dropped line
                raise LookupFailed(f"could not stage the images: {type(error).__name__}") from None
            lines = _run(model, tail, home_env, os.path.join(scratch, "work"), timeout, IMAGE_ARGS)
        else:
            lines = _run(model, tail, home_env, os.path.join(scratch, "work"), timeout)
        refuse_login_echo(lines, scratch)
        if collect:
            with open(os.path.join(home_env["GROK_HOME"], collect), encoding="utf-8") as handle:
                lines.append(handle.read())
        return lines
    finally:
        _keep_login(scratch)
        shutil.rmtree(scratch, ignore_errors=True)


def _offer_images(images, scratch: str, home_env: dict) -> None:
    """Write the images where image_mcp.py serves them from (outside cwd, read-only files) and give
    this session's fresh home the one MCP server the enforced policy allows."""
    folder = os.path.join(scratch, "in", "images")
    os.makedirs(folder, mode=0o700)
    for number, (suffix, data) in enumerate(images, 1):
        with open(os.open(os.path.join(folder, f"{number}{suffix}"),
                          os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o400), "wb") as out:  # fmt: skip
            out.write(data)
    command, *args = IMAGE_SERVER
    with open(os.path.join(home_env["GROK_HOME"], "config.toml"), "w", encoding="utf-8") as cfg:
        # JSON strings are valid TOML basic strings
        cfg.write(f"[mcp_servers.images]\ncommand = {json.dumps(command)}\n"
                  f"args = {json.dumps(args)}\n"
                  f"env = {{ IMAGE_DIR = {json.dumps(folder)}, "
                  f"IMAGE_COUNT = {json.dumps(str(len(images)))} }}\n")  # fmt: skip


def _run(
    model: str, tail: list[str], home_env: dict, cwd: str, timeout: int, locked=LOCKED_ARGS
) -> list[str]:
    command = [
        GROK, "-m", model, "--cwd", cwd, *tail, *locked,
        *(("--sandbox", SANDBOX) if SANDBOX else ()),
    ]  # fmt: skip
    return _run_command(command, home_env, cwd, timeout)


def _run_command(command: list[str], home_env: dict, cwd: str, timeout: int) -> list[str]:
    try:
        process = subprocess.Popen(
            command,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            cwd=cwd,
            env={"PATH": os.environ.get("PATH", "/usr/local/bin:/usr/bin:/bin"), **home_env, **ENV},
            start_new_session=True,
        )
    except (OSError, ValueError) as error:  # an argument too long for execve, or with a NUL
        raise LookupFailed(f"could not start grok: {type(error).__name__}") from None
    try:
        stdout, stderr = process.communicate(timeout=timeout)
    except subprocess.TimeoutExpired:
        os.killpg(process.pid, signal.SIGKILL)
        process.communicate()
        raise LookupFailed("grok timed out") from None
    if process.returncode != 0:
        raise classify_failure(f"grok exited {process.returncode}", stderr.strip()[-400:])
    return stdout.splitlines()


def _session(slot: threading.Semaphore | threading.Lock, still_wanted, **kwargs) -> list[str]:
    """Wait for a slot of this kind and for the gate, then run one session."""
    started = time.monotonic()
    if not slot.acquire(timeout=QUEUE_WAIT):
        raise Busy("no free Grok session")
    try:
        alone = login_expires_in() < REFRESH_MARGIN_SECONDS
        if not _GATE.enter(alone, max(0.0, QUEUE_WAIT - (time.monotonic() - started))):
            raise Busy("no free Grok session")
        try:
            if not still_wanted():  # the caller gave up while queued: spend nothing on it
                raise Gone("client went away")
            return run_grok(**kwargs)
        finally:
            _GATE.leave()
    finally:
        slot.release()


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
    lines = _session(_LOCK, still_wanted, prompt=prompt)
    answer = shape(kind, request, verify_stream(lines))
    if kind in ("post", "user"):  # found or not: repeats of the same question cost nothing
        _POST_CACHE[key] = (time.monotonic(), answer)
        while len(_POST_CACHE) > 256:
            _POST_CACHE.popitem(last=False)
    return answer


# ------------------------------------------------------------------------------------ models

_MODELS: tuple[float, list[dict]] = (0.0, [])
_MODELS_LOCK = threading.Lock()
MODELS_RETRY_SECONDS = 600  # after a failed refresh, keep the old list this long before retrying


def parse_models(cache: dict) -> list[dict]:
    """The catalog Grok wrote to models_cache.json: visible models with their effort menus.
    The file spells fields in snake_case (`reasoning_efforts`); the same catalog over ACP uses
    camelCase — both are read."""
    out = []
    for model_id, entry in (cache.get("models") or {}).items():
        info = (entry or {}).get("info") or {}
        meta = {**info, **(info.get("_meta") or {})}
        if meta.get("hidden") or not MODEL_ID.fullmatch(str(model_id)):
            continue
        menu = [
            e
            for e in meta.get("reasoning_efforts") or meta.get("reasoningEfforts") or []
            if isinstance(e, dict) and EFFORT.fullmatch(str(e.get("id")))
        ]
        efforts = [str(e["id"]) for e in menu]
        default = next(
            (str(e["id"]) for e in menu if e.get("default")),
            str(meta.get("reasoning_effort") or meta.get("reasoningEffort") or ""),
        )
        out.append(
            {
                "id": str(model_id),
                "name": str(meta.get("name") or model_id)[:60],
                "efforts": efforts,
                "default_effort": default if default in efforts else "",
            }
        )
    return out


def _refresh_models(still_wanted=lambda: True) -> list[dict]:
    """Run `grok models` (a session like any other, in a fresh home) and keep the catalog it
    writes. On failure the previous list stays, and the next attempt waits MODELS_RETRY_SECONDS:
    a broken `grok models` must not cost every chat turn a retry."""
    global _MODELS
    fetched_at, cached = _MODELS
    try:
        lines = _session(_LOCK, still_wanted, prompt=None, collect="models_cache.json")
        catalog = parse_models(json.loads(lines[-1]))
        if not catalog:
            raise LookupFailed("the model catalog is empty")
    except Exception as error:  # whatever broke (a missing cache file too): back off the same
        print(f"model catalog refresh failed ({error}); keeping {len(cached)} models", flush=True)
        if not cached:
            raise LookupFailed("could not read the model catalog") from None
        retry_at = time.monotonic() - MODELS_CACHE_SECONDS + MODELS_RETRY_SECONDS
        _MODELS = (retry_at, cached)
        return cached
    _MODELS = (time.monotonic(), catalog)
    return catalog


def models(still_wanted=lambda: True) -> list[dict]:
    """The models this login may use. Fetched synchronously only the first time; after that a
    stale list is served as is while one background thread refreshes it, so neither a chat turn
    nor /models ever waits behind a lookup (or a broken CLI) for the catalog."""
    fetched_at, cached = _MODELS
    if not cached:
        with _MODELS_LOCK:
            return _MODELS[1] or _refresh_models(still_wanted)
    if time.monotonic() - fetched_at >= MODELS_CACHE_SECONDS and _MODELS_LOCK.acquire(
        blocking=False
    ):

        def refresh() -> None:
            try:
                _refresh_models()
            finally:
                _MODELS_LOCK.release()

        threading.Thread(target=refresh, daemon=True).start()
    return cached


def _catalog_entry(model_id: str) -> dict | None:
    return next((m for m in models() if m["id"] == model_id), None)


# -------------------------------------------------------------------------------------- chat


def decode_images(given: object) -> list[tuple[str, bytes]]:
    """A chat request's images (base64 strings) as (suffix, bytes); PNG or JPEG by content."""
    if not isinstance(given, list) or len(given) > MAX_IMAGES:
        raise BadRequest(f"images must be a list of at most {MAX_IMAGES}")
    images, total = [], 0
    for item in given:
        if type(item) is not str or len(item) > (MAX_IMAGE_BYTES + 2) // 3 * 4:
            raise BadRequest(f"each image must be base64 of at most {MAX_IMAGE_BYTES} bytes")
        try:
            data = base64.b64decode(item, validate=True)
        except ValueError:
            raise BadRequest("an image is not valid base64") from None
        suffix = next((s for magic, s in IMAGE_TYPES.items() if data.startswith(magic)), None)
        if suffix is None:
            raise BadRequest("images must be PNG or JPEG")
        total += len(data)
        if total > MAX_IMAGES_BYTES:
            raise BadRequest(f"images may total at most {MAX_IMAGES_BYTES} bytes")
        images.append((suffix, data))
    return images


def chat(request: dict, still_wanted=lambda: True) -> dict:
    """One member turn: the Bot's whole prompt in, Grok's answer out — from a session that had
    no client tools (server-side X search is the only thing it may use), or, when the turn
    carries images, the image tool alone."""
    prompt, system = request.get("prompt"), request.get("system", "")
    model, effort = request.get("model"), request.get("effort", "")
    if type(prompt) is not str or not prompt.strip() or len(prompt) > MAX_PROMPT_CHARS:
        raise BadRequest("prompt must be a non-empty string")
    if type(system) is not str or len(system.encode("utf-8")) > MAX_SYSTEM_BYTES or "\0" in system:
        raise BadRequest(f"system must be a string of at most {MAX_SYSTEM_BYTES} bytes")
    if type(model) is not str or not MODEL_ID.fullmatch(model):
        raise BadRequest("model must be a model id")
    if type(effort) is not str or (effort and not EFFORT.fullmatch(effort)):
        raise BadRequest("effort must be an effort level")
    images = decode_images(request.pop("images", []))  # popped: the base64 is not kept alongside
    if images:
        system = f"{system}\n\n{IMAGE_NOTE.format(count=len(images))}".strip()
    entry = _catalog_entry(model)
    if entry is None:
        raise BadRequest(f"unknown model {model!r}")
    if effort and effort not in entry["efforts"]:
        raise BadRequest(f"{model} does not take effort {effort!r}")
    lines = _session(
        _CHAT_SLOTS,
        still_wanted,
        prompt=prompt,
        model=model,
        effort=effort,
        system=system,
        timeout=CHAT_TIMEOUT,
        **({"images": images} if images else {}),
    )
    # The model only ever saw `＠`; code it writes back (`＠dataclass`) must still run. The answer
    # never returns to the CLI as is: a replayed transcript is defused again.
    text = verify_stream(lines, mode="chat", images=len(images)).replace(FULLWIDTH_AT, "@")
    return {"text": text, "model": model, "effort": effort}


# ------------------------------------------------------------------------------------- usage

_USAGE: dict = {}
_USAGE_LOCK = threading.Lock()


def parse_billing(body: object, cache_status: str) -> dict | None:
    """The plan's quota window from the CLI's billing endpoint (not a public API: its shape may
    change without notice, so anything unexpected reads as unknown, never as zero)."""
    config = body.get("config") if isinstance(body, dict) else None
    if not isinstance(config, dict):
        return None
    period = config.get("currentPeriod") if isinstance(config.get("currentPeriod"), dict) else {}
    percent = config.get("creditUsagePercent")
    if type(percent) not in (int, float) or "WEEKLY" not in str(period.get("type") or ""):
        return None
    reset = config.get("billingPeriodEnd") or period.get("end")
    return {
        "weekly_percent": float(percent),
        "reset_at": reset if isinstance(reset, str) else "",
        "live": cache_status.strip().upper() in LIVE_CACHE_STATUS if cache_status else True,
    }


def usage() -> dict:
    """Weekly plan usage, cached USAGE_CACHE_SECONDS. A reading the CDN served from cache does
    not replace a live one."""
    global _USAGE
    with _USAGE_LOCK:
        if _USAGE and time.monotonic() - _USAGE["_at"] < USAGE_CACHE_SECONDS:
            return {k: v for k, v in _USAGE.items() if not k.startswith("_")}
        token = str(_login_entry().get("key") or "")
        if not token:
            raise LoginFailed("no login")
        req = urllib.request.Request(
            BILLING_URL, headers={"Accept": "application/json", "Cache-Control": "no-cache"}
        )
        req.add_unredirected_header("Authorization", f"Bearer {token}")  # never to a redirect
        try:
            with urllib.request.urlopen(req, timeout=8) as response:
                body = json.loads(response.read(65536).decode())
                status = response.headers.get("cf-cache-status", "")
        except urllib.error.HTTPError as error:
            raise classify_failure(f"billing HTTP {error.code}", str(error.code)) from None
        except (OSError, ValueError) as error:
            raise LookupFailed(f"billing unreadable: {type(error).__name__}") from None
        reading = parse_billing(body, status)
        if reading is None:
            raise LookupFailed("billing shape not understood")
        previous_live = _USAGE.get("live") and time.monotonic() - _USAGE["_at"] < 3600
        if not reading["live"] and previous_live:
            # A CDN copy does not replace a recent live reading — unless it shows *more* usage:
            # the reserve errs towards protecting the quota, not towards spending it.
            if reading["weekly_percent"] <= _USAGE["weekly_percent"]:
                reading = {k: v for k, v in _USAGE.items() if not k.startswith("_")}
        _USAGE = {**reading, "_at": time.monotonic()}
        return reading


MAX_CONNECTIONS = 16
_CONNECTIONS = threading.BoundedSemaphore(MAX_CONNECTIONS)
# Chat bodies carrying images are read (and held, decoded, until the turn ends) only this many at a
# time — the sessions plus one waiting — not one per open connection. Past that: busy, at once.
_IMAGE_BODIES = threading.BoundedSemaphore(CHAT_PARALLEL + 1)


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
        if self.path == "/health":
            return self._json(200, {"ok": True})
        if self.path not in ("/models", "/usage"):
            return self._json(404, {"error": "not found"})
        self._answer(lambda: {"models": models(self._client_waiting)} if self.path == "/models"
                     else usage())  # fmt: skip

    def do_POST(self) -> None:
        if self.path == "/chat":
            kind, limit = "chat", MAX_CHAT_BODY
        else:
            kind, limit = self.path.removeprefix("/x/"), MAX_BODY
            if kind not in SCHEMAS:
                return self._json(404, {"error": "not found"})
        raw_length = self.headers.get("Content-Length", "")
        if not raw_length.isdigit():
            return self._json(411, {"error": "Content-Length required"})
        length = int(raw_length)
        if length > limit:
            return self._json(413, {"error": "request too large"})
        large = kind == "chat" and length > 1024 * 1024  # text alone stays under 1 MiB
        if large and not _IMAGE_BODIES.acquire(blocking=False):
            # The body is not read. A client still uploading may see the connection drop rather
            # than this 429 — the Bot logs that as "unreachable", and falls back all the same.
            self.close_connection = True
            return self._json(429, {"error": "busy"})
        try:
            try:
                request = json.loads(self.rfile.read(length) or b"{}")
            except (json.JSONDecodeError, UnicodeDecodeError):
                return self._json(400, {"error": "body must be JSON"})
            if not isinstance(request, dict):
                return self._json(400, {"error": "body must be a JSON object"})
            if kind == "chat":
                return self._answer(lambda: chat(request, self._client_waiting))
            return self._answer(lambda: lookup(kind, request, self._client_waiting))
        finally:
            if large:
                _IMAGE_BODIES.release()

    def _answer(self, work) -> None:
        """Run `work` and map what happened onto a status the Bot can act on: 400 its own
        mistake (do not retry elsewhere), 402 plan quota spent, 401 login gone, 429 busy, 502
        failed, 503 refused (the session was not the locked-down one; never use its answer)."""
        try:
            return self._json(200, work())
        except BadRequest as error:
            return self._json(400, {"error": str(error)})
        except Busy:
            return self._json(429, {"error": "busy"})
        except Gone:
            return None
        except Unsafe as error:
            print(f"REFUSED unsafe grok session: {error}", flush=True)
            return self._json(503, {"error": "refused"})
        except QuotaExhausted as error:
            print(f"quota: {error}", flush=True)
            return self._json(402, {"error": "quota"})
        except LoginFailed as error:
            print(f"login: {error}", flush=True)
            return self._json(401, {"error": "login"})
        except LookupFailed as error:
            print(f"failed: {error}", flush=True)
            return self._json(502, {"error": "failed"})

    def log_message(self, fmt: str, *args) -> None:
        print(f"{self.address_string()} {fmt % args}", flush=True)


if __name__ == "__main__":
    ThreadingHTTPServer(("0.0.0.0", PORT), Handler).serve_forever()
