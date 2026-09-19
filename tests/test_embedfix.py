from __future__ import annotations

import pytest

from discord_codex_bot import embedfix
from discord_codex_bot.embedfix import (
    PROXIES,
    Fix,
    candidates,
    has_card,
    has_media,
    pick,
    rating,
)


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
            "https://www.threads.com/@mosseri/post/DDupwppSjcp",
            ["https://vxthreads.com/@mosseri/post/DDupwppSjcp"],
        ),
        (
            "https://www.threads.net/@mosseri/post/DDupwppSjcp",
            ["https://vxthreads.com/@mosseri/post/DDupwppSjcp"],
        ),
        ("https://www.threads.com/@mosseri", []),
        (
            "https://www.threads.com/share/BAXXXUHcT6/",
            ["https://vxthreads.com/share/BAXXXUHcT6/"],
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
    notice = '<meta property="og:title" content="Notice" />'
    assert not has_media(notice + '<meta property="og:description" content="gone" />')
    assert not has_media("")


def test_a_text_post_is_a_card_only_where_the_rule_says_so_and_never_the_placeholder():
    # vxthreads answers 200 with a generic card for a share code it cannot resolve, so the
    # status code cannot tell a real text post from a dead link -- only the description can.
    threads = next(rule for rule in PROXIES if "threads.com" in rule.hosts)
    x = next(rule for rule in PROXIES if "x.com" in rule.hosts)
    text = '<meta property="og:description" content="post body" />'
    placeholder = '<meta property="og:description" content="View this post on Threads." />'
    assert has_card(text, threads)
    assert not has_card(placeholder, threads)
    assert not has_card('<meta property="og:description" content="  " />', threads)
    assert not has_card("", threads)
    # Sites whose rule does not opt in keep the stricter media test.
    assert not has_card(text, x)
    assert has_card('<meta property="og:image" content="https://i/a.jpg">', x)


async def test_pick_swaps_a_threads_text_post_but_not_an_unresolved_share_code():
    pages = {
        "https://vxthreads.com/share/REAL/": '<meta property="og:description" content="body" />',
        "https://vxthreads.com/share/DEAD/": (
            '<meta property="og:title" content="Threads (@threads)" />'
            '<meta property="og:description" content="View this post on Threads." />'
        ),
    }

    async def fetch(url: str, headers: dict[str, str]) -> str | None:
        return pages.get(url)

    assert await pick("https://threads.com/share/REAL/", fetch) == Fix(
        "https://vxthreads.com/share/REAL/", spoiler=False
    )
    assert await pick("https://threads.com/share/DEAD/", fetch) is None


MEDIA = '<meta property="og:image" content="https://i/a.jpg">'


async def test_pick_returns_the_first_candidate_with_media_or_none():
    pages = {
        "https://fixupx.com/u/status/1": '<meta property="og:title" content="x">',
        "https://vxtwitter.com/u/status/1": '<meta property="og:video" content="https://v/a.mp4">',
    }
    seen: list[str] = []

    async def fetch(url: str, headers: dict[str, str]) -> str | None:
        if url.startswith("https://api.vxtwitter.com/"):
            return '{"possibly_sensitive": false}'
        seen.append(url)
        assert headers.get("User-Agent", "").startswith("Mozilla/5.0 (compatible; Discordbot")
        return pages.get(url)

    assert await pick("https://x.com/u/status/1", fetch) == Fix(
        "https://vxtwitter.com/u/status/1", spoiler=False
    )
    assert seen == list(pages)
    assert await pick("https://x.com/u/status/2", fetch) is None  # both unreachable
    assert await pick("https://x.com/u", fetch) is None  # not a post: nothing fetched
    assert seen[-2:] == ["https://fixupx.com/u/status/2", "https://vxtwitter.com/u/status/2"]


async def test_x_sensitive_flag_comes_from_the_vxtwitter_api():
    answers = {
        "https://api.vxtwitter.com/u/status/1": '{"possibly_sensitive": true}',
        "https://api.vxtwitter.com/u/status/2": '{"possibly_sensitive": false}',
    }

    async def fetch(url: str, headers: dict[str, str]) -> str | None:
        if url.startswith("https://fixupx.com/"):
            return MEDIA
        return answers.get(url)

    assert await rating("https://x.com/u/status/1?s=20", fetch) is True
    assert await rating("https://twitter.com/u/status/2", fetch) is False
    assert await rating("https://x.com/u/status/3", fetch) is None  # API down: no swap
    assert await pick("https://x.com/u/status/1", fetch) == Fix(
        "https://fixupx.com/u/status/1", spoiler=True
    )
    assert await pick("https://x.com/u/status/3", fetch) is None


async def test_pixiv_rating_drives_the_spoiler_and_an_unreadable_rating_blocks_the_swap():
    answers = {
        "https://www.pixiv.net/ajax/illust/1": '{"error": false, "body": {"xRestrict": 1}}',
        "https://www.pixiv.net/ajax/illust/2": '{"error": false, "body": {"xRestrict": 0}}',
        "https://www.pixiv.net/ajax/illust/3": "<html>login wall</html>",
    }

    async def fetch(url: str, headers: dict[str, str]) -> str | None:
        if "phixiv.net" in url:
            return MEDIA
        assert headers == {"Referer": "https://www.pixiv.net/"}
        return answers.get(url)

    assert await rating("https://www.pixiv.net/artworks/1", fetch) is True
    assert await rating("https://www.pixiv.net/en/artworks/2", fetch) is False
    assert await rating("https://www.pixiv.net/artworks/3", fetch) is None
    assert await rating("https://www.tiktok.com/@u/video/1", fetch) is False  # no rating
    assert await pick("https://www.pixiv.net/artworks/1", fetch) == Fix(
        "https://phixiv.net/artworks/1", spoiler=True
    )
    assert await pick("https://www.pixiv.net/artworks/2", fetch) == Fix(
        "https://phixiv.net/artworks/2", spoiler=False
    )
    assert await pick("https://www.pixiv.net/artworks/3", fetch) is None


def test_every_proxy_host_is_excluded_from_rewriting():
    for host in embedfix.PROXY_HOSTS:
        assert candidates(f"https://{host}/u/status/1") == []
