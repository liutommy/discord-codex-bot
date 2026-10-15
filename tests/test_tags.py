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


def test_what_a_run_body_holds_is_code_not_a_request() -> None:
    """A member's text the model wraps in a script is still the member's text."""
    body = '<run lang="python">s = """<api name="lol" path="x"/> <fetch url="https://e.com/"/>"""</run>'
    assert apis.extract_api_calls(body) == [] and links.extract_fetch_tags(body) == []
    assert len(sandbox.extract_runs(body)) == 1


def test_backticks_inside_a_tag_do_not_hide_the_tags_after_it() -> None:
    two = '<run lang="python">x = "```"</run><run lang="sh">echo hi</run>'
    assert [lang for lang, _ in sandbox.extract_runs(two)] == ["python", "sh"]
    later = '<run lang="python">print("```py")</run>\n<web query="later"/>'
    assert search.extract_web_queries(later) == ["later"]
    odd = '<memory scope="user" name="a">it`s</memory> 然後 `x` 再 ' + WEB
    assert search.extract_web_queries(odd) == ["台北 天氣"]


def test_quotes_are_what_discord_quotes() -> None:
    assert search.extract_web_queries(f">_< {WEB}") == ["台北 天氣"]  # a face, not a quote
    assert search.extract_web_queries(f"- > {WEB}") == []  # a quote inside a list item


def test_a_shown_real_tag_is_not_reported_as_invented() -> None:
    from discord_codex_bot.bot import strip_invented_tags

    text = f"用 `{CANCEL_TRACK}` 取消"
    assert strip_invented_tags(text) == (text, [])
    assert strip_invented_tags('好<make_coffee size="L"/>') == ("好", ["make_coffee"])


def test_many_tags_in_a_long_answer_stay_cheap() -> None:
    import time

    text = ("`x` " + WEB + " 文字 " * 50 + "\n") * 6000
    started = time.monotonic()
    assert len(search.extract_web_queries(text)) == 1  # one distinct query
    assert time.monotonic() - started < 1.0
