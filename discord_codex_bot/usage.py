from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path

from .codex import run_codex
from .config import Config


@dataclass(frozen=True, slots=True)
class RateLimits:
    primary_used_percent: float  # 5-hour window
    secondary_used_percent: float  # 7-day window
    source: Path


def read_rate_limits(config: Config) -> RateLimits | None:
    """Latest `rate_limits` Codex wrote into its newest session rollout, if any."""
    root = config.codex_home / "sessions"
    if not root.is_dir():
        return None
    rollouts = sorted(root.rglob("rollout-*.jsonl"), key=lambda p: p.stat().st_mtime)
    for rollout in reversed(rollouts[-5:]):
        latest = None
        try:
            for line in rollout.read_text("utf-8", errors="ignore").splitlines():
                if '"rate_limits"' not in line:
                    continue
                try:
                    event = json.loads(line)
                except json.JSONDecodeError:
                    continue
                limits = (event.get("payload") or {}).get("rate_limits") or {}
                if limits.get("primary"):
                    latest = limits
        except OSError:
            continue
        if latest:
            return RateLimits(
                float(latest["primary"].get("used_percent", 0.0)),
                float((latest.get("secondary") or {}).get("used_percent", 0.0)),
                rollout,
            )
    return None


async def probe_rate_limits(config: Config) -> RateLimits | None:
    """Run the cheapest possible turn so Codex records fresh rate limits, then read them."""
    await run_codex("回覆 OK 兩個字。", config, effort="low")
    return read_rate_limits(config)
