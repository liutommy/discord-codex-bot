import asyncio
import json
import os
from dataclasses import replace
from pathlib import Path

import pytest

from discord_codex_bot import codex
from discord_codex_bot.codex import (
    FALLBACK_INFO,
    CodexFallbackError,
    CodexServerOverloaded,
    CodexServiceError,
    CodexUsageLimit,
    _arguments,
    _communicate,
    _safe_environment,
    parse_codex_jsonl,
    parse_fallback_error,
    parse_usage_limit,
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

    async def __call__(
        self, prompt, config, images, effort, resume, schema=None, plain=False, isolated=False
    ):
        self.calls.append(
            dict(
                prompt=prompt,
                images=images,
                effort=effort,
                resume=resume,
                schema=schema,
                plain=plain,
                isolated=isolated,
            )
        )
        return self.replies.pop(0) if len(self.replies) > 1 else self.replies[0]


SPENT = (
    "You've hit your usage limit. Upgrade to Pro (https://chatgpt.com/explore/pro), visit "
    "https://chatgpt.com/codex/settings/usage to purchase more credits or try again at "
    "Sep 15th, 2026 12:13 AM."
)
THREAD = "01a0a071-5a3f-7ba1-937b-de4e06a7e09c"


def _exec_stream(message: str, thread: str = THREAD) -> str:
    """`codex exec --json` on a failed turn, the shape codex-cli 0.153.4 printed against a real
    spent quota on 2026-09-14: the message and nothing else, no error code. A resumed turn
    (`thread=""`) announces no thread.started."""
    events = [{"type": "thread.started", "thread_id": thread}] if thread else []
    events += [
        {"type": "turn.started"},
        {"type": "error", "message": message},
        {"type": "turn.failed", "error": {"message": message}},
    ]
    return "\n".join(json.dumps(e) for e in events)


def _rollout(codex_home: Path, thread: str, info: str, message: str) -> Path:
    """The rollout Codex wrote for the same turn -- the only place the code appears."""
    day = codex_home / "sessions" / "2026" / "09" / "14"
    day.mkdir(parents=True, exist_ok=True)
    path = day / f"rollout-2026-09-14T14-16-13-{thread}.jsonl"
    payload = {"type": "task_complete", "error": {"message": message, "codex_error_info": info}}
    path.write_text(json.dumps({"type": "event_msg", "payload": payload}) + "\n")
    return path


def test_exec_stream_has_no_code_so_the_rollout_supplies_it(tmp_path: Path) -> None:
    # Regression for 2026-09-14: the parser keyed on `codex_error_info`, which the exec stream
    # never carries, so a real spent quota fell through to a bare RuntimeError with an empty
    # stderr summary and the fallback never ran. Every job in the container failed for hours.
    _rollout(tmp_path, THREAD, "usage_limit_exceeded", SPENT)
    error = parse_fallback_error(_exec_stream(SPENT), tmp_path)
    assert isinstance(error, CodexUsageLimit) and error.info == "usage_limit_exceeded"
    assert "Sep 15th, 2026 12:13 AM" in str(error)


def test_a_resumed_turn_finds_its_rollout_by_the_stored_thread_id(tmp_path: Path) -> None:
    # `exec resume` announces no thread.started; the id the caller passed is the only key.
    _rollout(tmp_path, "t-old", "rate_limit_exceeded", "slow down")
    error = parse_fallback_error(_exec_stream("slow down", thread=""), tmp_path, "t-old")
    assert isinstance(error, CodexServiceError) and error.info == "rate_limit_exceeded"
    assert parse_fallback_error(_exec_stream("slow down", thread=""), tmp_path) is None


def test_usage_limit_is_still_recognised_without_a_rollout(tmp_path: Path) -> None:
    # No CODEX_HOME to look in, or the rollout is missing: the one message seen for real still
    # routes to the spare. Anything else without a code stays a plain failure.
    assert isinstance(parse_fallback_error(_exec_stream(SPENT)), CodexUsageLimit)
    assert isinstance(parse_fallback_error(_exec_stream(SPENT), tmp_path), CodexUsageLimit)
    assert parse_fallback_error(_exec_stream("something else broke"), tmp_path) is None
    assert parse_usage_limit(_exec_stream(SPENT)).endswith("12:13 AM.")
    assert parse_usage_limit(_events("fine")) == ""


@pytest.mark.parametrize("info", sorted(FALLBACK_INFO))
def test_every_listed_code_is_a_typed_fallback(tmp_path: Path, info: str) -> None:
    _rollout(tmp_path, THREAD, info, "")
    error = parse_fallback_error(_exec_stream("turn failed"), tmp_path)
    assert isinstance(error, FALLBACK_INFO[info]) and isinstance(error, CodexFallbackError)
    assert error.info == info and str(error)  # a code without a message still reads as something


@pytest.mark.parametrize(
    "info",
    [
        "bad_request",
        "sandbox_error",
        "session_budget_exceeded",
        "context_window_exceeded",
        "cyber_policy",
        "misalignment_policy_violation",
        "other",
        "stream_error",
    ],
)
def test_unlisted_codes_stay_plain_failures(tmp_path: Path, info: str) -> None:
    # Falling back on these would hide a Bot bug, paper over local config, or route a refused
    # prompt around the policy. They must surface as errors, not as answers from another model.
    _rollout(tmp_path, THREAD, info, "refused")
    assert parse_fallback_error(_exec_stream("refused"), tmp_path) is None


async def test_run_codex_routes_a_real_spent_quota_to_the_fallback(
    config: Config, tmp_path: Path, monkeypatch
) -> None:
    config = replace(config, codex_home=tmp_path)
    _rollout(tmp_path, "t-old", "usage_limit_exceeded", SPENT)
    stderr = "Reading additional input from stdin...\n"  # all stderr held on the real failure
    fake = FakeExec((1, _exec_stream(SPENT, thread=""), stderr))
    monkeypatch.setattr(codex, "_exec", fake)
    with pytest.raises(CodexUsageLimit) as raised:
        await run_codex("q", config, resume="t-old")
    assert raised.value.info == "usage_limit_exceeded" and len(fake.calls) == 1


def test_server_overload_is_a_typed_fallback_error() -> None:
    overloaded = json.dumps(
        {
            "type": "turn.complete",
            "error": {
                "message": "Selected model is at capacity. Please try a different model.",
                "codex_error_info": "server_overloaded",
            },
        }
    )
    error = parse_fallback_error(overloaded)
    assert isinstance(error, CodexServerOverloaded)
    assert isinstance(error, CodexFallbackError)
    assert "at capacity" in str(error)


async def test_run_codex_raises_usage_limit_without_retrying_the_resume(
    config: Config, monkeypatch
) -> None:
    spent = json.dumps(
        {"error": {"message": "usage limit", "codex_error_info": "usage_limit_exceeded"}}
    )
    fake = FakeExec((1, spent, ""))
    monkeypatch.setattr(codex, "_exec", fake)
    with pytest.raises(CodexUsageLimit, match="usage limit"):
        await run_codex("q", config, resume="t-old")
    assert len(fake.calls) == 1  # retrying a spent quota only wastes the member's wait


async def test_run_codex_raises_server_overload_without_retrying_the_resume(
    config: Config, monkeypatch
) -> None:
    overloaded = json.dumps(
        {
            "error": {
                "message": "Selected model is at capacity. Please try a different model.",
                "codex_error_info": "server_overloaded",
            }
        }
    )
    fake = FakeExec((1, overloaded, ""))
    monkeypatch.setattr(codex, "_exec", fake)
    with pytest.raises(CodexServerOverloaded, match="at capacity"):
        await run_codex("q", config, resume="t-old")
    assert [call["resume"] for call in fake.calls] == ["t-old"]


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


def test_isolated_arguments_disable_memory_history_and_tools(config: Config) -> None:
    args = _arguments(config, isolated=True)
    for value in (
        "features.memories=false",
        "memories.use_memories=false",
        "memories.generate_memories=false",
        'history.persistence="none"',
        'web_search="disabled"',
        "features.image_generation=false",
        "features.multi_agent=false",
    ):
        assert value in args


async def test_isolated_run_uses_plain_workspace(config: Config, monkeypatch) -> None:
    fake = FakeExec((0, _events("ok", "t1"), ""))
    monkeypatch.setattr(codex, "_exec", fake)
    await run_codex("untrusted", config, raw=True, isolated=True)
    assert fake.calls[0]["plain"] is True and fake.calls[0]["isolated"] is True


async def test_run_codex_falls_back_to_a_new_thread_when_resume_fails(
    config: Config, monkeypatch
) -> None:
    fake = FakeExec((1, "", "thread not found"), (0, _events("hi", "t-new"), ""))
    monkeypatch.setattr(codex, "_exec", fake)
    result = await run_codex("q", config, resume="t-old")
    assert [c["resume"] for c in fake.calls] == ["t-old", ""]
    assert result.text == "hi" and result.thread_id == "t-new" and result.resumed is False


async def test_run_codex_recognises_server_overload_from_the_fresh_retry(
    config: Config, monkeypatch
) -> None:
    overloaded = json.dumps(
        {
            "error": {
                "message": "Selected model is at capacity. Please try a different model.",
                "codex_error_info": "server_overloaded",
            }
        }
    )
    fake = FakeExec((1, "", "thread not found"), (1, overloaded, ""))
    monkeypatch.setattr(codex, "_exec", fake)
    with pytest.raises(CodexServerOverloaded, match="at capacity"):
        await run_codex("q", config, resume="t-old")
    assert [call["resume"] for call in fake.calls] == ["t-old", ""]


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


async def test_run_codex_wraps_prompt_unless_raw_and_style_keeps_the_persona(
    config: Config, tmp_path, monkeypatch
) -> None:
    style = tmp_path / "style.md"
    style.write_text("條列\n", "utf-8")
    config = replace(config, output_style_path=style)
    fake = FakeExec((0, _events("ok", "t"), ""))
    monkeypatch.setattr(codex, "_exec", fake)
    await run_codex("問題", config, memory="- [a](a.md) — x", personal_style="短")
    sent = fake.calls[-1]
    # a personal style is a formatting preference: it must not move the member off the persona
    assert sent["plain"] is False and sent["effort"] == ""
    assert "<USER_MESSAGE>\n問題\n</USER_MESSAGE>" in sent["prompt"]
    assert "<OUTPUT_STYLE>\n條列" in sent["prompt"] and "<PERSONAL_STYLE>\n短" in sent["prompt"]
    assert "<MEMORY>\n- [a](a.md) — x" in sent["prompt"]
    await run_codex("verbatim", config, raw=True, effort="low", schema=Path("/s.json"))
    sent = fake.calls[-1]
    assert sent["prompt"] == "verbatim" and sent["plain"] is False
    assert sent["effort"] == "low" and sent["schema"] == Path("/s.json")
    await run_codex("問題", config, personal_style="短", plain=True)
    assert fake.calls[-1]["plain"] is True  # only the explicit opt-out drops the persona
    await run_codex("問題", config, isolated=True)
    assert fake.calls[-1]["plain"] is True  # isolated runs never carry the persona


def test_prompt_carries_the_help_sheet_and_the_no_invention_rule() -> None:
    from discord_codex_bot.codex import _prompt

    with_help = _prompt("q", help="這個 Bot 的斜線指令：\n/x — y")
    assert "<HELP>\n這個 Bot 的斜線指令：\n/x — y\n</HELP>" in with_help
    assert "never invent commands, options or abilities" in with_help
    assert with_help.index("</HELP>") < with_help.index("<USER_MESSAGE>")
    assert "<HELP>" not in _prompt("q")


async def test_cancelling_a_request_kills_the_whole_process_group() -> None:
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
    task = asyncio.get_running_loop().create_task(_communicate(process, "", timeout_seconds=30))
    await asyncio.sleep(0.05)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert process.returncode is not None
    assert await _gone(grandchild_pid), "the grandchild outlived the cancellation"


def test_prompt_carries_attached_files_as_untrusted_content() -> None:
    from discord_codex_bot.codex import _prompt

    out = _prompt("q", files='<FILE name="a.txt">\nhello\n</FILE>')
    assert '<FILES>\n<FILE name="a.txt">\nhello\n</FILE>\n</FILES>' in out
    assert "FILES, when present, holds the text of documents" in out
    assert "<FILES>" not in _prompt("q")


def test_prompt_tells_the_model_the_current_taipei_time() -> None:
    import re

    from discord_codex_bot.codex import _prompt

    pattern = r"Current time: \d{4}-\d{2}-\d{2} \d{2}:\d{2} \(\w+\) Asia/Taipei\."
    assert re.search(pattern, _prompt("q"))
