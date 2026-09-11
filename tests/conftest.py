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
        max_attachments=4,
        attachment_sweep_minutes=10,
        thread_ttl_minutes=60,
        memory_index_max_lines=200,
        memory_index_max_bytes=25_000,
        memory_user_max_bytes=50_000_000,
        memory_guild_max_bytes=200_000_000,
        memory_recall_max_bytes=25_000,
        memory_recall_rounds=2,
        output_style_path=Path("/opt/discord-codex/output-style.md"),
    )
