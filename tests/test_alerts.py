from __future__ import annotations

import asyncio
from dataclasses import replace
from types import SimpleNamespace as NS

from discord_codex_bot.alerts import Alerter, immediate, login_watch
from discord_codex_bot.config import Config


class Client:
    def __init__(self, owner: int = 42) -> None:
        self.owner, self.sent = owner, []
        self.user = NS(send=self._send)

    async def application_info(self):
        return NS(owner=NS(id=self.owner))

    def get_user(self, user_id):
        return self.user if user_id == self.owner else None

    async def fetch_user(self, user_id):
        return self.user

    async def _send(self, text):
        self.sent.append(text)


def test_immediate_markers() -> None:
    assert immediate("Codex exited with code 1: 尚未以 ChatGPT 訂閱登入")
    assert immediate("OrcaRouter HTTP 401：Invalid API key")
    assert not immediate("OpenRouter HTTP 429：Provider returned error")


async def test_owner_is_the_application_owner_unless_configured(config: Config) -> None:
    client = Client(owner=42)
    assert await Alerter(client, config).resolve_owner() == 42
    assert await Alerter(client, replace(config, alert_user_id=7)).resolve_owner() == 7


async def test_streak_alerts_once_then_recovery(config: Config) -> None:
    client = Client()
    alerter = Alerter(client, config)  # threshold 3
    await alerter.record_failure("codex", "boom 1")
    await alerter.record_failure("codex", "boom 2")
    assert client.sent == []
    await alerter.record_failure("codex", "Codex exited with code 1: boom 3")
    assert len(client.sent) == 1 and "連續 3 次失敗" in client.sent[0]
    assert "boom 3" in client.sent[0] and "codex" in client.sent[0]
    await alerter.record_failure("codex", "boom 4")  # inside the cooldown: no second DM
    assert len(client.sent) == 1
    await alerter.record_success("codex")
    assert client.sent[-1] == "✅ **codex** 已恢復正常。" and len(client.sent) == 2
    await alerter.record_success("codex")  # no outstanding alert: silent
    assert len(client.sent) == 2


async def test_auth_failures_alert_at_once(config: Config) -> None:
    client = Client()
    alerter = Alerter(client, config)
    await alerter.record_failure("orcarouter", "OrcaRouter HTTP 401：Invalid API key")
    assert len(client.sent) == 1 and client.sent[0].startswith("🔑 **orcarouter**")


async def test_dm_failure_never_raises(config: Config) -> None:
    class Broken(Client):
        async def _send(self, text):
            raise RuntimeError("DM closed")

    alerter = Alerter(Broken(), config)
    for _ in range(3):
        await alerter.record_failure("codex", "x")  # would alert; the DM error is swallowed


async def test_login_watch_alerts_on_loss_and_notes_recovery(config: Config) -> None:
    client = Client()
    alerter = Alerter(client, config)
    statuses = iter(["尚未以 ChatGPT 訂閱登入", "尚未以 ChatGPT 訂閱登入", "ChatGPT 訂閱登入有效"])

    async def check(cfg):
        return next(statuses)

    task = asyncio.get_running_loop().create_task(login_watch(alerter, config, check, 0.01))
    await asyncio.sleep(0.08)
    task.cancel()
    assert client.sent[0].startswith("🔑 **Codex** 登入狀態異常")
    assert client.sent[1] == "✅ **Codex** 已恢復正常。" and len(client.sent) == 2
