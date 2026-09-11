from pathlib import Path

import pytest

from discord_codex_bot.config import Config


@pytest.fixture
def config() -> Config:
    return Config(
        discord_token="test-token",
        application_id=123456789012345678,
        allowed_guild_ids=frozenset({111111111111111111}),
        allowed_channel_ids=frozenset({222222222222222222}),
        codex_model="gpt-5.6-luna",
        codex_reasoning_effort="high",
        codex_home=Path("/var/lib/codex"),
        codex_workspace=Path("/workspace"),
        codex_timeout_seconds=600,
        max_prompt_chars=6000,
        max_response_chars=12000,
        max_queued_jobs=10,
        attachment_dir=Path("/tmp/discord-codex"),
        max_attachment_bytes=8_000_000,
        attachment_sweep_minutes=10,
    )
