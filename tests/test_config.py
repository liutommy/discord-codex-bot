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
    assert config.codex_reasoning_effort == "medium"
    assert config.max_queued_jobs == 10
    assert not config.tracking_enabled
    assert config.tracking_interval_seconds == 900
    assert config.tracking_min_remaining_percent == 50
    assert config.tracking_ai_max_calls_per_day == 30
    assert config.tracking_reasoning_effort == "high"


def test_tracking_config_validates_switch_and_usage_gate() -> None:
    base = {
        "DISCORD_TOKEN": "t",
        "DISCORD_APPLICATION_ID": "123456789012345678",
        "ALLOWED_GUILD_IDS": "111111111111111111",
    }
    config = load_config(
        {
            **base,
            "TRACKING_ENABLED": "true",
            "TRACKING_INTERVAL_MINUTES": "5",
            "TRACKING_MIN_REMAINING_PERCENT": "65",
        }
    )
    assert config.tracking_enabled
    assert config.tracking_interval_seconds == 300
    assert config.tracking_min_remaining_percent == 65
    with pytest.raises(ValueError, match="TRACKING_ENABLED"):
        load_config({**base, "TRACKING_ENABLED": "sometimes"})


def test_effort_accepts_verified_values_and_rejects_unknown_ones() -> None:
    base = {
        "DISCORD_TOKEN": "t",
        "DISCORD_APPLICATION_ID": "123456789012345678",
        "ALLOWED_GUILD_IDS": "111111111111111111",
    }
    override = {**base, "CODEX_REASONING_EFFORT": "xhigh"}
    assert load_config(override).codex_reasoning_effort == "xhigh"
    with pytest.raises(ValueError, match="CODEX_REASONING_EFFORT"):
        load_config({**base, "CODEX_REASONING_EFFORT": "extra_high"})


def test_link_render_settings_default_and_reject_non_positive_values() -> None:
    base = {
        "DISCORD_TOKEN": "t",
        "DISCORD_APPLICATION_ID": "123456789012345678",
        "ALLOWED_GUILD_IDS": "111111111111111111",
    }
    config = load_config(base)
    assert config.link_render_timeout_seconds == 40
    assert config.link_screenshot_max_height == 4000
    with pytest.raises(ValueError, match="LINK_RENDER_TIMEOUT_SECONDS"):
        load_config({**base, "LINK_RENDER_TIMEOUT_SECONDS": "0"})
    with pytest.raises(ValueError, match="LINK_SCREENSHOT_MAX_HEIGHT"):
        load_config({**base, "LINK_SCREENSHOT_MAX_HEIGHT": "-1"})


def test_command_prefix_defaults_and_validates() -> None:
    base = {
        "DISCORD_TOKEN": "t",
        "DISCORD_APPLICATION_ID": "123456789012345678",
        "ALLOWED_GUILD_IDS": "111111111111111111",
    }
    assert load_config(base).command_prefix == "codex"
    assert load_config({**base, "COMMAND_PREFIX": "inmu-king"}).command_prefix == "inmu-king"
    with pytest.raises(ValueError, match="COMMAND_PREFIX"):
        load_config({**base, "COMMAND_PREFIX": "Inmu King"})
