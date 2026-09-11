from __future__ import annotations

import asyncio
import logging
import shutil
import tempfile
import time
from pathlib import Path

import discord

from .config import Config

LOGGER = logging.getLogger(__name__)
REQUEST_DIR_PREFIX = "req-"
IMAGE_SUFFIXES = {
    "image/png": ".png",
    "image/jpeg": ".jpg",
    "image/webp": ".webp",
    "image/gif": ".gif",
}


def validate_image(content_type: str | None, size: int, config: Config) -> str:
    """Return the file suffix for an acceptable image, or a user-facing rejection reason."""
    media_type = (content_type or "").split(";")[0].strip().lower()
    if media_type not in IMAGE_SUFFIXES:
        return f"只接受 {', '.join(sorted(IMAGE_SUFFIXES))} 圖片。"
    if size > config.max_attachment_bytes:
        return f"圖片必須小於 {config.max_attachment_bytes // 1_000_000} MB。"
    return IMAGE_SUFFIXES[media_type]


async def download_image(attachment: discord.Attachment, suffix: str, config: Config) -> Path:
    """Save the attachment into a fresh per-request directory; the caller removes it."""
    config.attachment_dir.mkdir(parents=True, exist_ok=True)
    request_dir = Path(tempfile.mkdtemp(prefix=REQUEST_DIR_PREFIX, dir=config.attachment_dir))
    path = request_dir / f"image{suffix}"
    await attachment.save(path)
    return path


def remove_request_dir(path: Path) -> None:
    shutil.rmtree(path.parent, ignore_errors=True)


def sweep_stale(root: Path, max_age_seconds: float, now: float | None = None) -> int:
    """Delete request directories older than max_age_seconds (crash leftovers); return count."""
    if not root.is_dir():
        return 0
    cutoff = (time.time() if now is None else now) - max_age_seconds
    removed = 0
    for entry in root.iterdir():
        if entry.name.startswith(REQUEST_DIR_PREFIX) and entry.stat().st_mtime < cutoff:
            shutil.rmtree(entry, ignore_errors=True)
            removed += 1
    return removed


async def sweep_forever(config: Config) -> None:
    interval = config.attachment_sweep_minutes * 60
    # Anything older than one request timeout plus one sweep interval cannot belong to a live job.
    max_age = config.codex_timeout_seconds + interval
    while True:
        removed = sweep_stale(config.attachment_dir, max_age)
        if removed:
            LOGGER.info("Swept %d stale attachment directories", removed)
        await asyncio.sleep(interval)
