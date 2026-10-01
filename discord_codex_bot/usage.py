from __future__ import annotations

import asyncio
import json
import logging
import math
from dataclasses import dataclass
from pathlib import Path

from .codex import _safe_environment
from .config import Config

LOGGER = logging.getLogger(__name__)
APP_SERVER_TIMEOUT_SECONDS = 20


@dataclass(frozen=True, slots=True)
class RateLimits:
    primary_used_percent: float  # 5-hour window
    secondary_used_percent: float  # 7-day window
    source: Path | str


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
    """Read the authoritative account limits without consuming a Codex turn."""
    return await query_rate_limits(config)


def parse_app_server_rate_limits(response: dict) -> RateLimits:
    """Normalize one account/rateLimits/read JSON-RPC response.

    Window duration is the stable discriminator: app-server names have changed before, while
    the subscription windows remain five hours and seven days.
    """
    if response.get("error"):
        raise ValueError("account/rateLimits/read returned an error")
    result = response.get("result")
    snapshot = result.get("rateLimits") if isinstance(result, dict) else None
    if not isinstance(snapshot, dict):
        raise ValueError("account/rateLimits/read returned no rateLimits")
    windows = [snapshot.get("primary"), snapshot.get("secondary")]

    def used(duration: int) -> float:
        for window in windows:
            if isinstance(window, dict) and window.get("windowDurationMins") == duration:
                value = window.get("usedPercent")
                if type(value) not in (int, float):
                    break
                percent = float(value)
                if math.isfinite(percent) and 0 <= percent <= 100:
                    return percent
                break
        raise ValueError(f"account/rateLimits/read returned no valid {duration}-minute window")

    return RateLimits(
        primary_used_percent=used(300),
        secondary_used_percent=used(10_080),
        source="app-server account/rateLimits/read",
    )


async def _send(process: asyncio.subprocess.Process, message: dict) -> None:
    if process.stdin is None:
        raise RuntimeError("Codex app-server stdin unavailable")
    process.stdin.write((json.dumps(message, separators=(",", ":")) + "\n").encode())
    await process.stdin.drain()


async def _response(process: asyncio.subprocess.Process, request_id: int) -> dict:
    if process.stdout is None:
        raise RuntimeError("Codex app-server stdout unavailable")

    async def wait() -> dict:
        while raw := await process.stdout.readline():
            try:
                message = json.loads(raw)
            except json.JSONDecodeError:
                continue
            if message.get("id") == request_id:
                return message
        raise RuntimeError("Codex app-server closed before replying")

    return await asyncio.wait_for(wait(), timeout=APP_SERVER_TIMEOUT_SECONDS)


async def query_rate_limits(config: Config) -> RateLimits | None:
    """Query Codex app-server's account/rateLimits/read method.

    No rollout or get_goal fallback is used: an unknown authoritative reading must defer optional
    background AI work instead of being mistaken for available quota.
    """
    try:
        process = await asyncio.create_subprocess_exec(
            "codex",
            "app-server",
            "--stdio",
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.DEVNULL,
            env=_safe_environment(config),
        )
    except OSError as error:
        LOGGER.warning("Codex app-server usage probe unavailable: %s", type(error).__name__)
        return None
    try:
        await _send(
            process,
            {
                "method": "initialize",
                "id": 0,
                "params": {
                    "clientInfo": {
                        "name": "discord_codex_bot",
                        "title": "Discord Codex Bot",
                        "version": "0.1.0",
                    },
                    "capabilities": {
                        "experimentalApi": False,
                        "requestAttestation": False,
                        "optOutNotificationMethods": [],
                    },
                },
            },
        )
        initialized = await _response(process, 0)
        if initialized.get("error"):
            raise RuntimeError("Codex app-server initialization failed")
        await _send(process, {"method": "initialized", "params": {}})
        await _send(
            process,
            {"method": "account/rateLimits/read", "id": 1, "params": None},
        )
        return parse_app_server_rate_limits(await _response(process, 1))
    except (TimeoutError, RuntimeError, ValueError) as error:
        LOGGER.warning("Codex app-server usage probe failed: %s", type(error).__name__)
        return None
    finally:
        if process.returncode is None:
            process.terminate()
            try:
                await asyncio.wait_for(process.wait(), timeout=5)
            except TimeoutError:
                process.kill()
                await process.wait()
