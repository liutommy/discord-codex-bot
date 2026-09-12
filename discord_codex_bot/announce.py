from __future__ import annotations

import hashlib
import json
import logging
from pathlib import Path

import discord

from .config import Config

LOGGER = logging.getLogger(__name__)
LATEST = "latest.md"


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


async def announce_once(client: discord.Client, config: Config) -> int:
    """Post announce/latest.md once per configured channel (ANNOUNCE_CHANNEL_IDS). Off by
    default: with no channel configured nothing is ever posted — no guild-wide fallback."""
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
    for channel_id in sorted(config.announce_channel_ids):
        if state.get(str(channel_id)) == digest:
            continue
        channel = client.get_channel(channel_id)
        guild = getattr(channel, "guild", None)
        if channel is None or guild is None or guild.id not in config.allowed_guild_ids:
            LOGGER.warning("Announcement channel %s: not in an allowed guild", channel_id)
            continue
        try:
            await channel.send(text[:2000])
        except discord.HTTPException:
            LOGGER.exception("Announcement failed for channel %s", channel_id)
            continue
        state[str(channel_id)] = digest
        posted += 1
    if posted:
        _state_path(config).write_text(json.dumps(state), "utf-8")
        LOGGER.info("Announcement %s posted to %d channel(s)", digest, posted)
    return posted
