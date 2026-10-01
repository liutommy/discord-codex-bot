from __future__ import annotations

from pathlib import Path

import pytest

from discord_codex_bot import clearurls
from discord_codex_bot.clearurls import Rules
from discord_codex_bot.links import strip_tracking

REAL = Path(__file__).resolve().parent.parent / "config" / "clearurls.json"

INLINE = {
    "providers": {
        "globalRules": {
            "urlPattern": ".*",
            "rules": ["(?:%3F)?utm(?:_[a-z_]*)?", "yclid"],
            "referralMarketing": ["ref_?", "referrer"],
            "exceptions": ["^https?://(?:[a-z0-9-]+\\.)*?gitlab\\.com"],
        },
        "shop": {
            "urlPattern": "^https?://(?:[a-z0-9-]+\\.)*?shop\\.example",
            "rules": ["qid", "sr"],
            "rawRules": ["/ref=[^/?]*"],
            "referralMarketing": ["tag"],
        },
        "hop": {
            "urlPattern": "^https?://hop\\.example",
            "redirections": ["^https?://hop\\.example/out\\?.*?u=([^&]*)"],
        },
        "broken": {"urlPattern": "(unbalanced", "rules": ["x"]},
    }
}


@pytest.fixture
def rules() -> Rules:
    return Rules.from_dict(INLINE)


def test_rules_drop_matching_params_case_insensitively_and_keep_the_rest(rules):
    dirty = "https://a.example/p?UTM_Source=x&id=42&Yclid=1&v=%20"
    assert rules.clean(dirty) == "https://a.example/p?id=42&v=%20"
    assert rules.clean("https://a.example/p?id=42") == "https://a.example/p?id=42"


def test_fragment_params_are_cleaned_and_plain_fragments_kept(rules):
    assert rules.clean("https://a.example/p#utm_source=x&sec=2") == "https://a.example/p#sec=2"
    assert rules.clean("https://a.example/p#utm_source") == "https://a.example/p#utm_source"


def test_referral_marketing_rules_are_not_applied(rules):
    """Affiliate ids are a policy the operator did not choose; ClearURLs strips them by default."""
    url = "https://shop.example/i?tag=aff-20&ref=nav&referrer=x"
    assert rules.clean(url) == url


def test_exception_skips_the_provider_only(rules):
    url = "https://gitlab.com/g/p/-/tree/main?utm_source=x&ref_type=heads"
    assert rules.clean(url) == url
    # The floor in links.py still removes utm_* on the same link.
    assert strip_tracking(url) == "https://gitlab.com/g/p/-/tree/main?ref_type=heads"


def test_raw_rules_edit_the_path(rules):
    assert (
        rules.clean("https://www.shop.example/Name/dp/B0/ref=sr_1_1?keywords=k&qid=1&sr=8-1")
        == "https://www.shop.example/Name/dp/B0?keywords=k"
    )


def test_redirections_unwrap_and_clean_the_target(rules):
    assert (
        rules.clean(
            "https://hop.example/out?u=https%3A%2F%2Fa.example%2Fp%3Futm_source%3Dx%26id%3D1"
        )
        == "https://a.example/p?id=1"
    )
    # Only http(s) targets; anything else is left alone.
    url = "https://hop.example/out?u=javascript%3Aalert(1)"
    assert rules.clean(url) == url


def test_broken_provider_regex_is_dropped_not_fatal(rules):
    assert {p.name for p in rules.providers} == {"globalRules", "shop", "hop"}


def test_missing_rules_file_means_no_rules(monkeypatch, tmp_path):
    monkeypatch.setenv("CLEARURLS_FILE", str(tmp_path / "absent.json"))
    monkeypatch.setattr(clearurls, "_RULES", None)
    assert clearurls.rules().providers == []
    assert clearurls.clean("https://a.example/?utm_source=x") == "https://a.example/?utm_source=x"


# --- the shipped rule set ---------------------------------------------------------------


@pytest.fixture(scope="module")
def shipped() -> Rules:
    return Rules.load(REAL)


def test_shipped_rules_load_with_many_providers(shipped):
    assert len(shipped.providers) >= 100


@pytest.mark.parametrize(
    ("dirty", "clean"),
    [
        ("https://www.instagram.com/reel/ABC/?igsh=xyz", "https://www.instagram.com/reel/ABC/"),
        (
            "https://www.youtube.com/watch?v=abc&si=xyz&t=42",
            "https://www.youtube.com/watch?v=abc&t=42",
        ),
        (
            "https://www.google.com/url?q=https://a.example/x%3Fid%3D1&sa=D",
            "https://a.example/x?id=1",
        ),
        (
            "https://www.amazon.com/Name/dp/B08XYZ/ref=sr_1_1?keywords=foo&qid=123&tag=aff-20",
            # Upstream also drops `keywords` on Amazon; the affiliate `tag` stays (not applied).
            "https://www.amazon.com/Name/dp/B08XYZ?tag=aff-20",
        ),
    ],
)
def test_shipped_rules_cover_the_common_share_links(shipped, dirty, clean):
    assert shipped.clean(dirty) == clean


def test_shipped_rules_leave_a_plain_link_byte_identical(shipped):
    url = "https://a.example/?source=article&ref=revision&src=image&feature=preview&si=id"
    assert shipped.clean(url) == url
