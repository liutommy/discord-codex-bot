import asyncio
import json
import os
from dataclasses import replace
from pathlib import Path

import pytest

from discord_codex_bot import codex
from discord_codex_bot.codex import (
    _arguments,
    _communicate,
    _safe_environment,
    parse_codex_jsonl,
    run_codex,
)
from discord_codex_bot.config import Config


def _events(text: str, thread: str = "") -> str:
    events = [{"type": "thread.started", "thread_id": thread}] if thread else []
    events.append({"type": "item.completed", "item": {"type": "agent_message", "text": text}})
    return "\n".join(json.dumps(e) for e in events)


class FakeExec:
    """Stands in for codex._exec; `replies` is consumed per call, the last one repeats."""

    def __init__(self, *replies: tuple[int, str, str]) -> None:
        self.replies = list(replies)
        self.calls: list[dict] = []

    async def __call__(self, prompt, config, images, effort, resume, schema=None, plain=False):
        self.calls.append(
            dict(prompt=prompt, images=images, effort=effort, resume=resume, schema=schema,
                 plain=plain)
        )
        return self.replies.pop(0) if len(self.replies) > 1 else self.replies[0]


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
    assert await _gone(grandchild_pid), "the grandchild outlived the timeout"


async def _gone(pid: int) -> bool:
    """True once `pid` is dead: reaped (kill 0 fails) or a zombie waiting for init to reap it —
    on a busy runner that reap can lag a little behind the kill."""
    for _ in range(60):
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            return True
        try:
            stat = await asyncio.to_thread(Path(f"/proc/{pid}/stat").read_text)
        except OSError:
            return True
        if stat.rsplit(")", 1)[1].split()[0] == "Z":
            return True
        await asyncio.sleep(0.05)
    return False


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


def test_per_request_effort_overrides_default(config: Config) -> None:
    from discord_codex_bot.codex import _arguments

    assert 'model_reasoning_effort="high"' in _arguments(config)
    assert 'model_reasoning_effort="max"' in _arguments(config, effort="max")


def test_generated_images_are_collected_by_thread_id(config: Config, tmp_path) -> None:
    from dataclasses import replace

    from discord_codex_bot.codex import collect_generated_images, parse_thread_id

    stdout = "\n".join(
        (
            json.dumps({"type": "thread.started", "thread_id": "thread-1"}),
            json.dumps({"type": "item.completed", "item": {"type": "agent_message", "text": "ok"}}),
        )
    )
    assert parse_thread_id(stdout) == "thread-1"
    assert parse_thread_id("not json") == ""

    config = replace(config, codex_home=tmp_path)
    out = tmp_path / "generated_images" / "thread-1"
    out.mkdir(parents=True)
    (out / "b.png").write_bytes(b"x")
    (out / "a.png").write_bytes(b"x")
    directory, images = collect_generated_images(config, "thread-1")
    assert directory == out
    assert [p.name for p in images] == ["a.png", "b.png"]
    assert collect_generated_images(config, "missing") == (None, ())
    assert collect_generated_images(config, "") == (None, ())


def test_prompt_layers_default_and_personal_style() -> None:
    from discord_codex_bot.codex import _prompt

    text = _prompt("q", memory="", style="預設風格", personal_style="個人風格")
    assert text.index("<OUTPUT_STYLE>\n預設風格") < text.index("<PERSONAL_STYLE>\n個人風格")
    assert "<PERSONAL_STYLE>" not in _prompt("q", style="預設風格")
    assert "<OUTPUT_STYLE>" not in _prompt("q")


def test_arguments_combine_schema_images_and_plain_workspace(config: Config) -> None:
    images = [Path("/img/a.png"), Path("/img/b.png")]
    args = _arguments(config, images=images, schema=Path("/s.json"), plain=True)
    assert args[:2] == ("exec", "--model")
    assert args.index("--cd") + 1 == args.index("/workspace-plain")
    assert args.index("--output-schema") + 1 == args.index("/s.json")
    assert args.index("--output-schema") > args.index("--cd")
    # `-i` is variadic: images go last, `--` fences the stdin marker.
    assert args[-6:] == ("-i", "/img/a.png", "-i", "/img/b.png", "--", "-")
    resumed = _arguments(config, resume="t", schema=Path("/s.json"))
    assert "--cd" not in resumed and "--output-schema" in resumed


async def test_run_codex_falls_back_to_a_new_thread_when_resume_fails(
    config: Config, monkeypatch
) -> None:
    fake = FakeExec((1, "", "thread not found"), (0, _events("hi", "t-new"), ""))
    monkeypatch.setattr(codex, "_exec", fake)
    result = await run_codex("q", config, resume="t-old")
    assert [c["resume"] for c in fake.calls] == ["t-old", ""]
    assert result.text == "hi" and result.thread_id == "t-new" and result.resumed is False


async def test_run_codex_keeps_the_resumed_thread_id(config: Config, monkeypatch) -> None:
    # A resumed turn emits no thread.started; the stored id must survive into the result.
    monkeypatch.setattr(codex, "_exec", FakeExec((0, _events("ok"), "")))
    result = await run_codex("q", config, resume="t-old")
    assert result.thread_id == "t-old" and result.resumed is True


async def test_run_codex_collects_generated_images(config: Config, tmp_path, monkeypatch) -> None:
    config = replace(config, codex_home=tmp_path)
    out = tmp_path / "generated_images" / "t1"
    out.mkdir(parents=True)
    (out / "pic.png").write_bytes(b"x")
    monkeypatch.setattr(codex, "_exec", FakeExec((0, _events("看圖", "t1"), "")))
    result = await run_codex("draw", config)
    assert result.generated_dir == out and [p.name for p in result.images] == ["pic.png"]


async def test_run_codex_raises_on_failure_or_missing_message(config: Config, monkeypatch) -> None:
    monkeypatch.setattr(codex, "_exec", FakeExec((2, "", "a\nb\nc\nd")))
    with pytest.raises(RuntimeError, match=r"code 2: b \| c \| d"):
        await run_codex("q", config)
    only_thread = json.dumps({"type": "thread.started", "thread_id": "t"})
    monkeypatch.setattr(codex, "_exec", FakeExec((0, only_thread, "")))
    with pytest.raises(RuntimeError, match="no agent message"):
        await run_codex("q", config)
    # Without a stored thread there is nothing to fall back to: one attempt only.
    fake = FakeExec((1, "", "boom"))
    monkeypatch.setattr(codex, "_exec", fake)
    with pytest.raises(RuntimeError):
        await run_codex("q", config)
    assert len(fake.calls) == 1


async def test_run_codex_wraps_prompt_unless_raw_and_plain_follows_style(
    config: Config, tmp_path, monkeypatch
) -> None:
    style = tmp_path / "style.md"
    style.write_text("條列\n", "utf-8")
    config = replace(config, output_style_path=style)
    fake = FakeExec((0, _events("ok", "t"), ""))
    monkeypatch.setattr(codex, "_exec", fake)
    await run_codex("問題", config, memory="- [a](a.md) — x", personal_style="短")
    sent = fake.calls[-1]
    assert sent["plain"] is True and sent["effort"] == ""
    assert "<USER_MESSAGE>\n問題\n</USER_MESSAGE>" in sent["prompt"]
    assert "<OUTPUT_STYLE>\n條列" in sent["prompt"] and "<PERSONAL_STYLE>\n短" in sent["prompt"]
    assert "<MEMORY>\n- [a](a.md) — x" in sent["prompt"]
    await run_codex("verbatim", config, raw=True, effort="low", schema=Path("/s.json"))
    sent = fake.calls[-1]
    assert sent["prompt"] == "verbatim" and sent["plain"] is False
    assert sent["effort"] == "low" and sent["schema"] == Path("/s.json")


def test_prompt_carries_the_help_sheet_and_the_no_invention_rule() -> None:
    from discord_codex_bot.codex import _prompt

    with_help = _prompt("q", help="這個 Bot 的斜線指令：\n/x — y")
    assert "<HELP>\n這個 Bot 的斜線指令：\n/x — y\n</HELP>" in with_help
    assert "never invent commands, options or abilities" in with_help
    assert with_help.index("</HELP>") < with_help.index("<USER_MESSAGE>")
    assert "<HELP>" not in _prompt("q")


async def test_cancelling_a_request_kills_the_whole_process_group() -> None:
    process = await asyncio.create_subprocess_exec(
        "sh", "-c", "sleep 60 & echo $! ; wait",
        stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE, start_new_session=True,
    )
    grandchild_pid = int((await process.stdout.readline()).strip())
    task = asyncio.get_running_loop().create_task(_communicate(process, "", timeout_seconds=30))
    await asyncio.sleep(0.05)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert process.returncode is not None
    assert await _gone(grandchild_pid), "the grandchild outlived the cancellation"
