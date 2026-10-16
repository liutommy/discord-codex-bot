"""Operator commands for the running Bot, with no restart:

    python -m discord_codex_bot.admin <group> <action> [options] [--json] [--timeout S]
    python -m discord_codex_bot.admin result <id>
    python -m discord_codex_bot.admin pending

Run it inside the bot container (`docker exec <container> python -m discord_codex_bot.admin …`).
Reminders, threads, routing marks, quotas and in-flight requests live in the Bot's memory and are
saved by full overwrite, so a write from another process would be ignored and then lost. This
CLI therefore does nothing itself: it drops a request into an inbox, the Bot runs it inside its
own process through the same code paths as the slash commands (admin_server.py), and the CLI
reads the result back.

The inbox is CODEX_HOME/admin with inbox/, processing/, done/ and rejected/. The Bot creates them
with mode 0700 at start (and chmods them back to 0700 if they exist), so only their owner — the
container user, uid 1000, the same uid as the host user that owns the bind mount — and root can
write there. The models cannot: Codex runs read-only and agy denies writes. The CLI refuses to
run when the directory is not owned by its own uid or is writable by group or others, and the
Bot skips a pass in that state. The directory is left out of the nightly backup on purpose: a
restored inbox must not run old requests again.

A request is `inbox/<id>.json`, written as `<id>.json.tmp`, fsynced, then renamed into place:
{"v": 1, "id", "command": "<group>.<action>", "args": {...}, "created": <unix>, "by": <user>}.
`id` is 32 lowercase hex digits and must equal the file name. The Bot claims a file by renaming
it into processing/ (so the CLI can still withdraw an unclaimed one), validates it, runs it, and
writes done/<id>.json — or rejected/<id>.json with the reason when it did not run. An id with a
result already is never run again, a request left in processing/ by a restart is moved to
rejected/ and not run, and a request more than a day old is refused, so one pruned from done/
(after 7 days) cannot come back. Exit codes: 0 done, 1 failed or rejected, 2 not finished in
time.
"""

from __future__ import annotations

import argparse
import contextlib
import getpass
import json
import math
import os
import re
import secrets
import stat
import sys
import time
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from .announce import show_time
from .config import Config, load_config

ROOT_NAME = "admin"
INBOX, PROCESSING, DONE, REJECTED = "inbox", "processing", "done", "rejected"
SUBDIRS = (INBOX, PROCESSING, DONE, REJECTED)
VERSION = 1
MAX_REQUEST_BYTES = 64 * 1024
REQUEST_ID = re.compile(r"[0-9a-f]{32}")
MAX_AGE_SECONDS = 24 * 3600  # well inside the 7 days done/ keeps: a pruned id cannot run again
DEFAULT_TIMEOUT = 60.0
# The CLI waits this much longer than the Bot gives the command: the Bot polls every 2 s and
# the request may wait for a free slot.
WAIT_MARGIN = 30.0
POLL_SECONDS = 0.5
MAX_BY_CHARS = 64

# Argument names with a fixed meaning: Discord ids, checked against the guild before anything
# runs (admin_server.check_context).
CONTEXT_ARGS = ("guild", "channel", "user", "who")
MAX_ID = 10**20 - 1  # Discord ids have at most 20 digits
SCALAR_TYPES = ("id", "int", "str", "bool", "choice")
LIST_TYPES = ("list[id]", "list[int]", "list[str]")
SCHEMA_KEYS = {"help", "description", "timeout", "args"}
ARG_KEYS = {
    "type",
    "required",
    "help",
    "metavar",
    "max_len",
    "min_len",
    "min",
    "max",
    "choices",
    "max_items",
    "stdin",
}
DEFAULT_MAX_ITEMS = 10
MAX_TIMEOUT = 900
COMMAND_NAME = re.compile(r"[a-z][a-z_]*(?:\.[a-z][a-z_]*)?")
ARG_NAME = re.compile(r"[a-z][a-z0-9_]*")
LOCAL_COMMANDS = ("result", "pending")
RESERVED_ARGS = {"json", "timeout", "help", "h"}  # options every command already has

SEE_ALSO = """\
其他維運指令（各自獨立，不經過這個收件匣）：
  python -m discord_codex_bot.announce     公告：立即發送（send）、排程（queue）、查看（list）
  python -m discord_codex_bot.emoji        表情符號描述：import / export / status
  python -m discord_codex_bot.backup       立即備份一次
  python -m discord_codex_bot.harvest      把對話整理進記憶 [--force]
  python -m discord_codex_bot.digest       每週記憶摘要 [--force]
  python -m discord_codex_bot.consolidate  記憶整併 [--force]
"""


class Rejected(Exception):
    """The request was refused before anything ran; the reason goes to rejected/ and the log,
    so it never quotes member text."""

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


class AdminError(Exception):
    """The command ran and failed in a way the operator should read (a reminder that is not
    theirs, a store refusing a value); written to done/ with ok=false."""


class UnsafeDirectory(Exception):
    """The admin directory is missing, not ours, or writable by others."""


def admin_root(config: Config) -> Path:
    return config.codex_home / ROOT_NAME


def check_root(root: Path) -> None:
    """UnsafeDirectory unless the root and every subdirectory are real directories owned by
    this process's uid and not writable by group or others."""
    uid = os.getuid()
    for path in (root, *(root / name for name in SUBDIRS)):
        try:
            info = os.lstat(path)
        except FileNotFoundError:
            raise UnsafeDirectory(f"{path} 不存在；Bot 啟動時會建立它（Bot 有在跑嗎？）") from None
        if not stat.S_ISDIR(info.st_mode):
            raise UnsafeDirectory(f"{path} 不是目錄")
        if info.st_uid != uid:
            raise UnsafeDirectory(f"{path} 屬於 uid {info.st_uid}，不是目前的 uid {uid}")
        if info.st_mode & 0o022:
            raise UnsafeDirectory(
                f"{path} 的權限是 {stat.filemode(info.st_mode)}，群組或其他人可寫；"
                "請 chmod 700 後重啟 Bot"
            )


def write_private(path: Path, data: bytes, scratch: Path | None = None) -> None:
    """Mode 0600, fsynced, renamed into place: a reader never sees half a file."""
    scratch = scratch or path.with_name(f".{path.name}.{secrets.token_hex(4)}.tmp")
    fd = os.open(scratch, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(scratch, path)
    except BaseException:
        with contextlib.suppress(OSError):
            scratch.unlink()
        raise


def _who_am_i() -> str:
    try:
        return getpass.getuser()[:MAX_BY_CHARS]
    except (KeyError, OSError):  # a uid with no passwd entry, as in some containers
        return str(os.getuid())


def write_request(
    root: Path, command: str, args: Mapping[str, Any], now: float | None = None
) -> str:
    """Put a request in the inbox; returns its id. ValueError when it is over the size cap."""
    request_id = secrets.token_hex(16)
    body = {
        "v": VERSION,
        "id": request_id,
        "command": command,
        "args": dict(args),
        "created": time.time() if now is None else now,
        "by": _who_am_i(),
    }
    data = json.dumps(body, ensure_ascii=False).encode("utf-8")
    if len(data) > MAX_REQUEST_BYTES:
        raise ValueError(f"請求有 {len(data)} 位元組，超過上限 {MAX_REQUEST_BYTES}")
    inbox = root / INBOX
    # The scratch name does not end in .json: the Bot never picks up a half-written request.
    write_private(inbox / f"{request_id}.json", data, inbox / f"{request_id}.json.tmp")
    return request_id


def read_result(root: Path, request_id: str) -> tuple[str, dict] | None:
    """("done", document) or ("rejected", document), or None while there is neither."""
    for status in (DONE, REJECTED):
        try:
            text = (root / status / f"{request_id}.json").read_text("utf-8")
        except FileNotFoundError:
            continue
        return status, json.loads(text)
    return None


def withdraw(root: Path, request_id: str) -> bool:
    """Remove a request the Bot has not claimed yet; False when it is already claimed."""
    try:
        os.remove(root / INBOX / f"{request_id}.json")
    except FileNotFoundError:
        return False
    return True


def wait_result(
    root: Path, request_id: str, timeout: float, poll: float = POLL_SECONDS
) -> tuple[str, dict | None]:
    """Wait for the result. On timeout the request is withdrawn if the Bot has not claimed it
    ("withdrawn": nothing ran), else ("running", None)."""
    deadline = time.monotonic() + timeout
    while True:
        if found := read_result(root, request_id):
            return found
        if time.monotonic() >= deadline:
            break
        time.sleep(poll)
    if withdraw(root, request_id):
        return "withdrawn", None
    return read_result(root, request_id) or ("running", None)


# --------------------------------------------------------------------------- schemas


def _scalar(kind: str) -> str:
    return kind[5:-1] if kind.startswith("list[") else kind


def check_schema(command: str, schema: Mapping[str, Any]) -> None:
    """ValueError for a schema the server and the CLI could not both follow."""
    if not COMMAND_NAME.fullmatch(command):
        raise ValueError(f"{command}: command names are group.action in lowercase")
    if command.split(".")[0] in LOCAL_COMMANDS:
        raise ValueError(f"{command}: {command.split('.')[0]} is a CLI command")
    if not isinstance(schema, Mapping) or set(schema) - SCHEMA_KEYS:
        raise ValueError(f"{command}: schema keys are {sorted(SCHEMA_KEYS)}")
    if not isinstance(schema.get("help"), str) or not schema["help"]:
        raise ValueError(f"{command}: help is required")
    timeout = schema.get("timeout", DEFAULT_TIMEOUT)
    if isinstance(timeout, bool) or not isinstance(timeout, int | float):
        raise ValueError(f"{command}: timeout must be a number")
    if not 0 < timeout <= MAX_TIMEOUT:
        raise ValueError(f"{command}: timeout must be 1-{MAX_TIMEOUT} seconds")
    arguments = schema.get("args", {})
    if not isinstance(arguments, Mapping):
        raise ValueError(f"{command}: args must be a mapping")
    for name, spec in arguments.items():
        where = f"{command} --{name}"
        if not isinstance(name, str) or not ARG_NAME.fullmatch(name) or name in RESERVED_ARGS:
            raise ValueError(f"{where}: not a usable argument name")
        if not isinstance(spec, Mapping) or set(spec) - ARG_KEYS:
            raise ValueError(f"{where}: argument keys are {sorted(ARG_KEYS)}")
        kind = spec.get("type")
        if kind not in SCALAR_TYPES + LIST_TYPES:
            raise ValueError(f"{where}: type must be one of {SCALAR_TYPES + LIST_TYPES}")
        if name in CONTEXT_ARGS:
            allowed = ("list[id]", "list[int]") if name == "who" else ("id", "int")
            if kind not in allowed:
                raise ValueError(f"{where}: must be {allowed[0]}")
        if name in ("channel", "who") and "guild" not in arguments:
            raise ValueError(f"{where}: needs a guild argument to be checked against")
        if _scalar(kind) == "str" and not (
            isinstance(spec.get("max_len"), int) and spec["max_len"] > 0
        ):
            raise ValueError(f"{where}: text needs a positive max_len")
        if kind == "choice" and not (
            isinstance(spec.get("choices"), list | tuple)
            and spec["choices"]
            and all(isinstance(choice, str) for choice in spec["choices"])
        ):
            raise ValueError(f"{where}: choice needs a list of string choices")
        if spec.get("stdin") and kind != "str":
            raise ValueError(f"{where}: only a str can come from stdin")


def _flag(name: str) -> str:
    return "--" + name.replace("_", "-")


def _check_value(name: str, spec: Mapping[str, Any], value: Any) -> None:
    kind = _scalar(spec["type"])
    if name in CONTEXT_ARGS:
        kind = "id"
    flag = _flag(name)
    if kind in ("id", "int"):
        if type(value) is not int:  # bool is an int to Python, not here
            raise Rejected(f"{flag} 必須是整數")
        if kind == "id" and not 0 < value <= MAX_ID:
            raise Rejected(f"{flag} 必須是 1 到 20 位數的正整數")
        if "min" in spec and value < spec["min"]:
            raise Rejected(f"{flag} 不可小於 {spec['min']}")
        if "max" in spec and value > spec["max"]:
            raise Rejected(f"{flag} 不可大於 {spec['max']}")
    elif kind == "str":
        if not isinstance(value, str):
            raise Rejected(f"{flag} 必須是文字")
        if "\x00" in value:
            raise Rejected(f"{flag} 含有 NUL 字元")
        if len(value) > spec["max_len"]:
            raise Rejected(f"{flag} 最多 {spec['max_len']} 個字元（收到 {len(value)}）")
        if len(value.strip()) < spec.get("min_len", 1):
            raise Rejected(f"{flag} 不可空白")
    elif kind == "bool":
        if type(value) is not bool:
            raise Rejected(f"{flag} 必須是 true 或 false")
    elif kind == "choice":
        if value not in spec["choices"]:
            raise Rejected(f"{flag} 必須是 {'、'.join(spec['choices'])} 之一")


def validate_args(schema: Mapping[str, Any], args: Any) -> dict:
    """The args of one command against its schema; Rejected with the reason otherwise."""
    if not isinstance(args, dict):
        raise Rejected("args 必須是 JSON 物件")
    specs = schema.get("args", {})
    for name in args:
        if name not in specs:
            raise Rejected(f"不認識的參數 {str(name)[:40]!r}")
    for name, spec in specs.items():
        if name not in args:
            if spec.get("required"):
                raise Rejected(f"缺少 {_flag(name)}")
            continue
        value = args[name]
        if spec["type"] in LIST_TYPES:
            limit = spec.get("max_items", DEFAULT_MAX_ITEMS)
            if not isinstance(value, list) or not value:
                raise Rejected(f"{_flag(name)} 必須是非空的清單")
            if len(value) > limit:
                raise Rejected(f"{_flag(name)} 最多 {limit} 個")
            for item in value:
                _check_value(name, spec, item)
        else:
            _check_value(name, spec, value)
    return args


def validate_request(
    body: Any,
    schemas: Mapping[str, Mapping[str, Any]],
    name: str | None = None,
    now: float | None = None,
) -> tuple[str, str, dict]:
    """(id, command, args) of a request read from the inbox as file `<name>.json`; Rejected
    with the reason for anything the Bot would not run."""
    if not isinstance(body, dict):
        raise Rejected("請求必須是 JSON 物件")
    if extra := set(body) - {"v", "id", "command", "args", "created", "by"}:
        raise Rejected(f"請求有不認識的欄位：{', '.join(sorted(map(repr, extra)))[:80]}")
    if type(body.get("v")) is not int or body["v"] != VERSION:
        raise Rejected(f"不支援的請求版本（應為 {VERSION}）")
    request_id = body.get("id")
    if not isinstance(request_id, str) or not REQUEST_ID.fullmatch(request_id):
        raise Rejected("id 必須是 32 個小寫十六進位字元")
    if name is not None and name != request_id:
        raise Rejected("檔名與 id 不符")
    created = body.get("created")
    if isinstance(created, bool) or not isinstance(created, int | float):
        raise Rejected("created 必須是時間戳")
    now = time.time() if now is None else now
    if not math.isfinite(created) or abs(now - created) > MAX_AGE_SECONDS:
        raise Rejected("請求的建立時間離現在超過一天，不執行")
    by = body.get("by")
    if not isinstance(by, str) or len(by) > MAX_BY_CHARS:
        raise Rejected(f"by 必須是最多 {MAX_BY_CHARS} 字的文字")
    command = body.get("command")
    if not isinstance(command, str) or command not in schemas:
        raise Rejected("不認識的指令")
    return request_id, command, validate_args(schemas[command], body.get("args"))


def merge_exports(kind: str, modules) -> dict:
    """HANDLERS or SCHEMAS of every handler module in one dict; a command defined twice is an
    error, not a silent override."""
    merged: dict = {}
    for module in modules:
        for command, value in getattr(module, kind).items():
            if command in merged:
                raise ValueError(f"{command} is defined twice in {kind}")
            merged[command] = value
    return merged


def handler_modules() -> tuple:
    # Imported on use: the handler modules import admin_server, which bot.py imports.
    from . import admin_guild, admin_members, admin_tracking

    return admin_members, admin_tracking, admin_guild


def load_schemas() -> dict[str, dict]:
    """Every command's schema from the handler modules, each checked."""
    schemas = merge_exports("SCHEMAS", handler_modules())
    for command, schema in schemas.items():
        check_schema(command, schema)
    return schemas


# --------------------------------------------------------------------------- command line


def _id_text(text: str) -> int:
    if not re.fullmatch(r"[0-9]{1,20}", text) or int(text) == 0:
        raise argparse.ArgumentTypeError("必須是 1 到 20 位數的正整數")
    return int(text)


def _timeout_text(text: str) -> float:
    try:
        value = float(text)
    except ValueError:
        raise argparse.ArgumentTypeError("必須是秒數") from None
    if not 0 < value <= 3600:
        raise argparse.ArgumentTypeError("必須介於 0 到 3600 秒")
    return value


def _add_argument(parser: argparse.ArgumentParser, name: str, spec: Mapping[str, Any]) -> None:
    kind = spec["type"]
    scalar = "id" if name in CONTEXT_ARGS else _scalar(kind)
    options: dict[str, Any] = {
        "dest": f"arg_{name}",
        "required": bool(spec.get("required")),
        "help": spec.get("help", ""),
    }
    if spec.get("metavar"):
        options["metavar"] = spec["metavar"]
    elif scalar not in ("bool", "choice"):  # a flag takes none; choices list themselves
        options["metavar"] = name.upper()  # not the internal dest, arg_<name>
    if scalar == "bool":
        options.update(action="store_true", default=None)
    else:
        options["type"] = {"id": _id_text, "int": int, "str": str, "choice": str}[scalar]
        if scalar == "choice":
            options["choices"] = list(spec["choices"])
        if kind in LIST_TYPES:
            options.update(action="extend", nargs="+")
    if spec.get("stdin"):
        options["help"] = (options["help"] + "（寫 - 則從標準輸入讀）").strip()
    parser.add_argument(_flag(name), **options)


def _shared_options() -> tuple[argparse.ArgumentParser, argparse.ArgumentParser]:
    """(--json alone, --json and --timeout): parents for the CLI's own and the Bot's commands."""
    as_json = argparse.ArgumentParser(add_help=False)
    as_json.add_argument("--json", action="store_true", help="輸出 JSON 而不是文字")
    common = argparse.ArgumentParser(add_help=False, parents=[as_json])
    common.add_argument(
        "--timeout", type=_timeout_text, help="最多等幾秒（預設依指令而定）；逾時未開始就撤回"
    )
    return as_json, common


class _Parser(argparse.ArgumentParser):
    def error(self, message: str):  # a usage error exits 1: 2 means "not finished in time"
        self.print_usage(sys.stderr)
        self.exit(1, f"{self.prog}: 錯誤：{message}\n")


def build_parser(schemas: Mapping[str, Mapping[str, Any]]) -> argparse.ArgumentParser:
    parser = _Parser(
        prog="python -m discord_codex_bot.admin",
        description="在執行中的 Bot 裡執行維運指令（不需重啟）。指令交給 Bot 執行，結果再讀回來。",
        epilog=SEE_ALSO,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    as_json, common = _shared_options()
    top = parser.add_subparsers(dest="group", required=True, metavar="<指令>")
    result = top.add_parser("result", help="查看一個請求的結果", parents=[as_json])
    result.add_argument("request_id", metavar="id")
    top.add_parser("pending", help="列出還在排隊或執行中的請求")
    groups: dict[str, list[str]] = {}
    for command in sorted(schemas):
        group, _, action = command.partition(".")
        groups.setdefault(group, []).append(action)
    for group, actions in groups.items():
        if "" in actions and len(actions) > 1:
            raise ValueError(f"{group} is both a command and a group")
        if actions == [""]:
            leaves = [(top, group, group)]
        else:
            holder = top.add_parser(group, help=f"{group}：{', '.join(actions)}")
            sub = holder.add_subparsers(dest="action", required=True, metavar="<動作>")
            leaves = [(sub, action, f"{group}.{action}") for action in actions]
        for subparsers, word, command in leaves:
            schema = schemas[command]
            leaf = subparsers.add_parser(
                word,
                help=schema["help"],
                description=schema.get("description") or schema["help"],
                parents=[common],
                formatter_class=argparse.RawDescriptionHelpFormatter,
            )
            for name, spec in schema.get("args", {}).items():
                _add_argument(leaf, name, spec)
            leaf.set_defaults(command=command)
    return parser


# Control characters but newline and tab, DEL and the C1 range: a result may carry text from
# outside the Bot (a post title, a page, a model's answer), and an escape sequence in it must
# reach the operator's terminal as text, not as a command to the terminal.
CONTROL = re.compile(r"[\x00-\x08\x0b-\x1f\x7f-\x9f]")


def printable(text: str) -> str:
    """Text safe to print: each control character shown as its \\xNN escape."""
    return CONTROL.sub(lambda match: f"\\x{ord(match.group()):02x}", text)


def _render(result: Any) -> str:
    if isinstance(result, dict) and isinstance(result.get("text"), str):
        return result["text"]
    if isinstance(result, str):
        return result
    return json.dumps(result, ensure_ascii=False, indent=2)


def report(request_id: str, status: str, document: dict | None, as_json: bool) -> int:
    """Print an outcome; returns the exit code (0 done, 1 failed or rejected, 2 unfinished)."""
    if status == DONE:
        code = 0 if document and document.get("ok") else 1
    else:
        code = 1 if status == REJECTED else 2
    if as_json:
        text = json.dumps(
            {"status": status, "id": request_id, **(document or {})},
            ensure_ascii=False,
            indent=2,
        )
        # json.dumps escapes C0 itself; DEL and C1 become \\u escapes, still the same JSON.
        print(CONTROL.sub(lambda match: f"\\u{ord(match.group()):04x}", text))
        return code
    if status == DONE and code == 0:
        print(printable(_render(document.get("result"))))
    elif status == DONE:
        print(printable(f"失敗：{document.get('error') or '（沒有說明）'}"), file=sys.stderr)
    elif status == REJECTED:
        print(printable(f"被拒絕，沒有執行：{document.get('reason')}"), file=sys.stderr)
    elif status == "withdrawn":
        print("Bot 沒有接手這個請求；已撤回，什麼都沒執行。", file=sys.stderr)
    else:
        print(
            f"仍在執行中；之後用 `admin result {request_id}` 查看結果。",
            file=sys.stderr,
        )
    return code


def _show_time(stamp: Any) -> str:
    try:
        return show_time(float(stamp))
    except (TypeError, ValueError, OverflowError, OSError):
        return "?"


def pending(root: Path) -> list[str]:
    lines = []
    for status, label in ((INBOX, "排隊中"), (PROCESSING, "執行中")):
        for path in sorted((root / status).glob("*.json")):
            try:
                if path.lstat().st_size > MAX_REQUEST_BYTES:
                    raise ValueError("oversize")
                body = json.loads(path.read_text("utf-8"))
                lines.append(
                    f"{path.stem}  {label}  {str(body.get('command'))[:40]}  "
                    f"{_show_time(body.get('created'))}  {str(body.get('by'))[:MAX_BY_CHARS]}"
                )
            except (OSError, ValueError, AttributeError):
                lines.append(f"{path.stem}  {label}  （無法讀取）")
    return lines


def _result_command(root: Path, request_id: str, as_json: bool) -> int:
    if not REQUEST_ID.fullmatch(request_id):
        print("id 必須是 32 個小寫十六進位字元", file=sys.stderr)
        return 1
    if found := read_result(root, request_id):
        return report(request_id, found[0], found[1], as_json)
    if (root / INBOX / f"{request_id}.json").exists():
        print("還在排隊，Bot 尚未接手。", file=sys.stderr)
        return 2
    if (root / PROCESSING / f"{request_id}.json").exists():
        return report(request_id, "running", None, as_json)
    print("找不到這個 id（結果保留 7 天）。", file=sys.stderr)
    return 1


def _arguments(namespace: argparse.Namespace, schema: Mapping[str, Any]) -> dict:
    args = {}
    for name, spec in schema.get("args", {}).items():
        value = getattr(namespace, f"arg_{name}")
        if value is None:
            continue
        if spec.get("stdin") and value == "-":
            value = sys.stdin.read()
        args[name] = value
    return args


def main(argv: list[str] | None = None, root: Path | None = None) -> int:
    schemas = load_schemas()
    namespace = build_parser(schemas).parse_args(argv)
    if root is None:
        root = admin_root(load_config())
    try:
        check_root(root)
    except UnsafeDirectory as error:
        print(f"拒絕執行：{error}", file=sys.stderr)
        return 1
    if namespace.group == "pending":
        print(printable("\n".join(pending(root))) or "（沒有）")
        return 0
    if namespace.group == "result":
        return _result_command(root, namespace.request_id, namespace.json)
    command = namespace.command
    schema = schemas[command]
    try:
        args = validate_args(schema, _arguments(namespace, schema))
        request_id = write_request(root, command, args)
    except (Rejected, ValueError) as error:
        print(f"沒有送出：{error}", file=sys.stderr)
        return 1
    timeout = namespace.timeout or float(schema.get("timeout", DEFAULT_TIMEOUT)) + WAIT_MARGIN
    status, document = wait_result(root, request_id, timeout)
    return report(request_id, status, document, namespace.json)


if __name__ == "__main__":
    raise SystemExit(main())
