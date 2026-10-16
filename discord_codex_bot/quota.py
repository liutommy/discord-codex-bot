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


# The pools the owner keeps a reserve on; any other pool is held back only once agy says spent.
RESERVED = frozenset({"gemini-argon-wtu", "3p"})


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
        # group -> the bucket that ran out, as it was when marked: only that bucket with more
        # left, or with a later reset, lifts the mark (the same reading, one taken before the
        # mark, or another bucket of the pool refilling, is no news)
        self._spent_seen: dict[str, dict[str, tuple[float, datetime | None]]] = {}

    def update(self, reading: object) -> None:
        """Take a /usage reading; one of an unexpected shape is ignored (the last one stands).
        A pool marked spent whose buckets all have quota left in this newer reading is free
        again: the mark lasts until the emptiest bucket's reset, which can be days away."""
        buckets: dict[str, tuple[float, datetime | None]] = {}
        command = reading.get("command") if isinstance(reading, dict) else None
        data = command.get("data") if isinstance(command, dict) else None
        groups = data.get("groups") if isinstance(data, dict) else None
        for group in groups if isinstance(groups, list) else []:
            for bucket in group.get("buckets") or [] if isinstance(group, dict) else []:
                if not isinstance(bucket, dict):
                    continue
                fraction = bucket.get("remaining_fraction")
                if isinstance(fraction, (int, float)) and bucket.get("id"):
                    reset = parse_time(bucket.get("reset_time"))
                    buckets[str(bucket["id"])] = (float(fraction) * 100, reset)
        if not buckets:
            LOGGER.warning("agy usage reading has no buckets; keeping the last one")
            return
        self._buckets = buckets
        for group, seen in list(self._spent_seen.items()):
            if any(self._newer(bucket, then) for bucket, then in seen.items()):
                del self._spent_seen[group]
                self._spent_until.pop(group, None)

    def _newer(self, bucket: str, then: tuple[float, datetime | None]) -> bool:
        current = self._buckets.get(bucket)
        if current is None:
            return False
        (left, reset), (was_left, was_reset) = current, then
        later = reset is not None and was_reset is not None and reset > was_reset
        return left > was_left or later

    def _of(self, group: str) -> list[tuple[float, datetime | None]]:
        return [v for k, v in self._buckets.items() if k.rsplit("-", 1)[0] == group]

    def usable(self, group: str, now: datetime | None = None) -> bool:
        now = now or datetime.now(UTC)
        if now.timestamp() < self._spent_until.get(group, 0.0):
            return False
        if group not in RESERVED:
            return True
        # A bucket past its reset is full again, whatever an old reading said.
        return all(
            may_spend(left, self.base, reset, now)
            for left, reset in self._of(group)
            if reset is None or reset > now
        )

    def mark_spent(self, group: str, now: datetime | None = None) -> None:
        """agy said the pool is spent: skip it until the reset of its emptiest bucket, or until a
        newer reading shows quota left."""
        now = now or datetime.now(UTC)
        own = {k: v for k, v in self._buckets.items() if k.rsplit("-", 1)[0] == group}
        emptiest = min(own, key=lambda k: own[k][0]) if own else None
        reset = own[emptiest][1] if emptiest else None
        until = (
            reset.timestamp() if reset and reset > now else now.timestamp() + UNKNOWN_SPENT_SECONDS
        )
        self._spent_until[group] = until
        self._spent_seen[group] = {emptiest: own[emptiest]} if emptiest else {}
        LOGGER.warning(
            "agy pool %s spent; skipped until %s", group, datetime.fromtimestamp(until, UTC)
        )

    def summary(self) -> dict[str, float]:
        return {bucket: round(left, 1) for bucket, (left, _reset) in self._buckets.items()}


# The Bot's AgyQuota, set when it starts: background jobs that fall back to agy check it too.
shared: AgyQuota | None = None
