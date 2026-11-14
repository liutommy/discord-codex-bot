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
            [
                "https://vxtwitter.com/jack/status/20?s=20",
                "https://fixupx.com/jack/status/20?s=20",
            ],
        ),
        (
            "https://mobile.twitter.com/jack/status/20",
            ["https://vxtwitter.com/jack/status/20", "https://fixupx.com/jack/status/20"],
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
        (
            "https://www.facebook.com/FPCChannel/posts/1681075973388917",
            ["https://facebed.com/FPCChannel/posts/1681075973388917"],
        ),
        (
            "https://m.facebook.com/reel/2094282871197652",
            ["https://facebed.com/reel/2094282871197652"],
        ),
        (
            "https://www.facebook.com/share/r/1N2uyc1RzY/",
            ["https://facebed.com/share/r/1N2uyc1RzY/"],
        ),
        ("https://www.facebook.com/share/1ER9RCfJLi/", ["https://facebed.com/share/1ER9RCfJLi/"]),
        (
            "https://www.facebook.com/permalink.php?story_fbid=1&id=2",
            ["https://facebed.com/permalink.php?story_fbid=1&id=2"],
        ),
        (
            "https://web.facebook.com/groups/123/posts/456/?comment_id=7",
            ["https://facebed.com/groups/123/posts/456/?comment_id=7"],
        ),
        ("https://www.facebook.com/FPCChannel", []),  # a page, not a post
        ("https://www.facebook.com/watch/?v=1", []),  # not a facebed scheme
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
        "https://vxtwitter.com/u/status/1": '<meta property="og:title" content="x">',
        "https://fixupx.com/u/status/1": '<meta property="og:video" content="https://v/a.mp4">',
    }
    seen: list[str] = []

    async def fetch(url: str, headers: dict[str, str]) -> str | None:
        if url.startswith("https://api.vxtwitter.com/"):
            return '{"possibly_sensitive": false}'
        seen.append(url)
        assert headers.get("User-Agent", "").startswith("Mozilla/5.0 (compatible; Discordbot")
        return pages.get(url)

    assert await pick("https://x.com/u/status/1", fetch) == Fix(
        "https://fixupx.com/u/status/1", spoiler=False
    )
    assert seen == list(pages)  # the preferred proxy first, the fallback only after it fails
    assert await pick("https://x.com/u/status/2", fetch) is None  # both unreachable
    assert await pick("https://x.com/u", fetch) is None  # not a post: nothing fetched
    assert seen[-2:] == ["https://vxtwitter.com/u/status/2", "https://fixupx.com/u/status/2"]


async def test_x_sensitive_flag_comes_from_the_vxtwitter_api():
    answers = {
        "https://api.vxtwitter.com/u/status/1": '{"possibly_sensitive": true}',
        "https://api.vxtwitter.com/u/status/2": '{"possibly_sensitive": false}',
    }

    async def fetch(url: str, headers: dict[str, str]) -> str | None:
        if url.startswith("https://vxtwitter.com/"):  # the page, not the API host
            return MEDIA
        return answers.get(url)

    assert await rating("https://x.com/u/status/1?s=20", fetch) is True
    assert await rating("https://twitter.com/u/status/2", fetch) is False
    assert await rating("https://x.com/u/status/3", fetch) is None  # API down: no swap
    assert await pick("https://x.com/u/status/1", fetch) == Fix(
        "https://vxtwitter.com/u/status/1", spoiler=True
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


async def test_pick_swaps_a_facebook_post_but_not_the_login_wall():
    # facebed answers 200 for a post it cannot read, with Facebook's login wall: no media and
    # a fixed description (captured 2026-11-05 from facebed.com/share/p/ZZZZZZZZZZ/).
    wall = (
        '<meta property="og:title" content="Log in or sign up to view"/>'
        '<meta property="og:description" content="See posts, photos and more on Facebook."/>'
    )
    pages = {
        "https://facebed.com/reel/1": '<meta property="og:video" content="https://v/a.mp4"/>',
        "https://facebed.com/share/p/TEXT/": '<meta property="og:description" content="body"/>',
        "https://facebed.com/share/p/DEAD/": wall,
    }

    async def fetch(url: str, headers: dict[str, str]) -> str | None:
        return pages.get(url)

    assert await pick("https://www.facebook.com/reel/1", fetch) == Fix(
        "https://facebed.com/reel/1", spoiler=False
    )
    assert await pick("https://www.facebook.com/share/p/TEXT/", fetch) == Fix(
        "https://facebed.com/share/p/TEXT/", spoiler=False
    )
    assert await pick("https://www.facebook.com/share/p/DEAD/", fetch) is None


def _x_fetch(media: object, sensitive: bool = False, pages: dict[str, str] | None = None):
    """A fetch for one X post: the vxtwitter API answers with `media` as media_extended (an
    int is that many items), the proxy pages from `pages` (default: vxtwitter has a card)."""
    import json

    pages = {"https://vxtwitter.com/": MEDIA} if pages is None else pages
    body: dict[str, object] = {"possibly_sensitive": sensitive}
    if media is not None:
        body["media_extended"] = [{"type": "image"}] * media if isinstance(media, int) else media

    async def fetch(url: str, headers: dict[str, str]) -> str | None:
        if url.startswith("https://api.vxtwitter.com/"):
            return json.dumps(body)
        return next((page for prefix, page in pages.items() if url.startswith(prefix)), None)

    return fetch


def _pages(user: str, post: int, count: int) -> tuple[str, ...]:
    return tuple(f"https://vxtwitter.com/{user}/status/{post}/p{n}" for n in range(1, count + 1))


async def test_a_post_pages_only_past_ten_media():
    # Ten fit one vxtwitter card: today's single link. The eleventh opens page 2.
    assert await pick("https://x.com/u/status/1", _x_fetch(10)) == Fix(
        "https://vxtwitter.com/u/status/1", spoiler=False
    )
    p1, p2 = _pages("u", 1, 2)
    assert await pick("https://x.com/u/status/1", _x_fetch(11)) == Fix(p1, False, (p2,))
    assert await pick("https://x.com/u/status/1", _x_fetch(20)) == Fix(p1, False, (p2,))
    p1, p2, p3 = _pages("u", 1, 3)
    assert await pick("https://x.com/u/status/1", _x_fetch(21)) == Fix(p1, False, (p2, p3))


async def test_videos_and_gifs_count_toward_the_pages():
    mixed = [{"type": "video"}] * 4 + [{"type": "image"}] * 7 + [{"type": "gif"}]
    p1, p2 = _pages("u", 1, 2)
    assert await pick("https://x.com/u/status/1", _x_fetch(mixed)) == Fix(p1, False, (p2,))


async def test_an_unknown_media_count_keeps_the_single_link():
    single = Fix("https://vxtwitter.com/u/status/1", spoiler=False)
    assert await pick("https://x.com/u/status/1", _x_fetch(None)) == single  # field missing
    assert await pick("https://x.com/u/status/1", _x_fetch("many")) == single  # not a list


async def test_the_fixupx_fallback_and_paging_off_keep_one_link():
    fixupx = {"https://fixupx.com/": MEDIA}  # vxtwitter has no card: fixupx ignores /pN
    assert await pick("https://x.com/u/status/1", _x_fetch(30, pages=fixupx)) == Fix(
        "https://fixupx.com/u/status/1", spoiler=False
    )
    assert await pick("https://x.com/u/status/1", _x_fetch(30), paging=False) == Fix(
        "https://vxtwitter.com/u/status/1", spoiler=False
    )


async def test_page_links_come_from_the_plain_status_path_only():
    p1, p2 = _pages("u", 1, 2)
    # The query is not part of the page links.
    assert await pick("https://x.com/u/status/1?s=20", _x_fetch(15)) == Fix(p1, False, (p2,))
    assert await pick("https://twitter.com/u/status/1/", _x_fetch(15)) == Fix(p1, False, (p2,))
    # /photo/3 already selects one item: swapped as today, never paged.
    assert await pick("https://x.com/u/status/1/photo/3", _x_fetch(15)) == Fix(
        "https://vxtwitter.com/u/status/1/photo/3", spoiler=False
    )


async def test_a_sensitive_paged_post_keeps_its_spoiler_flag():
    p1, p2 = _pages("u", 1, 2)
    assert await pick("https://x.com/u/status/1", _x_fetch(12, sensitive=True)) == Fix(
        p1, True, (p2,)
    )


async def test_fill_adds_the_pages_a_member_link_lacks():
    p1, p2, p3 = _pages("u", 1, 3)
    for url in ("https://fixvx.com/u/status/1", "https://vxtwitter.com/u/status/1?s=20"):
        assert embedfix.fillable(url)
        assert await embedfix.fill(url, _x_fetch(25)) == Fix(url, False, (p2, p3))
    assert await embedfix.fill("https://fixvx.com/u/status/1", _x_fetch(25, True)) == Fix(
        "https://fixvx.com/u/status/1", True, (p2, p3)
    )
    # Nothing to add: one page of media, an unreadable count, a page or item already chosen,
    # a link that is not a vxtwitter / fixvx post.
    assert await embedfix.fill("https://fixvx.com/u/status/1", _x_fetch(10)) is None
    assert await embedfix.fill("https://fixvx.com/u/status/1", _x_fetch(None)) is None

    async def down(url: str, headers: dict[str, str]) -> str | None:
        return None

    assert await embedfix.fill("https://fixvx.com/u/status/1", down) is None
    for url in (
        "https://fixvx.com/u/status/1/p2",
        "https://fixvx.com/u/status/1/photo/3",
        "https://fixupx.com/u/status/1",
        "https://x.com/u/status/1",
    ):
        assert not embedfix.fillable(url)
        assert await embedfix.fill(url, _x_fetch(25)) is None
