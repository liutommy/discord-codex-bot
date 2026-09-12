from dataclasses import dataclass

from discord_codex_bot.bot import DiscordCodexClient, strip_mention, with_quoted_message
from discord_codex_bot.config import Config


@dataclass
class FakeAttachment:
    content_type: str | None
    size: int


def test_registers_only_expected_slash_commands(config: Config) -> None:
    from dataclasses import replace

    client = DiscordCodexClient(replace(config, command_prefix="inmu-king"))
    assert {command.name for command in client.tree.get_commands()} == {
        "inmu-king",
        "inmu-king-status",
        "inmu-king-reset",
        "inmu-king-remember",
        "inmu-king-forget",
        "inmu-king-memory",
        "inmu-king-style",
        "inmu-king-model",
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


def test_with_quoted_message_folds_reply_target_into_prompt() -> None:
    folded = with_quoted_message("這是什麼", "ryanlo", "看看這張\n圖", 1)
    assert folded == (
        "（後輩回覆了 ryanlo 的訊息：「看看這張 圖」）\n"
        "（那則訊息附了 1 張圖，已一併附上）\n這是什麼"
    )
    assert with_quoted_message("", "kimo", "", 2).endswith("請看這則訊息。")
    assert with_quoted_message("q", "kimo", "", 0) == "q"
