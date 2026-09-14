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
    "你是影片理解工具。用繁體中文整理這段影片，作為另一個助理回答問題的背景資料。"
    "輸出格式：先一句總結，再依時間順序分段（每段標大約的時間點），每段寫畫面發生什麼、人物與動作、"
    "畫面上的文字、對白或旁白重點；最後列出關鍵名詞（人名、作品名、卡名、產品名等原文）。"
    "只輸出整理結果本身：不要寫思考過程、不要自問自答、不要評論、不要回答問題。"
)
RETRY_STATUSES = (429, 500, 503)  # transient on the free tier; one retry after a short pause


@dataclass(frozen=True, slots=True)
class VideoResult:
    text: str
    source: str  # "gemini-video" (native understanding) or "youtube-captions" (transcript only)


def available(config: Config) -> bool:
    return bool(config.gemini_api_key)


def _clip(text: str, limit: int) -> str:
    text = text.strip()
    return text if len(text) <= limit else f"{text[:limit]}…"


async def _post(config: Config, model: str, body: dict) -> tuple[int, dict]:
    url = f"{API}/models/{model}:generateContent?key={config.gemini_api_key}"
    timeout = aiohttp.ClientTimeout(total=config.gemini_timeout_seconds)
    async with aiohttp.ClientSession(timeout=timeout) as session:
        async with session.post(url, json=body) as response:
            return response.status, await response.json(content_type=None)


async def _generate(config: Config, parts: list[dict]) -> str | None:
    """One generateContent call across the model chain (GEMINI_MODEL, then GEMINI_FALLBACK_MODEL),
    retrying a transient status once; the text, or None on any error/refusal so the caller can
    fall back further. Gemini is a fixed Google host, so no SSRF guard is needed here."""
    body = {
        "contents": [{"parts": parts}],
        "generationConfig": {"maxOutputTokens": 2000, "temperature": 0.2},
    }
    models = [m for m in (config.gemini_model, config.gemini_fallback_model) if m]
    payload: dict = {}
    for model in dict.fromkeys(models):
        for attempt in (1, 2):
            try:
                status, payload = await _post(config, model, body)
            except (aiohttp.ClientError, TimeoutError, ValueError) as error:
                LOGGER.warning("Gemini %s request failed: %s", model, type(error).__name__)
                status, payload = 0, {}
            if status == 200:
                break
            message = (payload.get("error") or {}).get("message", "") if payload else ""
            LOGGER.warning("Gemini %s HTTP %s: %s", model, status, message[:160])
            if status not in RETRY_STATUSES or attempt == 2:
                break
            await asyncio.sleep(2)
        if payload.get("candidates"):
            break
    else:
        return None
    if not payload.get("candidates"):
        return None
    candidates = payload.get("candidates") or []
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
