"""ClearURLs rules applied to a link: the maintained per-site knowledge of tracking params
that a hand-kept blocklist cannot keep up with (206 providers upstream vs. ~20 names here).

Rule data: https://gitlab.com/ClearURLs/rules (LGPL-3.0), fetched by scripts/fetch_clearurls.py
into config/clearurls.json (the image's copy) and, through scripts/refresh_data.py, into the
generated-data volume, which is read again whenever it changes. Only the data is theirs; this
matcher is ours, and it applies a deliberate subset of what the browser extension does:

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

from .datafiles import LiveFile, data_dir

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
    def parse(cls, text: str) -> Rules:
        """ValueError for anything but a rule file (including one with no usable provider), so a
        truncated or reshaped one is refused rather than loaded as "no rules"."""
        data = json.loads(text)
        if not isinstance(data, dict) or not isinstance(data.get("providers"), dict):
            raise ValueError("no providers object")
        try:
            rules = cls.from_dict(data)
        except (TypeError, AttributeError, re.error) as error:
            raise ValueError(f"malformed rule file: {type(error).__name__}: {error}") from error
        if not rules.providers:
            raise ValueError("no usable providers")  # an empty set must not replace a good one
        return rules

    @classmethod
    def load(cls, path: Path) -> Rules:
        return cls.parse(path.read_text("utf-8"))

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
                    # partition, not urlsplit: a malformed target ("http://[x") must not raise
                    if signed(target.partition("?")[2].partition("#")[0]):
                        return target  # unwrapped, but every parameter is under the signature
                    return self.clean(target, depth + 1)
            for raw in provider.raw_rules:
                url = raw.sub("", url)
            if provider.rules:
                url = _drop_params(url, provider.rules)
        return url


def signed(query: str) -> bool:
    """A signed URL is one page only as written; dropping any param would break it."""
    keys = {unquote(segment.split("=", 1)[0]).lower() for segment in query.split("&")}
    return bool(keys & {"signature", "sig", "x-amz-signature", "x-goog-signature"})


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


_RULES: LiveFile[Rules] | None = None


def rules() -> Rules:
    """The process-wide rule set: DATA_DIR/clearurls.json (the refreshed copy), else the image's
    copy (CLEARURLS_FILE, or the first DEFAULT_PATHS that exists); a changed file is picked up
    without a restart."""
    global _RULES
    if _RULES is None:
        env = os.environ.get("CLEARURLS_FILE", "").strip()
        floor = [Path(env)] if env else list(DEFAULT_PATHS)
        candidates = [data_dir() / "clearurls.json", *floor]
        _RULES = LiveFile("clearurls", candidates, Rules.parse, Rules.empty())
    return _RULES.get()


def clean(url: str) -> str:
    return rules().clean(url)
