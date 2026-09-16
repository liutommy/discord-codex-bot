from __future__ import annotations

import pytest

from discord_codex_bot import embedfix
from discord_codex_bot.embedfix import candidates, has_media, pick


@pytest.mark.parametrize(
    ("url", "expected"),
    [
        (
            "https://x.com/jack/status/20?s=20",
            ["https://fixupx.com/jack/status/20?s=20", "https://vxtwitter.com/jack/status/20?s=20"],
        ),
        (
            "https://mobile.twitter.com/jack/status/20",
            ["https://fixupx.com/jack/status/20", "https://vxtwitter.com/jack/status/20"],
        ),
        (
            "https://www.tiktok.com/@u/video/7654036719242726670",
            [
                "https://tnktok.com/@u/video/7654036719242726670",
                "https://tiktxk.com/@u/video/7654036719242726670",
            ],
        ),
        (
            "https://www.pixiv.net/en/artworks/149667269",
            ["https://phixiv.net/en/artworks/149667269"],
        ),
        (
            "https://www.tumblr.com/staff/811651663989538816/title",
            ["https://tpmblr.com/staff/811651663989538816/title"],
        ),
        ("https://x.com/jack", []),  # a profile, not a post
        ("https://fixupx.com/jack/status/20", []),  # already a proxy link
        ("https://www.tiktok.com/@tiktok", []),
        ("https://a.example/x/status/1", []),
        ("not a url", []),
    ],
)
def test_candidates_cover_posts_only_and_never_reproxy(url, expected):
    assert candidates(url) == expected


def test_has_media_reads_either_attribute_order_and_ignores_empty_tags():
    assert has_media('<meta property="og:video" content="https://v.example/a.mp4"/>')
    assert has_media('<meta content="https://i.example/a.jpg" property="twitter:image" />')
    assert has_media('<meta name="twitter:player:stream" content="https://v.example/a.mp4">')
    assert not has_media('<meta property="og:image" content="" />')
    assert not has_media(
        '<meta property="og:title" content="Notice" /><meta property="og:description" content="no longer available" />'
    )
    assert not has_media("")


async def test_pick_returns_the_first_candidate_with_media_or_none():
    pages = {
        "https://fixupx.com/u/status/1": '<meta property="og:title" content="x">',
        "https://vxtwitter.com/u/status/1": '<meta property="og:video" content="https://v/a.mp4">',
    }
    seen: list[str] = []

    async def fetch(url: str) -> str | None:
        seen.append(url)
        return pages.get(url)

    assert await pick("https://x.com/u/status/1", fetch) == "https://vxtwitter.com/u/status/1"
    assert seen == list(pages)
    assert await pick("https://x.com/u/status/2", fetch) is None  # both unreachable
    assert await pick("https://x.com/u", fetch) is None  # not a post: nothing fetched
    assert seen[-2:] == ["https://fixupx.com/u/status/2", "https://vxtwitter.com/u/status/2"]


def test_every_proxy_host_is_excluded_from_rewriting():
    for host in embedfix.PROXY_HOSTS:
        assert candidates(f"https://{host}/u/status/1") == []
