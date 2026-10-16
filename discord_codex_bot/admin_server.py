"""The Bot's side of the operator CLI (admin.py has the protocol and the CLI): every 2 s the
inbox is read, each request is claimed, checked and run inside this process, and its result is
written back. The commands come from the handler modules (admin_members, admin_tracking,
admin_guild), each exporting HANDLERS and SCHEMAS keyed by "<group>.<action>" (or a single word
such as "reset"); the CLI builds its options from the same SCHEMAS.

Handler: `async def handler(client, args: dict) -> dict`. `client` is the DiscordCodexClient and
`args` holds the validated arguments, absent options left out. It returns a JSON-able value,
by convention a dict whose "text" the CLI prints (the rest shows with --json). It raises:
- Rejected(reason) when it refuses before doing anything; the request goes to rejected/.
- AdminError(message) when the command ran and failed; done/ gets ok=false and the message.
Any other exception is ok=false with its type only, and the traceback is logged. Reasons and
messages are logged, so they never quote member text. A handler shares a slash command's logic
by calling the client method the slash command calls, never by copying its checks.

Schema, one per command:

    SCHEMAS = {
        "remind.add": {
            "help": "設定提醒",                  # one line in the CLI's list (required)
            "description": "…",                  # optional, longer: the command's --help
            "timeout": 60,                       # seconds the Bot allows it (default 60, max 900)
            "args": {
                "guild": {"type": "id", "required": True},
                "channel": {"type": "id", "required": True},
                "user": {"type": "id", "required": True, "help": "以這位成員的身分"},
                "when": {"type": "str", "required": True, "max_len": 100},
                "text": {"type": "str", "required": True, "max_len": 1000, "stdin": True},
                "who": {"type": "list[id]", "max_items": 10},
            },
        },
    }

Argument types, each becoming `--name` (underscores as dashes):
- "id": a positive int of at most 20 digits (Discord ids; also reminder and watch numbers).
- "int": an int, bounded by optional "min" / "max".
- "str": text of at most "max_len" characters (required) and not blank, unless "min_len": 0.
  "stdin": True lets `--name -` read the value from standard input.
- "bool": a flag; present means true, absent leaves the key out.
- "choice": one of "choices" (strings).
- "list[id]", "list[int]", "list[str]": one or more values (`--who 1 2` or `--who 1 --who 2`),
  at most "max_items" (default 10), each checked as the scalar type.
Other keys: "required" (default false), "help", "metavar". Unknown keys fail the schema check.

Names with a fixed meaning, checked by the server before the handler runs, so a handler never
sees an unchecked one (check_context):
- "guild" must be in ALLOWED_GUILD_IDS.
- "channel" must resolve (cache, else the API), belong to "guild", and pass the client's
  `_access` like a slash command in that channel.
- "user", when "guild" is given, must be a member of that guild.
- "who" (a list) needs "guild", and each id must be a member of it.
A handler that needs the channel or member object calls resolve_guild_channel / require_member
again: the lookups are cached.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import os
import re
import secrets
import shutil
import stat
import time
from collections.abc import Awaitable, Callable, Mapping
from pathlib import Path
from typing import Any

import discord

from .admin import (
    COMMAND_NAME,
    DEFAULT_TIMEOUT,
    DONE,
    INBOX,
    MAX_REQUEST_BYTES,
    PROCESSING,
    REJECTED,
    REQUEST_ID,
    SUBDIRS,
    AdminError,
    Rejected,
    UnsafeDirectory,
    check_root,
    handler_modules,
    load_schemas,
    merge_exports,
    validate_request,
    write_private,
)

__all__ = [
    "AdminError",
    "AdminServer",
    "Handler",
    "Rejected",
    "check_context",
    "require_guild",
    "require_member",
    "resolve_guild_channel",
]

LOGGER = logging.getLogger(__name__)
Handler = Callable[[Any, dict], Awaitable[Any]]
POLL_SECONDS = 2.0
KEEP_DAYS = 7
PRUNE_EVERY = 3600.0
MAX_RUNNING = 4  # a 10-minute ask must not hold up a quick lookup behind it
INTERRUPTED = "Bot 在開始執行前重啟；沒有執行，也不會再執行（interrupted before it ran）"
# Restarted after the handler began: not run again, but it may have changed something already.
PARTLY = (
    "Bot 在執行途中重啟；不會再執行，但可能已經做了一部分，請確認結果（interrupted while running）"
)
STARTED = ".started"  # next to processing/<id>.json once its handler has been called
DUPLICATE = "這個 id 已經有結果；同一個請求不會執行兩次"


def load_handlers() -> dict[str, Handler]:
    return merge_exports("HANDLERS", handler_modules())


# --------------------------------------------------------------------------- context checks


def require_guild(client: Any, guild_id: int) -> None:
    """Rejected unless the guild is on the allowlist (the same reason access.py gives)."""
    if guild_id not in client.config.allowed_guild_ids:
        raise Rejected(f"伺服器 {guild_id} 不在允許清單內。")


async def resolve_guild_channel(client: Any, guild_id: int, channel_id: int) -> Any:
    """The channel (or thread), when it belongs to the allowed guild and passes the same
    `_access` a slash command used there would; Rejected otherwise."""
    require_guild(client, guild_id)
    channel = client.get_channel(channel_id)
    if channel is None:
        try:
            channel = await client.fetch_channel(channel_id)
        except discord.NotFound:
            raise Rejected(f"找不到頻道 {channel_id}。") from None
        except discord.Forbidden:
            raise Rejected(f"Bot 看不到頻道 {channel_id}。") from None
        except discord.HTTPException as error:
            raise Rejected(f"查不到頻道 {channel_id}（HTTP {error.status}）。") from None
    if getattr(getattr(channel, "guild", None), "id", None) != guild_id:
        raise Rejected(f"頻道 {channel_id} 不屬於伺服器 {guild_id}。")
    if reason := client._access(guild_id, channel, channel_id):
        raise Rejected(reason)
    return channel


async def require_member(client: Any, guild_id: int, user_id: int) -> Any:
    """The guild's Member for `user_id`; Rejected when they are not a member of that guild."""
    require_guild(client, guild_id)
    guild = client.get_guild(guild_id)
    member = guild.get_member(user_id) if guild is not None else None
    if member is not None:
        return member
    if guild is None:
        try:
            guild = await client.fetch_guild(guild_id)
        except discord.HTTPException as error:
            raise Rejected(f"Bot 讀不到伺服器 {guild_id}（HTTP {error.status}）。") from None
    try:
        return await guild.fetch_member(user_id)
    except discord.NotFound:
        raise Rejected(f"使用者 {user_id} 不是伺服器 {guild_id} 的成員。") from None
    except discord.HTTPException as error:
        raise Rejected(f"無法確認使用者 {user_id} 的成員身分（HTTP {error.status}）。") from None


async def check_context(client: Any, args: Mapping[str, Any]) -> None:
    """The fixed-meaning arguments (see the module docstring); Rejected on the first failure."""
    guild_id = args.get("guild")
    if guild_id is not None:
        require_guild(client, guild_id)
    elif "channel" in args or "who" in args:
        raise Rejected("--channel／--who 需要同時給 --guild。")
    if "channel" in args:
        await resolve_guild_channel(client, guild_id, args["channel"])
    if guild_id is not None and "user" in args:
        await require_member(client, guild_id, args["user"])
    for target in args.get("who", ()):
        await require_member(client, guild_id, target)


# --------------------------------------------------------------------------- server


def _encode(document: dict) -> bytes:
    # ASCII escapes: a request's own text (a lone surrogate from a hand-written file) must never
    # stop its rejection from being written.
    return json.dumps(document, ensure_ascii=True, default=str).encode("ascii")


def _discard(path: Path) -> None:
    try:
        path.unlink()
    except FileNotFoundError:
        pass
    except IsADirectoryError:
        shutil.rmtree(path, ignore_errors=True)


def _label(value: Any) -> str:
    """A command name or file stem as the log shows it: read from a file, so a name that is
    not one of ours is quoted and cut short (a file name may even hold a newline)."""
    if isinstance(value, str) and (COMMAND_NAME.fullmatch(value) or REQUEST_ID.fullmatch(value)):
        return value
    return repr(value)[:64]


class AdminServer:
    def __init__(
        self,
        client: Any,
        root: Path,
        handlers: Mapping[str, Handler] | None = None,
        schemas: Mapping[str, Mapping[str, Any]] | None = None,
        poll_seconds: float = POLL_SECONDS,
    ) -> None:
        self.client = client
        self.root = root
        self.handlers = dict(load_handlers() if handlers is None else handlers)
        self.schemas = dict(load_schemas() if schemas is None else schemas)
        if missing := set(self.schemas) ^ set(self.handlers):
            raise ValueError(f"admin commands without both a handler and a schema: {missing}")
        self.poll_seconds = poll_seconds
        self.running: dict[str, asyncio.Task] = {}  # by file stem
        self._task: asyncio.Task | None = None
        self._pruned_at = 0.0
        self._unsafe = ""

    def _dir(self, name: str) -> Path:
        return self.root / name

    # ---- start-up

    def prepare(self) -> int:
        """Create the directories 0700 (chmod them back if they exist), then settle what a
        restart left in processing/; returns how many requests that interrupted."""
        for path in (self.root, *(self._dir(name) for name in SUBDIRS)):
            path.mkdir(mode=0o700, parents=True, exist_ok=True)
            os.chmod(path, 0o700)
        check_root(self.root)
        return self.recover()

    def recover(self) -> int:
        """Requests left in processing/ by a crash or restart go to rejected/, never run again;
        one whose result was already written only lost its clean-up."""
        interrupted = 0
        for path in sorted(self._dir(PROCESSING).glob("*.json")):
            stem = path.name.removesuffix(".json")
            marker = path.with_suffix(STARTED)
            try:
                if (self._dir(DONE) / path.name).exists():
                    continue
                request = None
                with contextlib.suppress(Exception):  # whatever the file holds, it is set aside
                    request = json.loads(self._read(path))
                self._reject(stem, request, PARTLY if marker.exists() else INTERRUPTED)
                interrupted += 1
            except Exception:
                LOGGER.exception("Admin: interrupted request %s not recorded", _label(stem))
            finally:
                _discard(path)
                _discard(marker)
        for marker in self._dir(PROCESSING).glob(f"*{STARTED}"):  # its request already gone
            _discard(marker)
        return interrupted

    def start(self) -> asyncio.Task:
        self._task = asyncio.get_running_loop().create_task(self.run_forever())
        return self._task

    def stop(self) -> None:
        """Cancel the loop and whatever is running; a cancelled request stays in processing/
        and is moved to rejected/ at the next start."""
        if self._task is not None:
            self._task.cancel()
        for task in list(self.running.values()):
            task.cancel()

    async def run_forever(self) -> None:
        try:
            if interrupted := self.prepare():
                LOGGER.warning("Admin: %d request(s) interrupted by the restart", interrupted)
        except (OSError, UnsafeDirectory):
            LOGGER.exception("Admin inbox %s unusable; operator commands are off", self.root)
            return
        except Exception:  # one bad leftover must not switch the operator channel off
            LOGGER.exception("Admin: recovery after the restart failed; serving anyway")
        await self.client.wait_until_ready()
        while True:
            try:
                await self.pass_once()
            except Exception:
                LOGGER.exception("Admin pass failed")
            await asyncio.sleep(self.poll_seconds)

    # ---- the loop

    def _safe(self) -> bool:
        """Nothing is read while the directory is not ours alone; logged once per change."""
        try:
            check_root(self.root)
        except UnsafeDirectory as error:
            if str(error) != self._unsafe:
                LOGGER.error("Admin inbox refused: %s", error)
            self._unsafe = str(error)
            return False
        self._unsafe = ""
        return True

    def _inbox(self) -> list[Path]:
        entries = []
        for path in self._dir(INBOX).glob("*.json"):
            try:
                entries.append((path.lstat().st_mtime, path.name, path))
            except OSError:  # withdrawn while listed
                continue
        return [path for *_, path in sorted(entries)]

    async def pass_once(self, now: float | None = None) -> int:
        """Claim and start what the inbox holds, oldest first; returns how many started."""
        if not self._safe():
            return 0
        now = time.time() if now is None else now
        if now - self._pruned_at >= PRUNE_EVERY:
            self._pruned_at = now
            if removed := self.prune(now):
                LOGGER.info("Admin: pruned %d old result(s)", removed)
        started = 0
        for path in self._inbox():
            if len(self.running) >= MAX_RUNNING:
                break
            stem = path.name.removesuffix(".json")
            if stem in self.running:
                continue  # a copy of one still running: judged a duplicate once that is done
            claimed = self._dir(PROCESSING) / path.name
            try:
                os.replace(path, claimed)  # claimed: the CLI can no longer withdraw it
            except OSError:
                continue
            task = asyncio.create_task(self._handle(claimed, now))
            self.running[stem] = task
            task.add_done_callback(lambda _task, stem=stem: self.running.pop(stem, None))
            started += 1
        return started

    async def drain(self) -> None:
        """Wait for every running request to finish."""
        while self.running:
            await asyncio.gather(*self.running.values(), return_exceptions=True)

    def _read(self, path: Path) -> bytes:
        """The file's bytes: a regular file of ours, at most the size cap; Rejected unread
        otherwise (a symlink, a FIFO, someone else's file, an oversize one)."""
        flags = os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK
        try:
            fd = os.open(path, flags)
        except OSError as error:
            raise Rejected(f"無法開啟請求檔（{type(error).__name__}）") from None
        try:
            info = os.fstat(fd)
            if not stat.S_ISREG(info.st_mode):
                raise Rejected("請求不是一般檔案")
            if info.st_uid != os.getuid():
                raise Rejected(f"請求檔屬於 uid {info.st_uid}，不是 Bot 的使用者")
            if info.st_size > MAX_REQUEST_BYTES:
                raise Rejected(f"請求超過 {MAX_REQUEST_BYTES} 位元組，未讀取")
            data = os.read(fd, MAX_REQUEST_BYTES + 1)
        finally:
            os.close(fd)
        if len(data) > MAX_REQUEST_BYTES:
            raise Rejected(f"請求超過 {MAX_REQUEST_BYTES} 位元組，未讀取")
        return data

    def _reject(self, stem: str, request: Any, reason: str) -> Path:
        """rejected/<id>.json, never over an earlier result: a duplicate or a name that is
        not an id gets its own name, so `admin result <id>` still shows the original."""
        command = request.get("command") if isinstance(request, dict) else None
        directory = self._dir(REJECTED)
        target = directory / f"{stem}.json"
        if (
            not REQUEST_ID.fullmatch(stem)
            or target.exists()
            or (self._dir(DONE) / target.name).exists()
        ):
            safe = re.sub(r"[^0-9A-Za-z_-]", "_", stem)[:64] or "request"
            target = directory / f"{safe}.{secrets.token_hex(4)}.json"
        document = {"request": request, "reason": reason, "at": time.time()}
        write_private(target, _encode(document))
        LOGGER.warning("Admin request %s (%s) rejected: %s", _label(stem), _label(command), reason)
        return target

    async def _handle(self, claimed: Path, now: float) -> None:
        try:
            await self._settle(claimed, now)
        except Exception as error:  # still settled: the CLI must not be left saying "running"
            LOGGER.exception("Admin request %s could not be settled", _label(claimed.stem))
            self._give_up(claimed, error)

    def _give_up(self, claimed: Path, error: Exception) -> None:
        if not claimed.exists() or (self._dir(DONE) / claimed.name).exists():
            return
        started = claimed.with_suffix(STARTED).exists()
        reason = PARTLY if started else f"Bot 內部錯誤（{type(error).__name__}），沒有執行"
        with contextlib.suppress(Exception):
            self._reject(claimed.stem, None, reason)
        _discard(claimed)
        _discard(claimed.with_suffix(STARTED))

    async def _settle(self, claimed: Path, now: float) -> None:
        stem = claimed.name.removesuffix(".json")
        request: Any = None
        try:
            try:
                request = json.loads(self._read(claimed))
            except Rejected:
                raise
            except Exception as error:  # bad bytes, nesting too deep for the parser, …
                raise Rejected(f"不是有效的 JSON（{type(error).__name__}）") from None
            try:
                request_id, command, args = validate_request(request, self.schemas, stem, now)
            except Rejected:
                raise
            except Exception as error:  # a value no check expected (a 400-digit number)
                raise Rejected(f"請求無法檢查（{type(error).__name__}）") from None
            if any((self._dir(name) / claimed.name).exists() for name in (DONE, REJECTED)):
                raise Rejected(DUPLICATE)
            try:
                await check_context(self.client, args)
            except Rejected:
                raise
            except Exception as error:  # Discord could not be asked: nothing was run
                LOGGER.warning("Admin %s id=%s context check failed", command, request_id)
                raise Rejected(
                    f"無法向 Discord 確認伺服器、頻道或成員（{type(error).__name__}），沒有執行"
                ) from None
        except Rejected as rejection:
            self._reject(stem, request, rejection.reason)
            _discard(claimed)
            return
        timeout = float(self.schemas[command].get("timeout", DEFAULT_TIMEOUT))
        ok, result, error = False, None, None
        # From here on a restart cannot say "nothing ran": the handler may have changed things.
        write_private(claimed.with_suffix(STARTED), b"")
        try:
            result = await asyncio.wait_for(self.handlers[command](self.client, args), timeout)
            ok = True
        except Rejected as rejection:  # the handler refused before doing anything
            self._reject(stem, request, rejection.reason)
            _discard(claimed)
            _discard(claimed.with_suffix(STARTED))
            return
        except AdminError as failure:
            error = str(failure)
        except TimeoutError:
            error = f"超過 {timeout:g} 秒，已中止；可能只做了一部分"
        except Exception as failure:
            LOGGER.exception("Admin %s id=%s raised", command, request_id)
            error = f"執行出錯（{type(failure).__name__}），詳情在 Bot 的 log"
        document = {
            "id": request_id,
            "command": command,
            "ok": ok,
            "result": result,
            "error": error,
            "finished": time.time(),
        }
        try:
            data = _encode(document)
        except (TypeError, ValueError):
            LOGGER.exception("Admin %s id=%s: result not JSON", command, request_id)
            ok = False
            data = _encode({**document, "ok": False, "result": None, "error": "結果無法寫成 JSON"})
        write_private(self._dir(DONE) / f"{request_id}.json", data)
        _discard(claimed)
        _discard(claimed.with_suffix(STARTED))
        LOGGER.info("Admin %s id=%s ok=%s", command, request_id, ok)

    def prune(self, now: float | None = None) -> int:
        """Results older than KEEP_DAYS, and scratch files a crashed writer left behind."""
        cutoff = (time.time() if now is None else now) - KEEP_DAYS * 86400
        removed = 0
        for name in (DONE, REJECTED, INBOX):
            for path in self._dir(name).iterdir():
                if name == INBOX and not path.name.endswith(".tmp"):
                    continue
                try:
                    if path.lstat().st_mtime >= cutoff:
                        continue
                except FileNotFoundError:
                    continue
                _discard(path)
                removed += 1
        return removed
