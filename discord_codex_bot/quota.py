"""How much of a quota the Bot may still spend.

Every metered pool keeps a reserve for the other users of the same account (hub's own agy work,
Grok's X lookups). The reserve shrinks as the pool's reset nears: the full base a week out,
nothing within a day of it, since what is left then would only expire unused.
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime

LOGGER = logging.getLogger(__name__)
FULL_RESERVE_HOURS = 168.0  # a weekly pool's reserve is whole this far from its reset
NO_RESERVE_HOURS = 24.0  # and gone this close to it
UNKNOWN_SPENT_SECONDS = 600.0  # a spent pool with no reading to say when it resets


def reserve_percent(base: float, reset_at: datetime | None, now: datetime) -> float:
    """The share (0-100) still kept back: `base` until FULL_RESERVE_HOURS before the reset, then
    falling linearly to nothing at NO_RESERVE_HOURS. With no known reset, the whole base."""
    if reset_at is None:
        return base
    hours = (reset_at - now).total_seconds() / 3600
    if hours <= NO_RESERVE_HOURS:
        return 0.0
    span = FULL_RESERVE_HOURS - NO_RESERVE_HOURS
    return base * min(1.0, (hours - NO_RESERVE_HOURS) / span)


def may_spend(remaining: float, base: float, reset_at: datetime | None, now: datetime) -> bool:
    """Whether a pool with `remaining` percent left is above its reserve right now."""
    return remaining > reserve_percent(base, reset_at, now)


def parse_time(value: object) -> datetime | None:
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)


def group_of(model: str) -> str:
    """The agy pool a model slug spends (bucket ids are "<group>-<window>")."""
    if model.startswith("gemini-4-argon"):
        return "gemini-argon-wtu"
    if model.startswith(("claude-", "gpt-oss")):
        return "3p"
    return "gemini"


class AgyQuota:
    """The last `agy /usage` reading, per bucket, and pools found spent since. Nothing read yet
    blocks nothing: a missing reading must not stop the Bot from answering."""

    def __init__(self, base: float) -> None:
        self.base = base
        self._buckets: dict[str, tuple[float, datetime | None]] = {}  # id -> (left %, reset)
        self._spent_until: dict[str, float] = {}  # group -> epoch seconds

    def update(self, reading: dict) -> None:
        buckets = {}
        for group in (reading.get("command") or {}).get("data", {}).get("groups") or []:
            for bucket in group.get("buckets") or []:
                fraction = bucket.get("remaining_fraction")
                if isinstance(fraction, (int, float)) and bucket.get("id"):
                    reset = parse_time(bucket.get("reset_time"))
                    buckets[str(bucket["id"])] = (float(fraction) * 100, reset)
        if buckets:
            self._buckets = buckets

    def _of(self, group: str) -> list[tuple[float, datetime | None]]:
        return [v for k, v in self._buckets.items() if k.rsplit("-", 1)[0] == group]

    def usable(self, group: str, now: datetime | None = None) -> bool:
        now = now or datetime.now(UTC)
        if now.timestamp() < self._spent_until.get(group, 0.0):
            return False
        return all(may_spend(left, self.base, reset, now) for left, reset in self._of(group))

    def mark_spent(self, group: str, now: datetime | None = None) -> None:
        """agy said the pool is spent: skip it until the reset of its emptiest bucket."""
        now = now or datetime.now(UTC)
        buckets = self._of(group)
        reset = min(buckets, key=lambda b: b[0])[1] if buckets else None
        until = (
            reset.timestamp() if reset and reset > now else now.timestamp() + UNKNOWN_SPENT_SECONDS
        )
        self._spent_until[group] = until
        LOGGER.warning(
            "agy pool %s spent; skipped until %s", group, datetime.fromtimestamp(until, UTC)
        )

    def summary(self) -> dict[str, float]:
        return {bucket: round(left, 1) for bucket, (left, _reset) in self._buckets.items()}
