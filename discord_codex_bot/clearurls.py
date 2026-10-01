"""ClearURLs rules applied to a link: the maintained per-site knowledge of tracking params
that a hand-kept blocklist cannot keep up with (206 providers upstream vs. ~20 names here).

Rule data: https://gitlab.com/ClearURLs/rules (LGPL-3.0), fetched daily by
scripts/fetch_clearurls.py into config/clearurls.json. Only the data is theirs; this matcher
is ours, and it applies a deliberate subset of what the browser extension does:

    redirections -> unwrap to the target in group(1) (percent-decoded) and clean that instead
    rawRules     -> regex removed from the whole URL (Amazon's ``/ref=...`` path segment)
    rules        -> query / fragment params whose key fully matches (case-insensitive) go

Not applied: ``referralMarketing`` (affiliate ids -- a policy the operator did not choose;
generic ``ref``/``tag`` stay, same stance as links.py) and ``completeProvider`` (the extension
blocks the request outright; a Bot never withholds a member's link).

Segments are kept verbatim, so a link without any matching param comes back byte-identical.
A missing or unreadable rules file degrades to "no rules" and links.py's own blocklist still
applies -- the blocklist is the floor, this is the reach."""

from __future__ import annotations

import json
import logging
import os
import re
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import unquote, urlsplit, urlunsplit

LOGGER = logging.getLogger(__name__)

DEFAULT_PATHS = (
    Path("/opt/discord-codex/clearurls.json"),
    Path(__file__).resolve().parent.parent / "config" / "clearurls.json",
)
MAX_REDIRECT_DEPTH = 3


@dataclass(frozen=True)
class Provider:
    name: str
    pattern: re.Pattern[str]
    rules: tuple[re.Pattern[str], ...]
    raw_rules: tuple[re.Pattern[str], ...]
    exceptions: tuple[re.Pattern[str], ...]
    redirections: tuple[re.Pattern[str], ...]


def _compile(patterns, where: str) -> tuple[re.Pattern[str], ...]:
    out = []
    for pattern in patterns or ():
        try:
            out.append(re.compile(pattern, re.IGNORECASE))
        except re.error as exc:
            LOGGER.warning("clearurls: skipping %s regex %r: %s", where, pattern, exc)
    return tuple(out)


class Rules:
    def __init__(self, providers: list[Provider]) -> None:
        self.providers = providers

    @classmethod
    def empty(cls) -> Rules:
        return cls([])

    @classmethod
    def from_dict(cls, data: dict) -> Rules:
        providers = []
        for name, spec in (data.get("providers") or {}).items():
            if not isinstance(spec, dict) or not spec.get("urlPattern"):
                continue
            try:
                pattern = re.compile(spec["urlPattern"], re.IGNORECASE)
            except re.error as exc:
                LOGGER.warning("clearurls: skipping provider %s: %s", name, exc)
                continue
            providers.append(
                Provider(
                    name=name,
                    pattern=pattern,
                    rules=_compile(spec.get("rules"), f"{name}.rules"),
                    raw_rules=_compile(spec.get("rawRules"), f"{name}.rawRules"),
                    exceptions=_compile(spec.get("exceptions"), f"{name}.exceptions"),
                    redirections=_compile(spec.get("redirections"), f"{name}.redirections"),
                )
            )
        return cls(providers)

    @classmethod
    def load(cls, path: Path) -> Rules:
        with open(path, encoding="utf-8") as fh:
            return cls.from_dict(json.load(fh))

    def clean(self, url: str, depth: int = 0) -> str:
        for provider in self.providers:
            if not provider.pattern.search(url):
                continue
            if any(exc.search(url) for exc in provider.exceptions):
                continue
            for redirect in provider.redirections:
                match = redirect.search(url)
                target = unquote(match.group(1)) if match and match.lastindex else ""
                if target.startswith(("http://", "https://")) and depth < MAX_REDIRECT_DEPTH:
                    return self.clean(target, depth + 1)
            for raw in provider.raw_rules:
                url = raw.sub("", url)
            if provider.rules:
                url = _drop_params(url, provider.rules)
        return url


def _drop_params(url: str, rules: tuple[re.Pattern[str], ...]) -> str:
    try:
        parts = urlsplit(url)
    except ValueError:
        return url

    def keep(segment: str) -> bool:
        key = unquote(segment.split("=", 1)[0])
        return not any(rule.fullmatch(key) for rule in rules)

    query = "&".join(s for s in parts.query.split("&") if keep(s)) if parts.query else ""
    fragment = parts.fragment
    if "=" in fragment:
        fragment = "&".join(s for s in fragment.split("&") if keep(s))
    if query == parts.query and fragment == parts.fragment:
        return url
    return urlunsplit(parts._replace(query=query, fragment=fragment))


_RULES: Rules | None = None


def rules() -> Rules:
    """The process-wide rule set: CLEARURLS_FILE, else the first DEFAULT_PATHS that exists."""
    global _RULES
    if _RULES is None:
        _RULES = Rules.empty()
        env = os.environ.get("CLEARURLS_FILE", "").strip()
        candidates = [Path(env)] if env else list(DEFAULT_PATHS)
        for path in candidates:
            if not path.is_file():
                continue
            try:
                _RULES = Rules.load(path)
            except (OSError, ValueError) as exc:
                LOGGER.warning("clearurls: %s unreadable (%s); using no rules", path, exc)
            break
        else:
            LOGGER.warning("clearurls: no rules file at %s; using no rules", candidates)
    return _RULES


def clean(url: str) -> str:
    return rules().clean(url)
