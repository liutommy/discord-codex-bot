from __future__ import annotations

import asyncio
import base64
import logging
import mimetypes
from dataclasses import dataclass
from pathlib import Path

import aiohttp

from .config import Config

LOGGER = logging.getLogger(__name__)
API = "https://generativelanguage.googleapis.com/v1beta"
# Free-tier Gemini reads video (frames + audio) natively; used as a tool, not a chat backend —
# the description it returns is injected into the prompt so every backend model can answer about
# the clip. A YouTube URL is understood without downloading; other sources are sent inline.
_INSTRUCTION = (
    "你是影片理解工具。用繁體中文描述這段影片，作為另一個助理回答問題的背景資料：畫面發生什麼、"
    "有哪些人物與動作、畫面上出現的文字、以及聽得到的對白或旁白重點。只客觀描述、不要評論或回答問題。"
)


@dataclass(frozen=True, slots=True)
class VideoResult:
    text: str
    source: str  # "gemini-video" (native understanding) or "youtube-captions" (transcript only)


def available(config: Config) -> bool:
    return bool(config.gemini_api_key)


def _clip(text: str, limit: int) -> str:
    text = text.strip()
    return text if len(text) <= limit else f"{text[:limit]}…"


async def _generate(config: Config, parts: list[dict]) -> str | None:
    """One generateContent call; the model's text, or None on any error/refusal so the caller
    can fall back. Gemini is a fixed Google host, so no SSRF guard is needed here."""
    body = {
        "contents": [{"parts": parts}],
        "generationConfig": {"maxOutputTokens": 700, "temperature": 0.2},
    }
    url = f"{API}/models/{config.gemini_model}:generateContent?key={config.gemini_api_key}"
    timeout = aiohttp.ClientTimeout(total=config.gemini_timeout_seconds)
    try:
        async with aiohttp.ClientSession(timeout=timeout) as session:
            async with session.post(url, json=body) as response:
                payload = await response.json(content_type=None)
                if response.status != 200:
                    message = (payload.get("error") or {}).get("message", "")
                    LOGGER.warning("Gemini HTTP %s: %s", response.status, message[:160])
                    return None
    except (aiohttp.ClientError, TimeoutError, ValueError) as error:
        LOGGER.warning("Gemini request failed: %s", type(error).__name__)
        return None
    candidates = payload.get("candidates") or []
    if not candidates:
        LOGGER.warning("Gemini returned no candidate (%s)", payload.get("promptFeedback"))
        return None
    reason = candidates[0].get("finishReason")
    if reason and reason not in ("STOP", "MAX_TOKENS"):
        LOGGER.warning("Gemini stopped early: %s", reason)  # SAFETY / RECITATION → fall back
        return None
    text = "".join(
        part.get("text", "") for part in (candidates[0].get("content") or {}).get("parts") or []
    )
    return text.strip() or None


async def describe_youtube_url(url: str, config: Config) -> str | None:
    """Understand a YouTube video from its URL alone (Gemini fetches it; long videos are fine)."""
    parts = [{"file_data": {"file_uri": url}}, {"text": _INSTRUCTION}]
    text = await _generate(config, parts)
    return _clip(text, config.gemini_video_max_chars) if text else None


async def describe_video_bytes(path: Path, config: Config) -> str | None:
    """Understand a downloaded clip sent inline (base64). The caller keeps the file under the
    inline size cap; larger clips are not sent (Gemini's request limit, not a licence to hang)."""
    try:
        raw = await asyncio.to_thread(path.read_bytes)
    except OSError:
        return None
    if len(raw) > config.gemini_video_inline_max_bytes:
        LOGGER.info("clip %s over inline cap (%d bytes); skipped", path.name, len(raw))
        return None
    encoded = base64.b64encode(raw).decode("ascii")
    mime = mimetypes.guess_type(path.name)[0] or "video/mp4"
    if not mime.startswith("video/"):
        mime = "video/mp4"
    parts = [
        {"inline_data": {"mime_type": mime, "data": encoded}},
        {"text": _INSTRUCTION},
    ]
    text = await _generate(config, parts)
    return _clip(text, config.gemini_video_max_chars) if text else None


async def youtube_transcript(video_id: str, config: Config) -> str | None:
    """The video's captions as plain text (no visuals); the cheap fallback when Gemini cannot
    watch it. Returns None when the clip has no captions or the library is unavailable."""
    def fetch() -> str | None:
        try:
            from youtube_transcript_api import YouTubeTranscriptApi
        except ImportError:
            return None
        try:
            fetched = YouTubeTranscriptApi().fetch(
                video_id, languages=["zh-Hant", "zh-Hans", "zh", "en", "ja"]
            )
        except Exception as error:  # the library raises many per-video reasons; all mean "no"
            LOGGER.info("no transcript for %s: %s", video_id, type(error).__name__)
            return None
        return " ".join(snippet.text for snippet in fetched).strip() or None

    text = await asyncio.to_thread(fetch)
    return _clip(text, config.gemini_video_max_chars) if text else None
