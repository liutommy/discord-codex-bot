from dataclasses import replace

from discord_codex_bot.access import check_access
from discord_codex_bot.config import Config


def test_rejects_dm_and_unlisted_guild(config: Config) -> None:
    assert not check_access(
        guild_id=None, channel_id=None, parent_channel_id=None, config=config
    ).allowed
    assert not check_access(
        guild_id=999999999999999999,
        channel_id=222222222222222222,
        parent_channel_id=None,
        config=config,
    ).allowed


def test_allows_listed_channel_and_its_thread(config: Config) -> None:
    assert check_access(
        guild_id=111111111111111111,
        channel_id=222222222222222222,
        parent_channel_id=None,
        config=config,
    ).allowed
    assert check_access(
        guild_id=111111111111111111,
        channel_id=333333333333333333,
        parent_channel_id=222222222222222222,
        config=config,
    ).allowed


def test_empty_channel_allowlist_permits_allowed_guild(config: Config) -> None:
    guild_only = replace(config, allowed_channel_ids=frozenset())
    assert check_access(
        guild_id=111111111111111111,
        channel_id=999999999999999999,
        parent_channel_id=None,
        config=guild_only,
    ).allowed
