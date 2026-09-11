from discord_codex_bot.bot import DiscordCodexClient
from discord_codex_bot.config import Config


def test_registers_only_expected_slash_commands(config: Config) -> None:
    client = DiscordCodexClient(config)
    assert {command.name for command in client.tree.get_commands()} == {
        "codex",
        "codex-status",
    }
    assert client.intents.guilds
    assert not client.intents.message_content
