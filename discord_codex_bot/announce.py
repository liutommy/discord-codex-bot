"""Announcements: an owner-approved message is sent now, or queued for a time or for the Bot's
next start, and every queued item is posted at most once.

Run inside the bot container, where the Bot's token and guild allowlist are:

    python -m discord_codex_bot.announce send  --channel <id>                 < text.md
    python -m discord_codex_bot.announce queue --channel <id> [--at "YYYY-MM-DD HH:MM"]
                                                [--after-deploy] [--not-after "YYYY-MM-DD HH:MM"]
                                                < text.md
    python -m discord_codex_bot.announce list

`--after-deploy` means the next start of the Bot after the item was queued — a deploy, or any
restart (a reboot of the host, a crash brought back by dcb-up) — whichever comes first.

Nothing is baked into the image and a start never creates an announcement — it only lets items
already in the queue go out when due (an `--after-deploy` one is due then). The old start-up
announcement kept its "already posted" record in a file and re-posted on 2026-10-02 when that
record was lost (owner 2026-10-07: replace it with this). The queue lives in
CODEX_HOME/announce-queue — persistent, outside git, the image and the nightly backup, and not
writable by the models (Codex runs read-only, agy denies writes). Times are Asia/Taipei.

An item is claimed by renaming it to `.sending` before it is posted and deleted after. A crash in
between leaves it `.sending` and it is never posted again — the owner chose a missed post over a
repeated one — and the owner is alerted to check the channel. Discord answering with a client
error means nothing was posted, so the item goes back to the queue (at most MAX_FAILURES times);
any other failure is treated like the crash. An item past its `not_after` is set aside, not posted
late: the host gets paused for hours.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
import secrets
import sys
import time
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import aiohttp
import discord

from .clock import STEP_SECONDS, sleep_for
from .config import Config, load_config

LOGGER = logging.getLogger(__name__)
TAIPEI = ZoneInfo("Asia/Taipei")
QUEUE = "announce-queue"
MAX_CHARS = 2000  # one Discord message
MAX_FAILURES = 3
# How long an item may wait past its time before it is stale and set aside instead.
TIMED_GRACE = 2 * 3600
DEPLOY_GRACE = 24 * 3600
NOW_GRACE = 3600
API = "https://discord.com/api/v10"


def queue_dir(config: Config) -> Path:
    return config.codex_home / QUEUE


def parse_time(text: str) -> float:
    """A "YYYY-MM-DD HH:MM" in Taipei time, as a timestamp."""
    return datetime.strptime(text.strip(), "%Y-%m-%d %H:%M").replace(tzinfo=TAIPEI).timestamp()


def show_time(stamp: float) -> str:
    return datetime.fromtimestamp(stamp, TAIPEI).strftime("%Y-%m-%d %H:%M")


def make_item(
    channel: int,
    text: str,
    at: float | None = None,
    after_deploy: bool = False,
    not_after: float | None = None,
    now: float | None = None,
) -> dict:
    """A queue item, checked; ValueError for anything that could not be posted as given."""
    now = time.time() if now is None else now
    text = text.strip()
    if not text or len(text) > MAX_CHARS:
        raise ValueError(f"text must be 1-{MAX_CHARS} characters, got {len(text)}")
    if at is not None and after_deploy:
        raise ValueError("choose --at or --after-deploy, not both")
    if not_after is None:
        not_after = (
            at + TIMED_GRACE if at is not None
            else now + DEPLOY_GRACE if after_deploy
            else now + NOW_GRACE
        )  # fmt: skip
    if not_after <= max(now, at or now):
        raise ValueError("not_after must be later than now and than --at")
    return {
        "channel": int(channel),
        "text": text,
        "at": at,
        "after_deploy": bool(after_deploy),
        "not_after": not_after,
        "queued_at": now,
        "failures": 0,
    }


def write_item(directory: Path, item: dict, name: str = "") -> Path:
    """Atomically (scratch file + os.replace): the Bot never reads half an item."""
    directory.mkdir(parents=True, exist_ok=True)
    name = name or f"{time.strftime('%Y%m%d-%H%M%S')}-{secrets.token_hex(3)}.json"
    path = directory / name
    scratch = directory / f".{name}.tmp"
    scratch.write_text(json.dumps(item, ensure_ascii=False), "utf-8")
    os.replace(scratch, path)
    return path


def _read_item(path: Path) -> dict:
    item = json.loads(path.read_text("utf-8"))
    if not isinstance(item, dict):
        raise ValueError("not an object")
    checked = make_item(
        item["channel"],
        item["text"],
        item.get("at"),
        item.get("after_deploy", False),
        item["not_after"],
        now=min(float(item["queued_at"]), float(item["not_after"]) - 1),
    )
    checked["queued_at"] = float(item["queued_at"])
    checked["failures"] = int(item.get("failures", 0))
    return checked


def _set_aside(path: Path, suffix: str) -> Path:
    target = path.with_suffix(suffix)
    os.replace(path, target)
    return target


async def announce_pass(
    client: discord.Client, config: Config, alerts, started_at: float, now: float, first: bool
) -> int:
    """Post every queued item that is due; returns how many were posted."""
    directory = queue_dir(config)
    if not directory.is_dir():
        return 0
    if first:
        for stuck in sorted(directory.glob("*.sending")):
            LOGGER.error("Announcement %s was being sent when the Bot stopped", stuck.name)
            await alerts.alert_now(
                "announce",
                "stuck",
                f"📢 公告 `{stuck.name}` 送出途中中斷，可能發了也可能沒發；不會再自動發。"
                "請看頻道確認後，刪掉它或改名回 .json 重排。",
            )
    posted = 0
    for path in sorted(directory.glob("*.json")):
        try:
            item = _read_item(path)
        except (OSError, ValueError, KeyError, TypeError) as error:
            _set_aside(path, ".bad")
            LOGGER.error("Announcement %s is malformed (%s); set aside", path.name, error)
            await alerts.alert_now("announce", "bad", f"📢 公告 `{path.name}` 格式不對，已移開")
            continue
        if now > item["not_after"]:
            _set_aside(path, ".expired")
            LOGGER.warning("Announcement %s missed its window; not posted late", path.name)
            await alerts.alert_now(
                "announce",
                "expired",
                f"📢 公告 `{path.name}` 過了最晚時間 {show_time(item['not_after'])} 還沒發，"
                "沒有補發（可能是主機被暫停）",
            )
            continue
        if item["at"] is not None and now < item["at"]:
            continue
        if item["after_deploy"] and started_at <= item["queued_at"]:
            continue  # waits for a Bot started after it was queued
        claim = path.with_suffix(".sending")
        try:
            os.replace(path, claim)  # claimed: from here it is never posted twice
        except OSError:
            continue
        channel = client.get_channel(item["channel"])
        guild = getattr(channel, "guild", None)
        if channel is None or guild is None or guild.id not in config.allowed_guild_ids:
            _set_aside(claim, ".bad")
            LOGGER.error("Announcement %s: channel %s not usable", path.name, item["channel"])
            await alerts.alert_now(
                "announce", "bad", f"📢 公告 `{path.name}` 的頻道不在允許的伺服器裡，已移開"
            )
            continue
        try:
            await channel.send(item["text"], allowed_mentions=discord.AllowedMentions.none())
        except discord.HTTPException as error:
            if error.status >= 500:  # the server failed: it may have posted
                LOGGER.error("Announcement %s: Discord %s; left .sending", path.name, error.status)
                await alerts.alert_now(
                    "announce",
                    "stuck",
                    f"📢 公告 `{path.name}` 送出時 Discord 回 {error.status}，可能發了也可能沒發；"
                    "不會再自動發，請看頻道確認",
                )
                continue
            item["failures"] += 1
            if item["failures"] >= MAX_FAILURES:
                write_item(directory, item, path.with_suffix(".failed").name)
                claim.unlink(missing_ok=True)
                await alerts.alert_now(
                    "announce",
                    "failed",
                    f"📢 公告 `{path.name}` 發送失敗 {MAX_FAILURES} 次，已停止",
                )
            else:
                write_item(directory, item, path.name)  # not posted: back in the queue
                claim.unlink(missing_ok=True)
            LOGGER.warning("Announcement %s: Discord %s (%s)", path.name, error.status, error.text)
            continue
        except Exception:
            LOGGER.exception("Announcement %s: unclear whether it was posted; left .sending", path)
            await alerts.alert_now(
                "announce",
                "stuck",
                f"📢 公告 `{path.name}` 送出時出錯，可能發了也可能沒發；不會再自動發，請看頻道確認",
            )
            continue
        claim.unlink(missing_ok=True)
        posted += 1
        LOGGER.info("Announcement %s posted to channel %s", path.name, item["channel"])
    return posted


async def announce_loop(client: discord.Client, config: Config, alerts, started_at: float) -> None:
    """Check the queue once a step of wall-clock time (a paused host catches up on resume)."""
    first = True
    while True:
        try:
            await announce_pass(client, config, alerts, started_at, time.time(), first)
        except Exception:
            LOGGER.exception("Announcement pass failed")
        first = False
        await sleep_for(STEP_SECONDS)


# --------------------------------------------------------------------------- command line


async def send_now(config: Config, channel: int, text: str) -> str:
    """Post `text` to `channel` right away through Discord's REST API; returns the message id.
    The channel must be in an allowed guild. The token never leaves this process."""
    item = make_item(channel, text)  # same checks as a queued one
    headers = {"Authorization": f"Bot {config.discord_token}", "Content-Type": "application/json"}
    async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=30)) as session:
        async with session.get(f"{API}/channels/{item['channel']}", headers=headers) as response:
            if response.status != 200:
                raise RuntimeError(f"channel lookup failed: HTTP {response.status}")
            found = await response.json()
        if int(found.get("guild_id") or 0) not in config.allowed_guild_ids:
            raise RuntimeError("channel is not in an allowed guild")
        body = {"content": item["text"], "allowed_mentions": {"parse": []}}
        async with session.post(
            f"{API}/channels/{item['channel']}/messages", headers=headers, json=body
        ) as response:
            if response.status != 200:
                raise RuntimeError(f"send failed: HTTP {response.status}")
            return str((await response.json())["id"])


def list_items(config: Config) -> list[str]:
    directory = queue_dir(config)
    lines = []
    for path in sorted(directory.glob("*")) if directory.is_dir() else []:
        if path.name.startswith("."):
            continue
        try:
            item = json.loads(path.read_text("utf-8"))
            when = (
                show_time(item["at"]) if item.get("at")
                else "下次啟動（部署或重啟）後" if item.get("after_deploy") else "下一輪"
            )  # fmt: skip
            lines.append(
                f"{path.name}  頻道 {item['channel']}  {when}  最晚 {show_time(item['not_after'])}"
                f"  {item['text'][:40]!r}"
            )
        except (OSError, ValueError, KeyError, TypeError):
            lines.append(f"{path.name}  (unreadable)")
    return lines


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m discord_codex_bot.announce")
    commands = parser.add_subparsers(dest="command", required=True)
    send = commands.add_parser("send", help="post now; text on stdin")
    send.add_argument("--channel", type=int, required=True)
    queue = commands.add_parser("queue", help="queue for a time or the Bot's next start")
    queue.add_argument("--channel", type=int, required=True)
    queue.add_argument("--at", default="", help='"YYYY-MM-DD HH:MM", Taipei')
    queue.add_argument(
        "--after-deploy", action="store_true", help="the Bot's next start: a deploy or any restart"
    )
    queue.add_argument("--not-after", default="", help='"YYYY-MM-DD HH:MM", Taipei')
    commands.add_parser("list", help="show the queue")
    args = parser.parse_args(argv)
    config = load_config()
    if args.command == "list":
        print("\n".join(list_items(config)) or "(empty)")
        return 0
    text = sys.stdin.read()
    if args.command == "send":
        print("posted message", asyncio.run(send_now(config, args.channel, text)))
        return 0
    item = make_item(
        args.channel,
        text,
        parse_time(args.at) if args.at else None,
        args.after_deploy,
        parse_time(args.not_after) if args.not_after else None,
    )
    print("queued", write_item(queue_dir(config), item).name)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
