from __future__ import annotations

from dataclasses import dataclass

from .config import Config


@dataclass(frozen=True, slots=True)
class AccessDecision:
    allowed: bool
    reason: str = ""


def check_access(
    *,
    guild_id: int | None,
    channel_id: int | None,
    parent_channel_id: int | None,
    config: Config,
) -> AccessDecision:
    if guild_id is None or guild_id not in config.allowed_guild_ids:
        return AccessDecision(False, "此 Discord 伺服器不在允許清單內。")
    if not config.allowed_channel_ids:
        return AccessDecision(True)
    if channel_id in config.allowed_channel_ids or parent_channel_id in config.allowed_channel_ids:
        return AccessDecision(True)
    return AccessDecision(False, "此頻道目前未開放 Codex。")
