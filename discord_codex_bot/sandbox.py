"""The model's scratch computer: `<run lang="python|sh">code</run>` executes in the sandbox
sidecar (isolated, no network, hard limits) and the output — plus any files the snippet saves
under ./out/ — comes back as a RESULT block, the files also going to the member."""

from __future__ import annotations

import asyncio
import base64
import logging
import re
from dataclasses import dataclass, field
from pathlib import Path

import aiohttp

from .config import Config

LOGGER = logging.getLogger(__name__)
RUN_TAG = re.compile(r'<run\s+lang="(python|sh)"\s*>(.*?)</run>', re.S)
MAX_RUNS_PER_ROUND = 2


@dataclass(frozen=True, slots=True)
class RunResult:
    exit: int
    timed_out: bool
    stdout: str
    stderr: str
    files: list[Path] = field(default_factory=list)
    skipped: list[str] = field(default_factory=list)


def _save(target: Path, data: bytes) -> None:
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(data)


def available(config: Config) -> bool:
    return bool(config.sandbox_url)


def extract_runs(answer: str) -> list[tuple[str, str]]:
    """[(lang, code)] in order; code is taken verbatim (the model writes real newlines)."""
    return [(lang, code.strip("\n")) for lang, code in RUN_TAG.findall(answer)][:MAX_RUNS_PER_ROUND]


async def run_code(lang: str, code: str, config: Config, out_dir: Path | None) -> RunResult:
    """POST the snippet to the sidecar; files it produced are saved under `out_dir`."""
    timeout = aiohttp.ClientTimeout(total=config.sandbox_timeout_seconds + 10)
    body = {"lang": lang, "code": code, "timeout": config.sandbox_timeout_seconds}
    async with aiohttp.ClientSession(timeout=timeout) as session:
        async with session.post(f"{config.sandbox_url.rstrip('/')}/run", json=body) as response:
            payload = await response.json(content_type=None)
    if "error" in payload:
        return RunResult(-1, False, "", str(payload["error"]))
    files: list[Path] = []
    skipped: list[str] = []
    for item in payload.get("files") or []:
        name = Path(str(item.get("name") or "file")).name
        if "b64" not in item or out_dir is None:
            skipped.append(f"{name}（{item.get('skipped', '未回傳')}）")
            continue
        target = out_dir / name
        await asyncio.to_thread(_save, target, base64.b64decode(item["b64"]))
        files.append(target)
    return RunResult(
        int(payload.get("exit", -1)), bool(payload.get("timed_out")),
        str(payload.get("stdout") or ""), str(payload.get("stderr") or ""), files, skipped,
    )


def render_result(lang: str, result: RunResult) -> str:
    """The RESULT block for the model: exit status, output, and which files came back."""
    status = "逾時被中止" if result.timed_out else f"exit {result.exit}"
    parts = [f'<RESULT kind="run" lang="{lang}" status="{status}">']
    if result.stdout.strip():
        parts.append(result.stdout.rstrip())
    if result.stderr.strip():
        parts.append(f"[stderr]\n{result.stderr.rstrip()}")
    if result.files:
        listed = "、".join(p.name for p in result.files)
        parts.append(f"[檔案已回傳並附給成員：{listed}]")
    if result.skipped:
        parts.append(f"[未回傳：{'、'.join(result.skipped)}]")
    if len(parts) == 1:
        parts.append("（沒有輸出）")
    parts.append("</RESULT>")
    return "\n".join(parts)
