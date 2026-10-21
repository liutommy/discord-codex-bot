"""Read original Discord turns, independently of the backend holding their transcript."""

from __future__ import annotations

import json
import logging
import re
from pathlib import Path

import aiohttp
import discord

from . import tags
from .attachments import download_attachment, remove_request_dir, validate_attachment
from .config import Config
from .output import PROMPT_ECHO_CHARS, truncate

LOGGER = logging.getLogger(__name__)
# Answers examined per read, newest first: each costs a fetch (two with its question), and a
# deleted answer or one without a readable question is skipped, so the scan has its own bound.
MAX_SCAN = 16
FETCH_ERRORS = (discord.HTTPException, aiohttp.ClientError, TimeoutError)
EARLIER_TAG = re.compile(r'<earlier(?:\s+turns="([0-9]{1,8})")?\s*/>')


def requested_turns(text: str) -> int | None:
    """One read per round; repeated tags share the largest bounded request."""
    requests = tags.findall(EARLIER_TAG, text)
    return max(min(8, max(1, int(n or 4))) for n in requests) if requests else None


async def read_earlier(
    channel,
    message_ids: list[int],
    user_id: int,
    bot_id: int,
    turns: int,
    config: Config,
    trusted: frozenset[int] | set[int] = frozenset(),
) -> tuple[str, list[Path]]:
    """Fetch only in this channel; never trust a cached cross-channel reference.

    The answer index selects the conversation, then the original question proves whose turn
    it was (shared backend threads and legacy index entries can include other members).
    The caller defangs this result along with the other untrusted read results. An answer in
    `trusted` (recorded for this member and channel) counts without a question to point to: a
    slash command's answer has none, and carries the question in its own text.
    """

    async def fetch(message_id):
        try:
            message = await channel.fetch_message(message_id)
        except FETCH_ERRORS:
            return None
        return message if message.channel.id == channel.id else None

    async def referenced(message):
        reference = message.reference
        if (
            reference is None
            or reference.message_id is None
            or reference.channel_id not in (None, channel.id)
        ):
            return None
        return await fetch(reference.message_id)

    def original(message):
        # JSON strings keep member-supplied newlines from forging another speaker's record.
        return {
            "author": message.author.display_name,
            "author_id": message.author.id,
            "text": message.content,
            "embeds": [
                {"url": embed.url, "title": embed.title, "description": embed.description}
                for embed in message.embeds
            ],
        }

    found = []
    if channel is not None:
        for message_id in list(reversed(message_ids))[:MAX_SCAN]:
            answer = await fetch(message_id)
            if answer is None or answer.author.id != bot_id:
                continue
            member = await referenced(answer)
            if member is None and message_id in trusted:
                found.append((None, None, answer))
            elif member is not None and member.author.id == user_id:
                found.append((member, await referenced(member), answer))
            else:
                continue
            if len(found) >= turns:
                break

    # Newest first within the budget, so a long old turn cannot push out the latest ones; the
    # kept turns are then given oldest first, and only their images come along.
    kept, used = [], 0
    for member, quoted, answer in found:
        block = json.dumps(
            {
                "member": original(member) if member is not None else None,
                "quoted": original(quoted) if quoted is not None else None,
                "answer": truncate(answer.content, PROMPT_ECHO_CHARS),
            },
            ensure_ascii=False,
        )
        if kept and used + len(block) + 1 > config.link_max_chars:
            LOGGER.info("Earlier Discord result kept %d of %d turns", len(kept), len(found))
            break
        kept.append((answer, member, quoted, block))
        used += len(block) + 1
    # Images newest turn first, so the cap keeps the latest ones ("剛剛那張圖"); the bot's own
    # answers count too (a generated picture). The text stays oldest first.
    kept.reverse()  # oldest first from here on
    images, notes, seen = [], {}, set()
    try:
        for index in range(len(kept) - 1, -1, -1):
            answer, member, quoted, _ = kept[index]
            for message in (answer, member, quoted):
                for attachment in message.attachments if message is not None else ():
                    if attachment.id in seen or len(images) >= config.max_attachments:
                        continue
                    seen.add(attachment.id)
                    kind, suffix = validate_attachment(
                        attachment.content_type, attachment.filename, attachment.size, config
                    )
                    if kind != "image":
                        continue
                    try:
                        images.append(await download_attachment(attachment, suffix, config))
                    except (*FETCH_ERRORS, OSError):
                        notes[index] = "（較早訊息的一張圖片無法下載）"
    except BaseException:
        for path in images:
            remove_request_dir(path)
        raise
    blocks = []
    for index, (_, _, _, block) in enumerate(kept):
        blocks.append(block)
        if index in notes:
            blocks.append(notes[index])
    body = "\n".join(blocks) or "No earlier turns found in this member's current conversation."
    if len(body) > config.link_max_chars:  # one turn alone over the budget
        LOGGER.info("Earlier Discord result truncated to %d characters", config.link_max_chars)
    body = truncate(body, config.link_max_chars)
    return f'<RESULT kind="earlier" untrusted="true">\n{body}\n</RESULT>', images
