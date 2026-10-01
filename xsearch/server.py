"""The X lookup sidecar: three fixed questions about X, answered by Grok Build through xAI's
server-side X search on the operator's Grok subscription. stdlib only.

Grok Build is a coding agent. Out of the box it can run shell commands, read and write files,
fetch URLs and start background jobs, and the obvious flags do not all remove them (a
`--disallowed-tools` entry spelled as the tool list spells it leaves the shell in place, and a
`monitor` tool runs commands too). Nothing a member writes reaches the prompt here — the bot
sends a post id or a handle, both validated below — but the posts Grok reads are untrusted
text, so the agent must have nothing to act with. Three layers, any one of which suffices:

1. Every client tool is removed on the command line (`LOCKED_ARGS`), and permission rules
   deny the rest.
2. A root-owned enforced policy (/etc/grok/requirements.toml) denies every PreToolUse.
3. Fail-closed verification of what actually happened (`verify_stream`): the session must
   start with an empty client toolset and may only use the server-side X search. Anything
   else and the answer is thrown away. Layers 1 and 2 are Grok's own machinery (its hooks
   fail open on error); this one is ours and does not.
"""

from __future__ import annotations

import json
import os
import re
import signal
import subprocess
import threading
from datetime import UTC, datetime
from email.utils import parsedate_to_datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

PORT = int(os.environ.get("XSEARCH_PORT", "8090"))
MODEL = os.environ.get("XSEARCH_MODEL", "grok-4.7")
TIMEOUT = int(os.environ.get("XSEARCH_TIMEOUT_SECONDS", "180"))
GROK = os.environ.get("XSEARCH_GROK_BIN", "grok")
CWD = os.environ.get("XSEARCH_CWD", "/work")
MAX_BODY = 4096
MAX_TEXT = 4000
_LOCK = threading.Lock()  # one Grok session at a time: each one spends subscription quota

POST_ID = re.compile(r"^[0-9]{1,20}$")
HANDLE = re.compile(r"^[A-Za-z0-9_]{1,15}$")

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


class BadRequest(ValueError):
    pass


class LookupFailed(RuntimeError):
    pass


class Unsafe(RuntimeError):
    """The session was not the locked-down one this service relies on."""


def build_prompt(kind: str, request: dict) -> str:
    """The whole prompt, from validated fields only."""
    if kind == "post":
        post_id = str(request.get("id", ""))
        if not POST_ID.match(post_id):
            raise BadRequest("id must be a numeric X post id")
        return (
            f"Use X search to look up the X post whose status id is {post_id}. If it exists, "
            "return it with its author, UTC time, full text, whether it is a reply or a repost, "
            "the text of any quoted post, engagement counts and media types. If it cannot be "
            "found, return found=false. Report only what X search returned."
        )
    handle = str(request.get("handle", "")).lstrip("@")
    if not HANDLE.match(handle):
        raise BadRequest("handle must be an X username")
    if kind == "user":
        return (
            f"Use X search to check whether the X account @{handle} exists. Return its handle, "
            "display name and bio, or exists=false. Report only what X search returned."
        )
    if kind == "recent":
        since = str(request.get("since_id", "") or "")
        if since and not POST_ID.match(since):
            raise BadRequest("since_id must be a numeric X post id")
        limit = request.get("limit", 10)
        if not isinstance(limit, int) or not 1 <= limit <= 20:
            raise BadRequest("limit must be 1-20")
        newer = f" with a status id greater than {since}" if since else ""
        return (
            f"Use X search to list up to {limit} of the most recent posts authored by "
            f"@{handle}{newer}, newest first. Include the account's replies and reposts but "
            "mark them. For each post give its numeric status id, UTC time, full text and "
            "the fields of the schema. Report only what X search returned."
        )
    raise BadRequest(f"unknown lookup {kind!r}")


def verify_stream(lines: list[str]) -> dict:
    """The structured answer, but only from a session that had no client tools and used
    nothing except server-side X search. Raises Unsafe or LookupFailed otherwise."""
    init_tools = None
    result = None
    for line in lines:
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            continue
        kind = event.get("type")
        if kind == "system" and event.get("subtype") == "init":
            init_tools = event.get("tools")
        elif kind == "assistant":
            for block in (event.get("message") or {}).get("content") or []:
                if block.get("type") in ("tool_use", "server_tool_use"):
                    tool_input = block.get("input") or {}
                    if (
                        tool_input.get("variant") != "XSearch"
                        or tool_input.get("backend") is not True
                    ):
                        raise Unsafe(
                            f"session used a tool other than X search: {block.get('name')!r}"
                        )
        elif kind == "result":
            result = event
    if init_tools is None:
        raise Unsafe("session reported no toolset")
    if init_tools:
        raise Unsafe(f"session started with client tools: {init_tools}")
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


def _clean_post(post: object) -> dict | None:
    if not isinstance(post, dict):
        return None
    post_id = str(post.get("id", ""))
    created = _utc_iso(str(post.get("created_at", "")))
    if created is None or not POST_ID.match(post_id):
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
        post = _clean_post(answer.get("post")) if answer.get("found") else None
        if post and post["id"] != str(request["id"]):
            post = None
        return {"found": post is not None, "post": post}
    if kind == "user":
        handle = str(request["handle"]).lstrip("@")
        same = str(answer.get("handle", handle)).lstrip("@").lower() == handle.lower()
        exists = bool(answer.get("exists")) and same
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


def run_grok(prompt: str) -> list[str]:
    command = [
        GROK, "-m", MODEL, "-p", prompt, "--cwd", CWD, *LOCKED_ARGS,
        *(("--sandbox", SANDBOX) if SANDBOX else ()),
    ]  # fmt: skip
    process = subprocess.Popen(
        command,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        env={**os.environ, **ENV},
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


def lookup(kind: str, request: dict) -> dict:
    prompt = (
        f"{build_prompt(kind, request)}\n\nSearch first. Then reply with exactly one JSON object "
        f"and nothing else, matching this JSON Schema: {json.dumps(SCHEMAS[kind])}"
    )
    with _LOCK:
        lines = run_grok(prompt)
    return shape(kind, request, verify_stream(lines))


class Handler(BaseHTTPRequestHandler):
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
        length = int(self.headers.get("Content-Length") or 0)
        if length > MAX_BODY:
            return self._json(413, {"error": "request too large"})
        try:
            request = json.loads(self.rfile.read(length) or b"{}")
            if not isinstance(request, dict):
                raise BadRequest("body must be a JSON object")
            return self._json(200, lookup(kind, request))
        except (json.JSONDecodeError, BadRequest) as error:
            return self._json(400, {"error": str(error)})
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
