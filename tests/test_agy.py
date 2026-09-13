import asyncio
import json
from dataclasses import replace
from pathlib import Path

import pytest

from discord_codex_bot import agy
from discord_codex_bot.agy import PROJECTS_DIR, ensure_project, project_id, run_agy
from discord_codex_bot.config import Config


def _project_file(config: Config, pid: str, workspace: Path) -> Path:
    directory = config.agy_home / PROJECTS_DIR
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / f"{pid}.json"
    data = {"id": pid, "projectResources": {"resources": [{"folderUri": workspace.as_uri()}]}}
    path.write_text(json.dumps(data), "utf-8")
    return path


def _stream(conversation: str, response: str, status: str = "SUCCESS", error: str = "") -> str:
    result = {"conversation_id": conversation, "status": status, "response": response}
    if error:
        result["error"] = error
    return "\n".join(
        (
            json.dumps({"event": "init", "conversation_id": conversation}),
            json.dumps({"event": "result", "result": result}),
        )
    )


class FakeRun:
    """Stands in for agy._run; `replies` is consumed per call, the last one repeats."""

    def __init__(self, *replies: tuple[int, str, str]) -> None:
        self.replies = list(replies)
        self.calls: list[dict] = []

    async def __call__(self, args, stdin, cwd, config, on_delta=None):
        self.calls.append(dict(args=list(args), stdin=stdin, cwd=cwd))
        return self.replies.pop(0) if len(self.replies) > 1 else self.replies[0]


@pytest.fixture
def agy_config(config: Config, tmp_path: Path) -> Config:
    (tmp_path / "ws").mkdir()
    (tmp_path / "ws-plain").mkdir()
    return replace(
        config,
        agy_home=tmp_path,
        codex_workspace=tmp_path / "ws",
        codex_workspace_plain=tmp_path / "ws-plain",
    )


def _content(call: dict) -> str:
    assert call["stdin"].endswith("\n")
    message = json.loads(call["stdin"])
    assert message["event"] == "user"
    return message["message"]["content"]


def test_project_id_matches_the_workspace_folder_uri(agy_config: Config, tmp_path) -> None:
    workspace = agy_config.codex_workspace
    assert project_id(agy_config, workspace) == ""  # no projects dir yet
    _project_file(agy_config, "other", tmp_path / "elsewhere")
    (agy_config.agy_home / PROJECTS_DIR / "broken.json").write_text("{not json", "utf-8")
    (agy_config.agy_home / PROJECTS_DIR / "empty.json").write_text("{}", "utf-8")
    assert project_id(agy_config, workspace) == ""
    _project_file(agy_config, "mine", workspace)
    assert project_id(agy_config, workspace) == "mine"
    # an unnormalised path resolves to the same uri
    assert project_id(agy_config, workspace / "." / "ws" / "..") == "mine"


async def test_ensure_project_registers_once(agy_config: Config, monkeypatch) -> None:
    workspace = agy_config.codex_workspace

    async def register(args, stdin, cwd, config):
        _project_file(config, "fresh", cwd)
        return 0, "", ""

    monkeypatch.setattr(agy, "_run", register)
    assert await ensure_project(agy_config, workspace) == "fresh"
    calls = FakeRun((0, "", ""))
    monkeypatch.setattr(agy, "_run", calls)
    assert await ensure_project(agy_config, workspace) == "fresh"
    assert calls.calls == []  # already registered: no subprocess
    monkeypatch.setattr(agy, "_run", FakeRun((1, "", "quota exhausted")))
    with pytest.raises(RuntimeError, match="registration failed: quota exhausted"):
        await ensure_project(agy_config, agy_config.codex_workspace_plain)


async def test_ensure_project_passes_probe_arguments(agy_config: Config, monkeypatch) -> None:
    fake = FakeRun((0, "", ""))
    monkeypatch.setattr(agy, "_run", fake)
    assert await ensure_project(agy_config, agy_config.codex_workspace) == ""
    args = fake.calls[0]["args"]
    assert "--new-project" in args and args[args.index("--model") + 1] == "gemini-3.8-flash-low"
    assert fake.calls[0]["cwd"] == agy_config.codex_workspace


@pytest.fixture
def project(monkeypatch):
    seen: list[Path] = []

    async def fake_ensure(config, workspace):
        seen.append(workspace)
        return "proj-1"

    monkeypatch.setattr(agy, "ensure_project", fake_ensure)
    return seen


async def test_run_agy_builds_stream_json_arguments(
    agy_config: Config, project, monkeypatch
) -> None:
    fake = FakeRun((0, _stream("conv-1", "  答案  \n"), ""))
    monkeypatch.setattr(agy, "_run", fake)
    result = await run_agy("問題", agy_config, "gemini-3.8-flash-high", memory="- [a](a.md) — x")
    assert result.text == "答案" and result.thread_id == "conv-1" and result.resumed is False
    assert result.images == () and result.generated_dir is None
    call = fake.calls[0]
    args = call["args"]
    assert args[:4] == ["--project", "proj-1", "--model", "gemini-3.8-flash-high"]
    assert args[args.index("--output-format") + 1] == "stream-json"
    assert args[args.index("--input-format") + 1] == "stream-json"
    assert args[args.index("--print-timeout") + 1] == "600s"
    assert "--conversation" not in args and "--json-schema" not in args and "--add-dir" not in args
    assert call["cwd"] == agy_config.codex_workspace and project == [agy_config.codex_workspace]
    content = _content(call)
    assert "<USER_MESSAGE>\n問題\n</USER_MESSAGE>" in content and "<MEMORY>\n- [a]" in content


async def test_run_agy_personal_style_alone_uses_the_plain_workspace(
    agy_config: Config, project, monkeypatch
) -> None:
    fake = FakeRun((0, _stream("c", "ok"), ""))
    monkeypatch.setattr(agy, "_run", fake)
    await run_agy("q", agy_config, "m", personal_style="條列、少於 100 字")
    assert project == [agy_config.codex_workspace_plain]
    assert fake.calls[0]["cwd"] == agy_config.codex_workspace_plain


async def test_run_agy_raw_plain_and_schema(agy_config: Config, project, monkeypatch) -> None:
    fake = FakeRun((0, _stream("c", "ok"), ""))
    monkeypatch.setattr(agy, "_run", fake)
    await run_agy("verbatim", agy_config, "m", raw=True, schema=Path("/s.json"), plain=True)
    call = fake.calls[0]
    assert _content(call) == "verbatim"
    assert call["args"][call["args"].index("--json-schema") + 1] == "/s.json"
    assert call["cwd"] == agy_config.codex_workspace_plain
    assert project == [agy_config.codex_workspace_plain]


async def test_run_agy_resume_fallback_strips_conversation(
    agy_config: Config, project, monkeypatch
) -> None:
    fake = FakeRun((1, "", "no such conversation"), (0, _stream("conv-new", "fresh"), ""))
    monkeypatch.setattr(agy, "_run", fake)
    result = await run_agy("q", agy_config, "m", resume="conv-old")
    first, second = (c["args"] for c in fake.calls)
    assert first[first.index("--conversation") + 1] == "conv-old"
    assert "--conversation" not in second and "conv-old" not in second
    assert second == [a for a in first if a not in ("--conversation", "conv-old")]
    assert fake.calls[0]["stdin"] == fake.calls[1]["stdin"]
    assert result.text == "fresh" and result.thread_id == "conv-new" and result.resumed is False


async def test_run_agy_resumed_success_is_marked(agy_config: Config, project, monkeypatch) -> None:
    monkeypatch.setattr(agy, "_run", FakeRun((0, _stream("conv-old", "more"), "")))
    result = await run_agy("q", agy_config, "m", resume="conv-old")
    assert result.resumed is True and result.thread_id == "conv-old"


async def test_run_agy_images_add_dirs_and_prompt_suffix(
    agy_config: Config, project, tmp_path, monkeypatch
) -> None:
    (tmp_path / "req-b").mkdir()
    (tmp_path / "req-a").mkdir()
    images = [
        tmp_path / "req-b" / "1.png",
        tmp_path / "req-a" / "2.png",
        tmp_path / "req-b" / "3.jpg",
    ]
    fake = FakeRun((0, _stream("c", "看到了"), ""))
    monkeypatch.setattr(agy, "_run", fake)
    await run_agy("這是什麼", agy_config, "m", images=images, raw=True)
    args = fake.calls[0]["args"]
    dirs = [args[i + 1] for i, a in enumerate(args) if a == "--add-dir"]
    assert dirs == [str(tmp_path / "req-a"), str(tmp_path / "req-b")]  # sorted, deduplicated
    content = _content(fake.calls[0])
    listed = "、".join(str(p) for p in images)
    assert content.startswith("這是什麼\n\n（附件圖片：" + listed + "。")
    assert content.endswith("不要搜尋或執行指令。）")


async def test_run_agy_raises_on_error_status_or_empty_response(
    agy_config: Config, project, monkeypatch
) -> None:
    monkeypatch.setattr(agy, "_run", FakeRun((0, _stream("c", "", "ERROR", "boom"), "")))
    with pytest.raises(RuntimeError, match="code 0: ERROR: boom"):
        await run_agy("q", agy_config, "m")
    monkeypatch.setattr(agy, "_run", FakeRun((3, "", "x\ny\nz\nlast")))
    with pytest.raises(RuntimeError, match=r"code 3: y \| z \| last"):
        await run_agy("q", agy_config, "m")
    monkeypatch.setattr(agy, "_run", FakeRun((0, _stream("c", "  \n"), "  model idle  ")))
    with pytest.raises(RuntimeError, match=r"no response \(model idle\)"):
        await run_agy("q", agy_config, "m")
    monkeypatch.setattr(agy, "_run", FakeRun((0, "not json at all", "")))
    with pytest.raises(RuntimeError, match="no response$"):
        await run_agy("q", agy_config, "m")


def test_environment_is_minimal_and_pins_home(agy_config: Config, monkeypatch) -> None:
    monkeypatch.setenv("DISCORD_TOKEN", "secret")
    env = agy._environment(agy_config)
    assert "DISCORD_TOKEN" not in env and env["HOME"] == str(agy_config.agy_home)
    assert env["AGY_CLI_DISABLE_AUTO_UPDATE"] == "true"


async def test_run_streams_agent_text_deltas_while_agy_runs(monkeypatch, config) -> None:
    """A stand-in `agy` prints step_update lines with pauses; on_delta must see the text grow
    before the process ends, and the full stdout must still parse."""
    from discord_codex_bot import agy as agy_module

    def update(delta, state):
        return json.dumps({"event": "step_update", "step_update": {
            "step_type": "agent_response", "state": state, "text_delta": delta}})

    events = [
        json.dumps({"event": "init", "conversation_id": "c1"}),
        update("Hel", "ACTIVE"),
        "SLEEP",
        update("lo", "DONE"),
        json.dumps({"event": "result", "result": {
            "conversation_id": "c1", "response": "Hello", "status": "SUCCESS"}}),
    ]
    script = ";".join(
        "sleep 0.05" if e == "SLEEP" else "printf '%s\\n' '" + e.replace("'", "'\\''") + "'"
        for e in events
    )
    real_exec = asyncio.create_subprocess_exec

    async def fake_exec(program, *args, **kwargs):
        return await real_exec("sh", "-c", script, **kwargs)

    monkeypatch.setattr(agy_module.asyncio, "create_subprocess_exec", fake_exec)
    seen = []

    async def on_delta(text):
        seen.append(text)

    code, out, err = await agy_module._run(["x"], "{}", Path("/tmp"), config, on_delta)
    assert code == 0 and seen == ["Hel", "Hello"]
    assert agy_module.parse_stream(out) == ("c1", "Hello", "")
