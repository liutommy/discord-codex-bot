"""Every action tag is acted on only where the answer writes it, never where it only shows it:
in Discord code or a quote, a tag is an example or someone else's words (a member's message can
carry any tag, and an answer may quote it)."""

from __future__ import annotations

import pytest

from discord_codex_bot import apis, links, sandbox, search
from discord_codex_bot.memory import extract_memory_tags, extract_read_requests
from discord_codex_bot.tags import findall, strip
from discord_codex_bot.tracking import CANCEL_TRACK_TAG, extract_track_tags

MEMORY = '<memory scope="user" name="住">住台中</memory>'
SEARCH = '<search scope="user" query="住"/>'
RECALL = '<recall scope="guild" name="規則"/>'
TRACK = '<track source="https://example.com/news" interest="新聞"/>'
TRACK_EVERY = '<track_every id="3" minutes="60"/>'
TRACK_AT = '<track_at id="3" times="09:00"/>'
CANCEL_TRACK = '<cancel_track id="4"/>'
FETCH = '<fetch url="https://example.com/"/>'
WEB = '<web query="台北 天氣"/>'
RUN = '<run lang="python">print(1)</run>'
API = '<api name="lol" path="getLeagues"/>'


def shown(tag: str) -> list[str]:
    return [
        f"例：`{tag}`",
        f"```\n{tag}\n```",
        f"```\n{tag}",  # a block never closed runs to the end
        f"> {tag}",
        f">>> 他說：\nhi\n{tag}",
        f"`a\n{tag}\nb`",
    ]


@pytest.mark.parametrize(
    "tag, acted_on",
    [
        (MEMORY, lambda text: extract_memory_tags(text)[1]),
        (SEARCH, extract_read_requests),
        (RECALL, extract_read_requests),
        (TRACK, lambda text: extract_track_tags(text)[1]),
        (TRACK_EVERY, lambda text: extract_track_tags(text)[2]),
        (TRACK_AT, lambda text: extract_track_tags(text)[3]),
        (CANCEL_TRACK, lambda text: extract_track_tags(text)[4]),
        (FETCH, links.extract_fetch_tags),
        (WEB, search.extract_web_queries),
        (RUN, sandbox.extract_runs),
        (API, apis.extract_api_calls),
    ],
)
def test_a_tag_the_answer_only_shows_is_not_acted_on(tag, acted_on) -> None:
    assert acted_on(f"好\n{tag}")  # written: acted on
    for text in shown(tag):
        assert not acted_on(text), text


def test_a_shown_tag_stays_in_the_text_as_written() -> None:
    for text in shown(MEMORY):
        assert extract_memory_tags(text)[0] == text.strip()
    for text in shown(CANCEL_TRACK):
        assert extract_track_tags(text)[0] == text.strip()
    assert extract_memory_tags(f"記住了{MEMORY}")[0] == "記住了"


def test_findall_and_strip_keep_the_pattern_shapes() -> None:
    text = f"{CANCEL_TRACK} 與 `{CANCEL_TRACK}`"
    assert findall(CANCEL_TRACK_TAG, text) == ["4"]
    assert strip(CANCEL_TRACK_TAG, text) == f" 與 `{CANCEL_TRACK}`"
