from __future__ import annotations

import sys
from dataclasses import replace
from pathlib import Path
from types import ModuleType, SimpleNamespace

import pytest

from discord_codex_bot import gemini, links
from discord_codex_bot.config import Config


class Response:
    def __init__(self, payload, status: int = 200) -> None:
        self.payload, self.status = payload, status

    async def json(self, content_type=None):
        return self.payload

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        return False


class Session:
    last_body: dict | None = None

    def __init__(self, response: Response) -> None:
        self.response = response

    def post(self, url: str, json=None, **kwargs):
        Session.last_body = json
        self.url = url
        return self.response

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        return False


def _install(monkeypatch, response: Response) -> None:
    monkeypatch.setattr(gemini.aiohttp, "ClientSession", lambda **kw: Session(response))


def _candidate(text: str, finish: str = "STOP") -> dict:
    return {"candidates": [{"finishReason": finish, "content": {"parts": [{"text": text}]}}]}


def test_available_follows_the_key(config: Config) -> None:
    assert gemini.available(config)
    assert not gemini.available(replace(config, gemini_api_key=""))


async def test_describe_youtube_url_sends_the_uri_and_returns_text(monkeypatch, config) -> None:
    _install(monkeypatch, Response(_candidate("公園散步影片")))
    out = await gemini.describe_youtube_url("https://youtu.be/abcdefghijk", config)
    assert out == "公園散步影片"
    part = Session.last_body["contents"][0]["parts"][0]
    assert part["file_data"]["file_uri"] == "https://youtu.be/abcdefghijk"


async def test_describe_youtube_url_returns_none_on_error_or_safety(monkeypatch, config) -> None:
    _install(monkeypatch, Response({"error": {"message": "high demand"}}, status=503))
    assert await gemini.describe_youtube_url("https://youtu.be/x", config) is None
    _install(monkeypatch, Response(_candidate("擋", finish="SAFETY")))
    assert await gemini.describe_youtube_url("https://youtu.be/x", config) is None
    _install(monkeypatch, Response({"candidates": []}))
    assert await gemini.describe_youtube_url("https://youtu.be/x", config) is None


async def test_describe_video_bytes_inlines_and_enforces_the_size_cap(
    monkeypatch, config, tmp_path: Path
) -> None:
    clip = tmp_path / "c.mp4"
    clip.write_bytes(b"MP4DATA")
    _install(monkeypatch, Response(_candidate("有人在講話")))
    assert await gemini.describe_video_bytes(clip, config) == "有人在講話"
    part = Session.last_body["contents"][0]["parts"][0]
    assert part["inline_data"]["mime_type"] == "video/mp4"
    small = replace(config, gemini_video_inline_max_bytes=3)
    assert await gemini.describe_video_bytes(clip, small) is None  # over cap: not sent
    assert await gemini.describe_video_bytes(tmp_path / "missing.mp4", config) is None


async def test_describe_clips_to_the_char_limit(monkeypatch, config) -> None:
    _install(monkeypatch, Response(_candidate("字" * 50)))
    out = await gemini.describe_youtube_url("https://youtu.be/x", replace(config, gemini_video_max_chars=10))
    assert out == "字" * 10 + "…"


async def test_youtube_transcript_joins_snippets_and_handles_absence(monkeypatch, config) -> None:
    module = ModuleType("youtube_transcript_api")

    class FakeApi:
        raise_it = False

        def fetch(self, video_id, languages):
            if FakeApi.raise_it:
                raise RuntimeError("no transcript")
            return [SimpleNamespace(text="一句"), SimpleNamespace(text="兩句")]

    module.YouTubeTranscriptApi = FakeApi
    monkeypatch.setitem(sys.modules, "youtube_transcript_api", module)
    assert await gemini.youtube_transcript("vid", config) == "一句 兩句"
    FakeApi.raise_it = True
    assert await gemini.youtube_transcript("vid", config) is None


@pytest.mark.parametrize(
    ("url", "expected"),
    [
        ("https://www.youtube.com/watch?v=dQw4w9WgXcQ", "dQw4w9WgXcQ"),
        ("https://youtu.be/dQw4w9WgXcQ?t=30", "dQw4w9WgXcQ"),
        ("https://www.youtube.com/shorts/abcdefghijk", "abcdefghijk"),
        ("https://m.youtube.com/watch?v=abcdefghijk&list=x", "abcdefghijk"),
        ("https://www.youtube.com/watch?v=short", ""),
        ("https://vimeo.com/12345", ""),
    ],
)
def test_youtube_id_parses_every_url_shape(url: str, expected: str) -> None:
    assert links.youtube_id(url) == expected


def test_has_video_covers_youtube_and_x_only() -> None:
    assert links.has_video("https://youtu.be/dQw4w9WgXcQ")
    assert links.has_video("https://x.com/a/status/12345")
    assert not links.has_video("https://example.com/page")


async def test_understand_video_youtube_prefers_gemini_then_captions(monkeypatch, config) -> None:
    async def gemini_ok(url, cfg):
        return "看到公園"

    async def gemini_none(url, cfg):
        return None

    async def captions(video_id, cfg):
        return "字幕內容"

    monkeypatch.setattr(links.gemini, "describe_youtube_url", gemini_ok)
    monkeypatch.setattr(links.gemini, "youtube_transcript", captions)
    url = "https://youtu.be/dQw4w9WgXcQ"
    assert await links.understand_video(url, config, None) == f"{links.VIDEO_LABEL}看到公園"
    monkeypatch.setattr(links.gemini, "describe_youtube_url", gemini_none)
    assert await links.understand_video(url, config, None) == f"{links.CAPTION_LABEL}字幕內容"

    async def no_captions(video_id, cfg):
        return None

    monkeypatch.setattr(links.gemini, "youtube_transcript", no_captions)
    assert await links.understand_video(url, config, None) is None


async def test_understand_video_x_downloads_the_clip_and_describes_it(
    monkeypatch, config, tmp_path: Path
) -> None:
    async def video_url(url, cfg):
        return "https://video.twimg.com/v.mp4"

    async def download(session, url, path, cfg):
        path.write_bytes(b"clip")
        return path

    async def describe(path, cfg):
        return "影片裡有貓"

    class FakeSession:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            return False

    monkeypatch.setattr(links, "x_video_url", video_url)
    monkeypatch.setattr(links, "_guarded_session", lambda cfg: FakeSession())
    monkeypatch.setattr(links, "_download_video", download)
    monkeypatch.setattr(links.gemini, "describe_video_bytes", describe)
    out = await links.understand_video("https://x.com/a/status/1", config, tmp_path)
    assert out == f"{links.VIDEO_LABEL}影片裡有貓"

    async def no_url(url, cfg):
        return ""

    monkeypatch.setattr(links, "x_video_url", no_url)
    assert await links.understand_video("https://x.com/a/status/1", config, tmp_path) is None
