import pytest

from discord_codex_bot.config import load_config, parse_id_set


def test_parses_ids_without_duplicates() -> None:
    assert parse_id_set(
        "111111111111111111, 111111111111111111", "TEST"
    ) == frozenset({111111111111111111})


def test_rejects_missing_allowlist_and_malformed_ids() -> None:
    base = {
        "DISCORD_TOKEN": "test-token",
        "DISCORD_APPLICATION_ID": "123456789012345678",
    }
    with pytest.raises(ValueError, match="at least one"):
        load_config({**base, "ALLOWED_GUILD_IDS": ""})
    with pytest.raises(ValueError, match="invalid"):
        load_config({**base, "ALLOWED_GUILD_IDS": "not-an-id"})


def test_loads_safe_operational_defaults() -> None:
    config = load_config(
        {
            "DISCORD_TOKEN": "test-token",
            "DISCORD_APPLICATION_ID": "123456789012345678",
            "ALLOWED_GUILD_IDS": "111111111111111111",
        }
    )
    assert config.codex_model == "gpt-5.6-luna"
    assert config.codex_reasoning_effort == "high"
    assert config.max_queued_jobs == 10
