"""The one client tool a Grok chat session may have: the images the member attached to this turn.

A stdio MCP server (newline-delimited JSON-RPC) with a single tool, `get_image(index)`. Grok
Build can only take an image inline on its command line, where Linux caps one argument at
128 KiB; through a tool result it takes any size. server.py writes the turn's images into a
directory of the session's scratch as 1.png, 2.jpg, ... and starts this with IMAGE_DIR and
IMAGE_COUNT; the model asks for a number, never a path, and nothing else on disk is reachable.
stdlib only, root-owned, pinned by command in /etc/grok/requirements.toml.
"""

from __future__ import annotations

import base64
import json
import os
import sys

DIR = os.environ.get("IMAGE_DIR", "")
try:
    COUNT = int(os.environ.get("IMAGE_COUNT", "0"))
except ValueError:
    COUNT = 0
TYPES = {".png": "image/png", ".jpg": "image/jpeg"}
TOOL = {
    "name": "get_image",
    "description": (
        f"Return image number `index` (1 to {COUNT}) of the images the member attached to this "
        "message. Call it once per image before answering."
    ),
    "inputSchema": {
        "type": "object",
        "properties": {"index": {"type": "integer", "minimum": 1, "maximum": max(COUNT, 1)}},
        "required": ["index"],
        "additionalProperties": False,
    },
}


def _image(index: object) -> dict:
    if type(index) is not int or not 1 <= index <= COUNT:
        return {"content": [{"type": "text", "text": f"no image {index!r}"}], "isError": True}
    for suffix, mime in TYPES.items():
        try:
            fd = os.open(os.path.join(DIR, f"{index}{suffix}"), os.O_RDONLY | os.O_NOFOLLOW)
        except FileNotFoundError:
            continue
        except OSError:  # a symlink (O_NOFOLLOW), or anything else that is not our file
            return {"content": [{"type": "text", "text": f"image {index} unreadable"}],
                    "isError": True}  # fmt: skip
        with os.fdopen(fd, "rb") as handle:
            data = base64.b64encode(handle.read()).decode("ascii")
        return {"content": [{"type": "image", "data": data, "mimeType": mime}]}
    return {"content": [{"type": "text", "text": f"image {index} is missing"}], "isError": True}


def handle(request: dict) -> dict | None:
    """The response to one JSON-RPC message, or None for a notification."""
    method, request_id = request.get("method"), request.get("id")
    if request_id is None:
        return None
    if method == "initialize":
        params = request.get("params") if isinstance(request.get("params"), dict) else {}
        result = {
            "protocolVersion": str(params.get("protocolVersion") or "2025-06-18"),
            "capabilities": {"tools": {}},
            "serverInfo": {"name": "images", "version": "1"},
        }
    elif method == "ping":
        result = {}
    elif method == "tools/list":
        result = {"tools": [TOOL]}
    elif method == "tools/call":
        params = request.get("params") if isinstance(request.get("params"), dict) else {}
        arguments = params.get("arguments") if isinstance(params.get("arguments"), dict) else {}
        if params.get("name") != TOOL["name"]:
            result = {"content": [{"type": "text", "text": "unknown tool"}], "isError": True}
        else:
            result = _image(arguments.get("index"))
    else:
        return {"jsonrpc": "2.0", "id": request_id,
                "error": {"code": -32601, "message": "method not found"}}  # fmt: skip
    return {"jsonrpc": "2.0", "id": request_id, "result": result}


def main() -> None:
    for line in sys.stdin:
        try:
            request = json.loads(line)
        except ValueError:
            continue
        response = handle(request) if isinstance(request, dict) else None
        if response is not None:
            sys.stdout.write(json.dumps(response) + "\n")
            sys.stdout.flush()


if __name__ == "__main__":
    main()
