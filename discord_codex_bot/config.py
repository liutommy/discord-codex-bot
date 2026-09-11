from __future__ import annotations

import os
import re
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path

DISCORD_ID = re.compile(r"^\d{17,20}$")


def _required(env: Mapping[str, str], name: str) -> str:
    value = env.get(name, "").strip()
    if not value:
        raise ValueError(f"Missing required environment variable: {name}")
    return value


def parse_id_set(value: str | None, name: str, *, required: bool = False) -> frozenset[int]:
    raw_ids = {part.strip() for part in (value or "").split(",") if part.strip()}
    if required and not raw_ids:
        raise ValueError(f"{name} must contain at least one Discord ID")
    if any(not DISCORD_ID.fullmatch(item) for item in raw_ids):
        raise ValueError(f"{name} contains an invalid Discord ID")
    return frozenset(int(item) for item in raw_ids)


def _positive_int(env: Mapping[str, str], name: str, default: int) -> int:
    raw = env.get(name, "").strip()
    if not raw:
        return default
    try:
        value = int(raw)
    except ValueError as error:
        raise ValueError(f"{name} must be a positive integer") from error
    if value <= 0:
        raise ValueError(f"{name} must be a positive integer")
    return value


@dataclass(frozen=True, slots=True)
class Config:
    discord_token: str
    application_id: int
    allowed_guild_ids: frozenset[int]
    allowed_channel_ids: frozenset[int]
    codex_model: str
    codex_reasoning_effort: str
    codex_home: Path
    codex_workspace: Path
    codex_timeout_seconds: int
    max_prompt_chars: int
    max_response_chars: int
    max_queued_jobs: int
    attachment_dir: Path
    max_attachment_bytes: int
    max_attachments: int
    attachment_sweep_minutes: int


def load_config(env: Mapping[str, str] | None = None) -> Config:
    values = os.environ if env is None else env
    application_id = _required(values, "DISCORD_APPLICATION_ID")
    if not DISCORD_ID.fullmatch(application_id):
        raise ValueError("DISCORD_APPLICATION_ID contains an invalid Discord ID")
    return Config(
        discord_token=_required(values, "DISCORD_TOKEN"),
        application_id=int(application_id),
        allowed_guild_ids=parse_id_set(
            values.get("ALLOWED_GUILD_IDS"), "ALLOWED_GUILD_IDS", required=True
        ),
        allowed_channel_ids=parse_id_set(
            values.get("ALLOWED_CHANNEL_IDS"), "ALLOWED_CHANNEL_IDS"
        ),
        codex_model=values.get("CODEX_MODEL", "").strip() or "gpt-5.6-luna",
        codex_reasoning_effort=(
            values.get("CODEX_REASONING_EFFORT", "").strip() or "high"
        ),
        codex_home=Path(values.get("CODEX_HOME", "/var/lib/codex")),
        codex_workspace=Path(values.get("CODEX_WORKSPACE", "/workspace")),
        codex_timeout_seconds=_positive_int(values, "CODEX_TIMEOUT_SECONDS", 600),
        max_prompt_chars=_positive_int(values, "MAX_PROMPT_CHARS", 6_000),
        max_response_chars=_positive_int(values, "MAX_RESPONSE_CHARS", 12_000),
        max_queued_jobs=_positive_int(values, "MAX_QUEUED_JOBS", 10),
        attachment_dir=Path(values.get("ATTACHMENT_DIR", "/tmp/discord-codex")),
        max_attachment_bytes=_positive_int(values, "MAX_ATTACHMENT_BYTES", 8_000_000),
        max_attachments=_positive_int(values, "MAX_ATTACHMENTS", 4),
        attachment_sweep_minutes=_positive_int(values, "ATTACHMENT_SWEEP_MINUTES", 10),
    )
