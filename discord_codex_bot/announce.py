from __future__ import annotations

import hashlib
import json
import logging
from pathlib import Path

import discord

from .config import Config

LOGGER = logging.getLogger(__name__)
LATEST = "latest.md"
# How far back a channel is read for an earlier copy of the announcement. Announcement channels
# are quiet; 500 messages is five history pages per channel, once per start.
HISTORY_LIMIT = 500


def pending_announcement(config: Config) -> tuple[str, str]:
    """(digest, text) of announce/latest.md, or ("", "") when there is nothing to post."""
    try:
        text = (config.announce_dir / LATEST).read_text("utf-8").strip()
    except OSError:
        return "", ""
    if not text:
        return "", ""
    return hashlib.sha256(text.encode("utf-8")).hexdigest()[:16], text


def _state_path(config: Config) -> Path:
    return config.codex_home / "announced.json"


def _load(config: Config) -> dict[str, str]:
    try:
        return dict(json.loads(_state_path(config).read_text("utf-8")))
    except (OSError, ValueError):
        return {}


async def _already_posted(client: discord.Client, channel, text: str) -> bool | None:
    """Whether this Bot already posted `text` in the channel's last HISTORY_LIMIT messages;
    None when the history cannot be read."""
    me = getattr(client.user, "id", None)
    try:
        async for message in channel.history(limit=HISTORY_LIMIT):
            # Discord trims a message's edges, which a cut at 2000 characters can expose.
            if message.author.id == me and message.content.strip() == text.strip():
                return True
    except discord.HTTPException:
        return None
    return False


async def announce_once(client: discord.Client, config: Config) -> int:
    """Post announce/latest.md once per configured channel (ANNOUNCE_CHANNEL_IDS). Off by
    default: with no channel configured nothing is ever posted — no guild-wide fallback.

    The approval is single-use per channel. announced.json alone did not hold that: on
    2026-10-02 a start on an empty CODEX_HOME posted an approved announcement a second time. So
    the channel itself is the record too — a copy already there is never posted again, and a
    channel whose history cannot be read is skipped rather than risked."""
    digest, text = pending_announcement(config)
    if not digest or not config.announce_channel_ids:
        return 0
    if digest != config.announce_approved:
        LOGGER.info(
            "Announcement %s not approved (ANNOUNCE_APPROVED=%r); not posting",
            digest,
            config.announce_approved,
        )
        return 0
    state = _load(config)
    posted = 0
    changed = False
    for channel_id in sorted(config.announce_channel_ids):
        if state.get(str(channel_id)) == digest:
            continue
        channel = client.get_channel(channel_id)
        guild = getattr(channel, "guild", None)
        if channel is None or guild is None or guild.id not in config.allowed_guild_ids:
            LOGGER.warning("Announcement channel %s: not in an allowed guild", channel_id)
            continue
        seen = await _already_posted(client, channel, text[:2000])
        if seen is None:
            LOGGER.warning("Announcement channel %s: history unreadable; not posting", channel_id)
            continue
        if seen:
            LOGGER.info(
                "Announcement %s already in channel %s; not posting again", digest, channel_id
            )
            state[str(channel_id)] = digest
            changed = True
            continue
        try:
            await channel.send(text[:2000])
        except discord.HTTPException:
            LOGGER.exception("Announcement failed for channel %s", channel_id)
            continue
        state[str(channel_id)] = digest
        posted += 1
        changed = True
    if changed:
        _state_path(config).write_text(json.dumps(state), "utf-8")
    if posted:
        LOGGER.info("Announcement %s posted to %d channel(s)", digest, posted)
    return posted
