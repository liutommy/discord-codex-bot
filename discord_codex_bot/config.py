from __future__ import annotations

import os
import re
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path

DISCORD_ID = re.compile(r"^\d{17,20}$")
# Discord command names: lowercase, digits, hyphen; the prefix leaves room for "-remember" etc.
COMMAND_PREFIX = re.compile(r"^[a-z0-9][a-z0-9-]{0,19}$")
# Codex CLI value -> Codex UI label. Verified against `codex debug models` for gpt-5.6-luna
# (supported_reasoning_levels) and the request payload each value produces; re-verify on upgrade.
REASONING_EFFORTS = {
    "low": "Low",
    "medium": "Medium",
    "high": "High",
    "xhigh": "Extra high",
    "max": "Max",
}


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


def _bounded_int(env: Mapping[str, str], name: str, default: int, low: int, high: int) -> int:
    raw = env.get(name, "").strip()
    if not raw:
        return default
    try:
        value = int(raw)
    except ValueError as error:
        raise ValueError(f"{name} must be an integer between {low} and {high}") from error
    if not low <= value <= high:
        raise ValueError(f"{name} must be an integer between {low} and {high}")
    return value


def _command_prefix(env: Mapping[str, str]) -> str:
    value = env.get("COMMAND_PREFIX", "").strip() or "codex"
    if not COMMAND_PREFIX.fullmatch(value):
        raise ValueError("COMMAND_PREFIX must be 1-20 lowercase letters, digits or hyphens")
    return value


def _effort(env: Mapping[str, str]) -> str:
    value = env.get("CODEX_REASONING_EFFORT", "").strip() or "medium"
    if value not in REASONING_EFFORTS:
        raise ValueError(
            f"CODEX_REASONING_EFFORT must be one of {', '.join(REASONING_EFFORTS)}"
        )
    return value


@dataclass(frozen=True, slots=True)
class Config:
    discord_token: str
    application_id: int
    allowed_guild_ids: frozenset[int]
    allowed_channel_ids: frozenset[int]
    command_prefix: str
    codex_model: str
    codex_reasoning_effort: str
    codex_home: Path
    codex_workspace: Path
    codex_workspace_plain: Path
    codex_timeout_seconds: int
    max_prompt_chars: int
    max_response_chars: int
    max_queued_jobs: int
    attachment_dir: Path
    max_attachment_bytes: int
    max_attachments: int
    attachment_sweep_minutes: int
    thread_ttl_minutes: int
    harvest_interval_minutes: int
    memory_index_max_lines: int
    memory_index_max_bytes: int
    memory_user_max_bytes: int
    memory_guild_max_bytes: int
    memory_read_max_lines: int
    memory_read_max_bytes: int
    memory_search_max_matches: int
    memory_search_context_lines: int
    memory_recall_rounds: int
    output_style_path: Path
    permanent_memory_dir: Path
    agy_home: Path
    agy_probe_model: str
    agy_settings_path: Path
    announce_dir: Path
    announce_channel_ids: frozenset[int]
    announce_approved: str
    link_max_urls: int
    link_max_bytes: int
    link_max_chars: int
    link_timeout_seconds: int
    link_render_timeout_seconds: int
    link_screenshot_max_height: int
    consolidate_hour: int
    consolidate_timezone: str
    consolidate_min_remaining_percent: int
    consolidate_max_input_bytes: int
    consolidate_schema_path: Path


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
        command_prefix=_command_prefix(values),
        codex_model=values.get("CODEX_MODEL", "").strip() or "gpt-5.6-luna",
        codex_reasoning_effort=_effort(values),
        codex_home=Path(values.get("CODEX_HOME", "/var/lib/codex")),
        codex_workspace=Path(values.get("CODEX_WORKSPACE", "/workspace")),
        # Same rules without the operator persona; used when a member set a personal style.
        codex_workspace_plain=Path(values.get("CODEX_WORKSPACE_PLAIN", "/workspace-plain")),
        codex_timeout_seconds=_positive_int(values, "CODEX_TIMEOUT_SECONDS", 600),
        max_prompt_chars=_positive_int(values, "MAX_PROMPT_CHARS", 6_000),
        max_response_chars=_positive_int(values, "MAX_RESPONSE_CHARS", 12_000),
        max_queued_jobs=_positive_int(values, "MAX_QUEUED_JOBS", 10),
        attachment_dir=Path(values.get("ATTACHMENT_DIR", "/tmp/discord-codex")),
        max_attachment_bytes=_positive_int(values, "MAX_ATTACHMENT_BYTES", 8_000_000),
        max_attachments=_positive_int(values, "MAX_ATTACHMENTS", 4),
        attachment_sweep_minutes=_positive_int(values, "ATTACHMENT_SWEEP_MINUTES", 10),
        thread_ttl_minutes=_positive_int(values, "THREAD_TTL_MINUTES", 60),
        # Finished threads are distilled into the member's memory on this cadence.
        harvest_interval_minutes=_positive_int(values, "HARVEST_INTERVAL_MINUTES", 10),
        # Two-tier memory sized like Claude Code auto memory (index 200 lines / 25 KB).
        memory_index_max_lines=_positive_int(values, "MEMORY_INDEX_MAX_LINES", 200),
        memory_index_max_bytes=_positive_int(values, "MEMORY_INDEX_MAX_BYTES", 25_000),
        memory_user_max_bytes=_positive_int(values, "MEMORY_USER_MAX_BYTES", 50_000_000),
        memory_guild_max_bytes=_positive_int(values, "MEMORY_GUILD_MAX_BYTES", 200_000_000),
        # Read/search pages sized like pi's tool-output defaults (2000 lines / 50 KB).
        memory_read_max_lines=_positive_int(values, "MEMORY_READ_MAX_LINES", 2000),
        memory_read_max_bytes=_positive_int(values, "MEMORY_READ_MAX_BYTES", 50_000),
        memory_search_max_matches=_positive_int(values, "MEMORY_SEARCH_MAX_MATCHES", 50),
        memory_search_context_lines=_positive_int(values, "MEMORY_SEARCH_CONTEXT_LINES", 3),
        memory_recall_rounds=_positive_int(values, "MEMORY_RECALL_ROUNDS", 10),
        # Operator-written default output style, injected into every prompt when non-empty.
        output_style_path=Path(
            values.get("OUTPUT_STYLE_FILE", "/opt/discord-codex/output-style.md")
        ),
        # Operator-managed permanent memory (baked into the image, read-only for the Bot).
        permanent_memory_dir=Path(
            values.get("PERMANENT_MEMORY_DIR", "/opt/discord-codex/permanent")
        ),
        # Antigravity CLI backend (second backend; members pick it per user with /<prefix>-model).
        agy_home=Path(values.get("AGY_HOME", "/home/node")),
        agy_probe_model=values.get("AGY_PROBE_MODEL", "").strip() or "gemini-3.8-flash-low",
        agy_settings_path=Path(
            values.get("AGY_SETTINGS_FILE", "/opt/discord-codex/agy-settings.json")
        ),
        # Release announcements: announce/latest.md is posted once per guild when it changes.
        announce_dir=Path(values.get("ANNOUNCE_DIR", "/opt/discord-codex/announce")),
        announce_channel_ids=parse_id_set(
            values.get("ANNOUNCE_CHANNEL_IDS"), "ANNOUNCE_CHANNEL_IDS"
        ),
        # Hard gate: only the announcement whose content hash the owner approved is ever posted.
        announce_approved=values.get("ANNOUNCE_APPROVED", "").strip(),
        # Bot-side link fetching (all backends): URLs in a message are fetched into the prompt and
        # the model may ask for more with <fetch url/>. Public addresses only.
        link_max_urls=_positive_int(values, "LINK_MAX_URLS", 3),
        link_max_bytes=_positive_int(values, "LINK_MAX_BYTES", 2_000_000),
        link_max_chars=_positive_int(values, "LINK_MAX_CHARS", 20_000),
        link_timeout_seconds=_positive_int(values, "LINK_TIMEOUT_SECONDS", 15),
        # Chromium fallback (bot-challenge / client-rendered pages) and on-demand page screenshots.
        link_render_timeout_seconds=_positive_int(values, "LINK_RENDER_TIMEOUT_SECONDS", 40),
        link_screenshot_max_height=_positive_int(values, "LINK_SCREENSHOT_MAX_HEIGHT", 4000),
        # Daily memory consolidation: every scope of every guild, gated on 5h quota remaining.
        consolidate_hour=_bounded_int(values, "CONSOLIDATE_HOUR", 2, 0, 23),
        consolidate_timezone=values.get("CONSOLIDATE_TIMEZONE", "").strip() or "Asia/Taipei",
        consolidate_min_remaining_percent=_bounded_int(
            values, "CONSOLIDATE_MIN_REMAINING_PERCENT", 50, 0, 100
        ),
        consolidate_max_input_bytes=_positive_int(values, "CONSOLIDATE_MAX_INPUT_BYTES", 100_000),
        consolidate_schema_path=Path(
            values.get("CONSOLIDATE_SCHEMA_FILE", "/opt/discord-codex/consolidate-schema.json")
        ),
    )
