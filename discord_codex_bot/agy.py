from __future__ import annotations

import asyncio
import json
import logging
import os
from collections.abc import Sequence
from pathlib import Path

from .codex import KILL_GRACE_SECONDS, MAX_PROCESS_OUTPUT_BYTES, CodexResult, _prompt, output_style
from .config import Config

LOGGER = logging.getLogger(__name__)
PROJECTS_DIR = Path(".gemini/config/projects")


def _environment(config: Config) -> dict[str, str]:
    return {
        "PATH": os.environ.get("PATH", "/usr/local/bin:/usr/bin:/bin"),
        "HOME": str(config.agy_home),
        "LANG": "C.UTF-8",
        "LC_ALL": "C.UTF-8",
        "AGY_CLI_DISABLE_AUTO_UPDATE": "true",  # the image pins what it installed
        "TERM": "dumb",
    }


def project_id(config: Config, workspace: Path) -> str:
    """The agy project whose only resource is `workspace`, or "" when none exists yet.

    `--new-project` writes one <id>.json per project under ~/.gemini/config/projects with a
    `projectResources.resources[].folderUri`; AGENTS.md is only honoured inside a project.
    """
    wanted = workspace.resolve().as_uri()
    directory = config.agy_home / PROJECTS_DIR
    if not directory.is_dir():
        return ""
    for path in sorted(directory.glob("*.json")):
        try:
            data = json.loads(path.read_text("utf-8"))
        except (OSError, ValueError):
            continue
        resources = (data.get("projectResources") or {}).get("resources") or []
        if any(r.get("folderUri") == wanted for r in resources):
            return str(data.get("id", ""))
    return ""


async def _run(args: list[str], stdin: str, cwd: Path, config: Config) -> tuple[int, str, str]:
    process = await asyncio.create_subprocess_exec(
        "agy",
        *args,
        cwd=str(cwd),
        stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        env=_environment(config),
        start_new_session=True,
    )
    try:
        stdout, stderr = await asyncio.wait_for(
            process.communicate(stdin.encode("utf-8")), timeout=config.codex_timeout_seconds
        )
    except TimeoutError:
        for sig in (15, 9):
            try:
                os.killpg(process.pid, sig)
            except ProcessLookupError:
                break
            try:
                await asyncio.wait_for(process.wait(), timeout=KILL_GRACE_SECONDS)
                break
            except TimeoutError:
                continue
        raise RuntimeError("agy request timed out") from None
    if len(stdout) + len(stderr) > MAX_PROCESS_OUTPUT_BYTES:
        raise RuntimeError("agy output exceeded the process limit")
    return (
        process.returncode or 0,
        stdout.decode("utf-8", errors="replace"),
        stderr.decode("utf-8", errors="replace"),
    )


async def ensure_project(config: Config, workspace: Path) -> str:
    """Register `workspace` as an agy project once; AGENTS.md is only honoured inside a project."""
    existing = project_id(config, workspace)
    if existing:
        return existing
    code, _out, err = await _run(
        [
            "--new-project", "-p", "回覆 OK",
            "--model", config.agy_probe_model,
            "--output-format", "json",
        ],
        "",
        workspace,
        config,
    )
    if code != 0:
        raise RuntimeError(f"agy project registration failed: {err.strip()[-200:]}")
    return project_id(config, workspace)


def parse_stream(stdout: str) -> tuple[str, str, str]:
    """(conversation_id, response text, error) from an agy stream-json run."""
    conversation = ""
    response = ""
    error = ""
    for line in stdout.splitlines():
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            continue
        if event.get("event") == "result":
            result = event.get("result") or {}
            conversation = str(result.get("conversation_id") or conversation)
            response = str(result.get("response") or "")
            if result.get("status") not in (None, "SUCCESS"):
                error = f"{result.get('status')}: {result.get('error') or ''}"
        elif event.get("event") == "init":
            conversation = str(event.get("conversation_id") or conversation)
    return conversation, response, error


async def run_agy(
    user_prompt: str,
    config: Config,
    model: str,
    images: Sequence[Path] = (),
    resume: str = "",
    memory: str = "",
    raw: bool = False,
    personal_style: str = "",
    schema: Path | None = None,
    plain: bool = False,
    links: str = "",
    help: str = "",
) -> CodexResult:
    """One turn on Antigravity CLI with the same contract as run_codex.

    Images are made visible by adding their directory to the workspace (--add-dir) and telling the
    model to open them with view_file; agy has no attach-image flag in print mode.
    """
    # Same rule as run_codex: a member with a personal style gets the persona-free workspace.
    plain = plain or bool(personal_style)
    workspace = config.codex_workspace_plain if plain else config.codex_workspace
    project = await ensure_project(config, workspace)
    prompt = (
        user_prompt
        if raw
        else _prompt(user_prompt, memory, output_style(config), personal_style, links, help)
    )
    args = ["--project", project, "--model", model, "--output-format", "stream-json"]
    args += ["--input-format", "stream-json", "--print-timeout", f"{config.codex_timeout_seconds}s"]
    if resume:
        args += ["--conversation", resume]
    if schema is not None:
        args += ["--json-schema", str(schema)]
    if images:
        for directory in sorted({image.parent for image in images}):
            args += ["--add-dir", str(directory)]
        listed = "、".join(str(image) for image in images)
        prompt += (
            f"\n\n（附件圖片：{listed}。回答前先用 view_file 逐一打開這些檔案，"
            "不要搜尋或執行指令。）"
        )
    stdin = json.dumps({"event": "user", "message": {"content": prompt}}, ensure_ascii=False) + "\n"
    code, out, err = await _run(args, stdin, workspace, config)
    if code != 0 and resume:
        LOGGER.warning("agy resume of %s failed (%s); starting a new conversation", resume, code)
        index = args.index("--conversation")
        args = args[:index] + args[index + 2 :]
        resume = ""
        code, out, err = await _run(args, stdin, workspace, config)
    conversation, response, error = parse_stream(out)
    if code != 0 or error:
        summary = error or " | ".join(err.strip().splitlines()[-3:])
        raise RuntimeError(f"agy exited with code {code}: {summary}")
    if not response.strip():
        detail = f" ({err.strip()[-160:]})" if err.strip() else ""
        raise RuntimeError("agy returned no response" + detail)
    return CodexResult(response.strip(), (), None, conversation, bool(resume))
