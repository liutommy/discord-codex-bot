"""The Bot's own tags in an answer, minus the ones the answer only shows.

Text an answer shows rather than does: Discord code (``` blocks, a block not closed yet running
to the end, `inline` spans, which may cross lines), quotes (a "> " line, also inside a list item;
">>> " quotes the rest of the message) and the body of a <run> script. A tag in there is an
example, or someone else's words (a member's message can carry any tag, and an answer may quote
it), never a request. Every parser of an action tag goes through here.

This is a heuristic over Discord's rendering, not a boundary: a member's words the model repeats
in plain prose or 「」 still read as its own.
"""

from __future__ import annotations

import bisect
import itertools
import re

SHOWN = re.compile(
    r"```[\s\S]*?(?:```|\Z)|``(?:(?!``)[\s\S])+?``|`[^`]+`"
    r"|^[ \t]*>>>[ \t][\s\S]*|^[ \t]*(?:[-*+][ \t]+|\d+\.[ \t]+)?>[ \t].*$",
    re.M,
)
# Any tag in the Bot's vocabulary, with its body where it has one. Blanked out before SHOWN is
# worked out, so a backtick inside a tag (a script's string, a remembered "it`s") neither opens
# nor closes code around the rest of the answer.
_TAG = re.compile(
    r"<(run|memory)\b[^>]*>[\s\S]*?</\1>"
    r'|<[a-z_]{2,20}(?:\s+[a-z_]{1,12}="[^"]*")*\s*/?>(?:\s*</[a-z_]{2,20}>)?'
)
_RUN_BODY = re.compile(r"<run\b[^>]*>([\s\S]*?)</run>")


def _blank(match: re.Match[str]) -> str:
    return re.sub(r"[^\n]", " ", match.group(0))


def _shown(text: str) -> list[tuple[int, int]]:
    spans = [m.span() for m in SHOWN.finditer(_TAG.sub(_blank, text))]
    spans += [m.span(1) for m in _RUN_BODY.finditer(text)]
    return sorted(spans)


def live(pattern: re.Pattern[str], text: str) -> list[re.Match[str]]:
    """Matches of `pattern` that start outside shown text."""
    spans = _shown(text)
    starts = [a for a, _ in spans]
    ends = [b for _, b in spans]
    reach = list(itertools.accumulate(ends, max))  # furthest end of any span so far
    found = []
    for match in pattern.finditer(text):
        i = bisect.bisect_right(starts, match.start()) - 1
        if i < 0 or reach[i] <= match.start():
            found.append(match)
    return found


def findall(pattern: re.Pattern[str], text: str) -> list:
    """`pattern.findall(text)`, live matches only (same shape: the group, a tuple of groups, or
    the whole match)."""
    found = []
    for match in live(pattern, text):
        groups = match.groups("")  # an unmatched group is "", as re.findall gives it
        found.append(match.group(0) if not groups else groups[0] if len(groups) == 1 else groups)
    return found


def only(text: str) -> str:
    """The live tags of `text`, one per line, with every word around them dropped."""
    return "\n".join(match.group(0) for match in live(_TAG, text))


def strip(pattern: re.Pattern[str], text: str) -> str:
    """`pattern.sub("", text)` for live matches only; a shown tag stays as written."""
    parts, at = [], 0
    for match in live(pattern, text):
        parts.append(text[at : match.start()])
        at = match.end()
    parts.append(text[at:])
    return "".join(parts)
