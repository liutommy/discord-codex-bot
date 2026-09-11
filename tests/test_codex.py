import asyncio
import json
import os

import pytest

from discord_codex_bot.codex import _communicate, _safe_environment, parse_codex_jsonl
from discord_codex_bot.config import Config


def test_child_environment_excludes_discord_secrets(config: Config, monkeypatch) -> None:
    monkeypatch.setenv("DISCORD_TOKEN", "secret")
    env = _safe_environment(config)
    assert "DISCORD_TOKEN" not in env
    assert env["CODEX_HOME"] == str(config.codex_home)


async def test_timeout_kills_whole_process_group() -> None:
    # Mirrors the real launch: a wrapper (sh) whose grandchild (sleep) must not outlive the timeout.
    process = await asyncio.create_subprocess_exec(
        "sh",
        "-c",
        "sleep 60 & echo $! ; wait",
        stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        start_new_session=True,
    )
    grandchild_pid = int((await process.stdout.readline()).strip())
    with pytest.raises(RuntimeError, match="timed out"):
        await _communicate(process, "", timeout_seconds=1)
    assert process.returncode is not None
    with pytest.raises(ProcessLookupError):
        os.kill(grandchild_pid, 0)


def test_extracts_last_completed_agent_message() -> None:
    stdout = "\n".join(
        (
            json.dumps({"type": "thread.started", "thread_id": "thread-1"}),
            json.dumps(
                {"type": "item.completed", "item": {"type": "agent_message", "text": "first"}}
            ),
            "not json",
            json.dumps(
                {
                    "type": "item.completed",
                    "item": {"type": "agent_message", "text": "final answer"},
                }
            ),
        )
    )
    assert parse_codex_jsonl(stdout) == "final answer"
