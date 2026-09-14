"""Operator alerts by DM: a backend failing repeatedly, a login or key that stopped working,
and the recovery afterwards. Without this the Bot's failures live only in the container log."""

from __future__ import annotations

import asyncio
import logging
import time
from typing import Any

from .config import Config

LOGGER = logging.getLogger(__name__)
# Failures that mean "the operator must act now", alerted on first sight rather than by streak.
_IMMEDIATE = (
    "login",
    "logged in",
    "unauthorized",
    "401",
    "invalid api key",
    "authentication",
    "credential",
    "尚未以 chatgpt",
    "not logged",
)


def immediate(error: str) -> bool:
    lowered = error.lower()
    return any(marker in lowered for marker in _IMMEDIATE)


class Alerter:
    """Streak counter per backend plus a per-kind cooldown; `client` only needs to resolve a
    user and send it a DM, so tests hand in a stub."""

    def __init__(self, client: Any, config: Config) -> None:
        self._client = client
        self._config = config
        self.owner_id: int = config.alert_user_id
        self._streak: dict[str, int] = {}
        self._alerted: dict[str, bool] = {}  # backend -> an alert is outstanding (for recovery)
        self._last_sent: dict[str, float] = {}

    async def resolve_owner(self) -> int:
        """ALERT_USER_ID when set, otherwise the Discord application's owner (whoever created
        the bot in the Developer Portal — the operator by definition)."""
        if not self.owner_id:
            try:
                info = await self._client.application_info()
                self.owner_id = int(info.owner.id)
            except Exception as error:  # discord raises several; alerts are best-effort
                LOGGER.warning("Could not resolve the application owner: %s", type(error).__name__)
        return self.owner_id

    async def record_failure(self, backend: str, error: str) -> None:
        streak = self._streak.get(backend, 0) + 1
        self._streak[backend] = streak
        summary = error.strip().splitlines()[-1][:300] if error.strip() else "（無訊息）"
        if immediate(error):
            await self._send(f"{backend}:auth", f"🔑 **{backend}** 看起來登入／金鑰失效：{summary}")
            self._alerted[backend] = True
        elif streak >= self._config.alert_after_failures:
            await self._send(
                f"{backend}:fail",
                f"⚠️ **{backend}** 連續 {streak} 次失敗，最近一次：{summary}\n請查 container log。",
            )
            self._alerted[backend] = True

    async def record_success(self, backend: str) -> None:
        self._streak[backend] = 0
        if self._alerted.pop(backend, False):
            await self._send(f"{backend}:ok", f"✅ **{backend}** 已恢復正常。", cooldown=False)

    async def login_lost(self, backend: str, detail: str) -> None:
        self._alerted[backend] = True
        await self._send(f"{backend}:auth", f"🔑 **{backend}** 登入狀態異常：{detail}")

    async def _send(self, kind: str, text: str, cooldown: bool = True) -> None:
        now = time.monotonic()
        window = self._config.alert_cooldown_minutes * 60
        if cooldown and now - self._last_sent.get(kind, float("-inf")) < window:
            return
        owner_id = await self.resolve_owner()
        if not owner_id:
            return
        try:
            user = self._client.get_user(owner_id) or await self._client.fetch_user(owner_id)
            await user.send(text)
            self._last_sent[kind] = now
        except Exception as error:  # DM closed, network, rate limit — never break the request
            LOGGER.warning("Alert DM failed: %s", type(error).__name__)


async def login_watch(alerter: Alerter, config: Config, check, interval_seconds: float) -> None:
    """Periodically confirm the Codex login; alert once when it is gone, and note recovery."""
    lost = False
    while True:
        try:
            status = await check(config)
            if "有效" not in status and not lost:
                lost = True
                await alerter.login_lost("Codex", status)
            elif "有效" in status and lost:
                lost = False
                await alerter.record_success("Codex")
        except Exception:
            LOGGER.exception("Login watch failed")
        await asyncio.sleep(interval_seconds)
