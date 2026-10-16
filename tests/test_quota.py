"""How much of a quota the Bot may still spend: a reserve that shrinks as the reset nears."""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta

import pytest

from discord_codex_bot import agy
from discord_codex_bot.quota import AgyQuota, group_of, may_spend, reserve_percent

NOW = datetime(2026, 10, 16, 3, 0, tzinfo=UTC)


def at(hours: float) -> datetime:
    return NOW + timedelta(hours=hours)


@pytest.mark.parametrize(
    ("hours", "reserve"),
    [(168, 20.0), (300, 20.0), (96, 10.0), (24, 0.0), (5, 0.0), (-1, 0.0)],
)
def test_the_reserve_shrinks_as_the_reset_nears(hours, reserve) -> None:
    """20% a week out, nothing within a day of the reset: what is left then would only expire."""
    assert reserve_percent(20.0, at(hours), NOW) == pytest.approx(reserve)


def test_an_unknown_reset_keeps_the_whole_reserve() -> None:
    assert reserve_percent(20.0, None, NOW) == 20.0
    assert not may_spend(15.0, 20.0, None, NOW) and may_spend(25.0, 20.0, None, NOW)
    assert may_spend(15.0, 20.0, at(12), NOW)  # a day from the reset: spend it


USAGE = {
    "status": "SUCCESS",
    "usage": {"total_tokens": 0},
    "command": {
        "name": "usage",
        "data": {
            "groups": [
                {"name": "Gemini", "buckets": [
                    {"id": "gemini-weekly", "window": "weekly", "remaining_fraction": 0.99,
                     "reset_time": "2026-10-23T02:39:19Z"},
                    {"id": "gemini-5h", "window": "5h", "remaining_fraction": 0.99,
                     "reset_time": "2026-10-16T07:39:19Z"}]},
                {"name": "Third party", "buckets": [
                    {"id": "3p-weekly", "window": "weekly", "remaining_fraction": 0.0005,
                     "reset_time": "2026-10-21T07:32:34Z"},
                    {"id": "3p-5h", "window": "5h", "remaining_fraction": 0.99,
                     "reset_time": "2026-10-16T08:13:48Z"}]},
                {"name": "Gemini 4 Argon", "buckets": [
                    {"id": "gemini-argon-wtu-weekly", "window": "weekly",
                     "remaining_fraction": 0.15, "reset_time": "2026-10-22T03:00:00Z"}]},
            ]
        },
    },
}  # fmt: skip


def test_groups_follow_the_model_family() -> None:
    assert group_of("gemini-4-argon-medium") == "gemini-argon-wtu"
    assert group_of("claude-opus-5-5-high") == group_of("gpt-oss-120b-medium") == "3p"
    assert group_of("gemini-3.8-flash-low") == "gemini"


def test_a_usage_reading_says_which_pools_the_bot_may_spend() -> None:
    quota = AgyQuota(20.0)
    quota.update(USAGE)
    assert quota.usable("gemini", NOW)
    assert not quota.usable("3p", NOW)  # 0.05% left, five days out
    assert not quota.usable("gemini-argon-wtu", NOW)  # 15% left, six days out: under 20%
    assert quota.usable("gemini-argon-wtu", datetime(2026, 10, 21, 12, tzinfo=UTC))  # < 24h
    assert AgyQuota(20.0).usable("gemini-argon-wtu", NOW)  # nothing read yet: do not block


def test_a_spent_pool_stays_spent_until_its_reset() -> None:
    quota = AgyQuota(20.0)
    quota.update(USAGE)
    quota.mark_spent("gemini", NOW)
    assert not quota.usable("gemini", NOW + timedelta(days=3))
    assert quota.usable("gemini", datetime(2026, 10, 23, 3, tzinfo=UTC))  # past its reset
    unknown = AgyQuota(20.0)
    unknown.mark_spent("gemini-argon-wtu", NOW)  # no reading: until the next one, at least
    assert not unknown.usable("gemini-argon-wtu", NOW + timedelta(minutes=5))


async def test_the_usage_command_is_read_without_a_conversation(config, monkeypatch) -> None:
    seen = []

    async def fake_run(args, stdin, cwd, cfg, on_delta=None):
        seen.append(args)
        return 0, json.dumps(USAGE), ""

    monkeypatch.setattr(agy, "_run", fake_run)
    reading = await agy.read_usage(config)
    assert reading["command"]["name"] == "usage"
    assert seen[0][:2] == ["-p", "/usage"] and "--print-timeout" in seen[0]
    monkeypatch.setattr(agy, "_run", lambda *a, **k: _failed())
    assert await agy.read_usage(config) is None


async def _failed():
    return 1, json.dumps({"status": "ERROR", "error": "authentication failed or timed out"}), ""
