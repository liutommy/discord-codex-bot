from __future__ import annotations

import asyncio
import logging
import shutil
import tempfile
import time
from collections.abc import Collection
from pathlib import Path

import discord

from .config import Config

LOGGER = logging.getLogger(__name__)
REQUEST_DIR_PREFIX = "req-"
# Request directories a request in this process still uses. The sweep's age limit cannot tell a
# crash leftover from a request queued behind others for longer than that; this set can.
LIVE: set[str] = set()
IMAGE_SUFFIXES = {
    "image/png": ".png",
    "image/jpeg": ".jpg",
    "image/webp": ".webp",
    "image/gif": ".gif",
}


# Documents the Bot reads as text for the model (untrusted, bounded). Anything else is refused.
DOCUMENT_TYPES = {
    "application/pdf": ".pdf",
    "application/json": ".json",
    "application/xml": ".xml",
    "application/x-yaml": ".yaml",
    "application/toml": ".toml",
    "application/javascript": ".js",
}
CODE_SUFFIXES = {
    ".txt",
    ".md",
    ".csv",
    ".log",
    ".json",
    ".yaml",
    ".yml",
    ".toml",
    ".xml",
    ".html",
    ".css",
    ".py",
    ".js",
    ".ts",
    ".tsx",
    ".jsx",
    ".go",
    ".rs",
    ".java",
    ".kt",
    ".c",
    ".h",
    ".cpp",
    ".hpp",
    ".cs",
    ".rb",
    ".php",
    ".sh",
    ".sql",
    ".lua",
    ".swift",
    ".dart",
    ".r",
    ".ini",
    ".cfg",
    ".env",
}
ACCEPTED_DOCUMENTS = "PDF、純文字／Markdown／CSV、JSON／YAML／TOML／XML、程式碼檔"


def validate_attachment(
    content_type: str | None, filename: str, size: int, config: Config
) -> tuple[str, str]:
    """("image", suffix) | ("document", suffix) | ("", user-facing rejection reason)."""
    media_type = (content_type or "").split(";")[0].strip().lower()
    suffix = Path(filename or "").suffix.lower()
    if media_type in IMAGE_SUFFIXES:
        kind, chosen = "image", IMAGE_SUFFIXES[media_type]
    elif media_type in DOCUMENT_TYPES or media_type.startswith("text/") or suffix in CODE_SUFFIXES:
        kind, chosen = "document", DOCUMENT_TYPES.get(media_type) or suffix or ".txt"
    else:
        images = ", ".join(sorted(IMAGE_SUFFIXES))
        return "", f"只接受圖片（{images}）或文件（{ACCEPTED_DOCUMENTS}）。"
    if size > config.max_attachment_bytes:
        return "", f"附件必須小於 {config.max_attachment_bytes // 1_000_000} MB。"
    return kind, chosen


def validate_image(content_type: str | None, size: int, config: Config) -> str:
    """Return the file suffix for an acceptable image, or a user-facing rejection reason."""
    media_type = (content_type or "").split(";")[0].strip().lower()
    if media_type not in IMAGE_SUFFIXES:
        return f"只接受 {', '.join(sorted(IMAGE_SUFFIXES))} 圖片。"
    if size > config.max_attachment_bytes:
        return f"圖片必須小於 {config.max_attachment_bytes // 1_000_000} MB。"
    return IMAGE_SUFFIXES[media_type]


async def download_attachment(
    attachment: discord.Attachment, suffix: str, config: Config, stem: str = "image"
) -> Path:
    """Save the attachment into a fresh per-request directory; the caller removes it."""
    path = request_dir(config) / f"{stem}{suffix}"
    try:
        await attachment.save(path)
    except BaseException:
        remove_dir(path.parent)
        raise
    return path


async def download_image(attachment: discord.Attachment, suffix: str, config: Config) -> Path:
    return await download_attachment(attachment, suffix, config)


def extract_text(path: Path, max_chars: int, max_pdf_pages: int = 50) -> str:
    """A document's text for the prompt, bounded. PDF via pypdf (page-limited); anything else is
    decoded as UTF-8 and refused when it looks binary."""
    if path.suffix.lower() == ".pdf":
        try:
            from pypdf import PdfReader

            reader = PdfReader(str(path))
            pages = [page.extract_text() or "" for page in reader.pages[:max_pdf_pages]]
            text = "\n\n".join(p.strip() for p in pages if p.strip())
            if len(reader.pages) > max_pdf_pages:
                text += f"\n[只讀了前 {max_pdf_pages} 頁，共 {len(reader.pages)} 頁]"
        except Exception as error:  # encrypted, malformed, or not a PDF at all
            LOGGER.info("pdf %s unreadable: %s", path.name, type(error).__name__)
            return "（PDF 讀不出文字：可能是掃描圖檔或加密）"
    else:
        raw = path.read_bytes()
        if b"\x00" in raw[:4096]:
            return "（這不是文字檔，讀不出內容）"
        text = raw.decode("utf-8", errors="replace").strip()
    if not text:
        return "（檔案沒有可讀文字）"
    return text if len(text) <= max_chars else f"{text[:max_chars]}\n[已截斷至 {max_chars} 字]"


def request_dir(config: Config) -> Path:
    """A fresh per-request directory, kept from the sweep until removed with remove_dir."""
    config.attachment_dir.mkdir(parents=True, exist_ok=True)
    path = Path(tempfile.mkdtemp(prefix=REQUEST_DIR_PREFIX, dir=config.attachment_dir))
    LIVE.add(path.name)
    return path


def remove_request_dir(path: Path) -> None:
    remove_dir(path.parent)


def remove_dir(path: Path | None) -> None:
    if path is not None:
        LIVE.discard(path.name)
        shutil.rmtree(path, ignore_errors=True)


def generated_images_root(config: Config) -> Path:
    return config.codex_home / "generated_images"


def sweep_stale(
    root: Path,
    max_age_seconds: float,
    now: float | None = None,
    prefix: str = "",
    keep: Collection[str] = (),
) -> int:
    """Delete subdirectories older than max_age_seconds (crash leftovers), except those named in
    `keep`; return count."""
    if not root.is_dir():
        return 0
    cutoff = (time.time() if now is None else now) - max_age_seconds
    removed = 0
    for entry in root.iterdir():
        if entry.name in keep:
            continue
        if entry.is_dir() and entry.name.startswith(prefix) and entry.stat().st_mtime < cutoff:
            shutil.rmtree(entry, ignore_errors=True)
            removed += 1
    return removed


async def sweep_forever(config: Config) -> None:
    interval = config.attachment_sweep_minutes * 60
    # Anything older than one request timeout plus one sweep interval cannot belong to a live job.
    longest = max(
        config.codex_timeout_seconds,
        config.local_llm_timeout_seconds,
        config.image_gen_timeout_seconds,
    )
    max_age = longest + interval
    while True:
        removed = sweep_stale(config.attachment_dir, max_age, prefix=REQUEST_DIR_PREFIX, keep=LIVE)
        removed += sweep_stale(generated_images_root(config), max_age)
        if removed:
            LOGGER.info("Swept %d stale attachment/generated-image directories", removed)
        await asyncio.sleep(interval)
