from dataclasses import dataclass

from discord_codex_bot.bot import DiscordCodexClient, strip_mention
from discord_codex_bot.config import Config


@dataclass
class FakeAttachment:
    content_type: str | None
    size: int


def test_registers_only_expected_slash_commands(config: Config) -> None:
    client = DiscordCodexClient(config)
    assert {command.name for command in client.tree.get_commands()} == {
        "codex",
        "codex-status",
        "codex-reset",
        "remember",
        "forget",
        "memory",
        "style",
    }
    assert client.intents.guilds
    assert client.intents.message_content


def test_strip_mention_removes_every_bot_mention_form() -> None:
    assert strip_mention("<@123> 你好 <@!123>  嗎", 123) == "你好   嗎"
    assert strip_mention("<@999> 不是我", 123) == "<@999> 不是我"


def test_validate_rejects_too_many_or_non_image_attachments(config: Config) -> None:
    client = DiscordCodexClient(config)
    images = [FakeAttachment("image/png", 10)] * config.max_attachments
    assert client._validate("q", images) == ""
    assert "最多" in client._validate("q", images + [FakeAttachment("image/png", 10)])
    assert client._validate("q", [FakeAttachment("text/plain", 10)])
    assert client._validate("", []) != ""


def test_codex_command_offers_every_verified_effort(config: Config) -> None:
    from discord_codex_bot.config import REASONING_EFFORTS

    client = DiscordCodexClient(config)
    command = next(c for c in client.tree.get_commands() if c.name == "codex")
    effort = next(p for p in command.parameters if p.name == "effort")
    assert {choice.value: choice.name for choice in effort.choices} == REASONING_EFFORTS
    assert not effort.required
