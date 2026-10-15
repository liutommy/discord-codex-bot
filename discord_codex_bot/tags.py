"""The Bot's own tags in an answer, minus the ones the answer only shows.

Text an answer shows rather than does: Discord code (``` blocks, a block not closed yet running
to the end, `inline` spans, which may cross lines) and quotes (a > line; >>> quotes the rest of
the message). A tag in there is an example, or a quote of someone else's words (a member's message
can carry any tag), never a request. Every parser of an action tag goes through here.
"""

from __future__ import annotations

import re

SHOWN = re.compile(
    r"```[\s\S]*?(?:```|\Z)|``(?:(?!``)[\s\S])+?``|`[^`]+`|^[ \t]*>>>[ \t][\s\S]*|^[ \t]*>.*$",
    re.M,
)


def live(pattern: re.Pattern[str], text: str) -> list[re.Match[str]]:
    """Matches of `pattern` that start outside shown text."""
    shown = [m.span() for m in SHOWN.finditer(text)]
    return [m for m in pattern.finditer(text) if not any(a <= m.start() < b for a, b in shown)]


def findall(pattern: re.Pattern[str], text: str) -> list:
    """`pattern.findall(text)`, live matches only (same shape: the group, a tuple of groups, or
    the whole match)."""
    found = []
    for match in live(pattern, text):
        groups = match.groups("")  # an unmatched group is "", as re.findall gives it
        found.append(match.group(0) if not groups else groups[0] if len(groups) == 1 else groups)
    return found


def strip(pattern: re.Pattern[str], text: str) -> str:
    """`pattern.sub("", text)` for live matches only; a shown tag stays as written."""
    for match in reversed(live(pattern, text)):
        text = text[: match.start()] + text[match.end() :]
    return text
