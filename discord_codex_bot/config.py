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


def _effort(
    env: Mapping[str, str], name: str = "CODEX_REASONING_EFFORT", default: str = "medium"
) -> str:
    value = env.get(name, "").strip() or default
    if value not in REASONING_EFFORTS:
        raise ValueError(f"{name} must be one of {', '.join(REASONING_EFFORTS)}")
    return value


def _boolean(env: Mapping[str, str], name: str, default: bool = False) -> bool:
    raw = env.get(name, "").strip().lower()
    if not raw:
        return default
    if raw in {"1", "true", "yes", "on"}:
        return True
    if raw in {"0", "false", "no", "off"}:
        return False
    raise ValueError(f"{name} must be true or false")


@dataclass(frozen=True, slots=True)
class Config:
    discord_token: str
    application_id: int
    allowed_guild_ids: frozenset[int]
    allowed_channel_ids: frozenset[int]
    command_prefix: str
    codex_model: str
    codex_reasoning_effort: str
    codex_fallback_model: str
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
    openrouter_api_key: str
    orcarouter_api_key: str
    alert_user_id: int
    alert_after_failures: int
    alert_cooldown_minutes: int
    alert_login_check_minutes: int
    search_url: str
    search_api: str
    search_api_key: str
    search_max_results: int
    remember_emoji_name: str
    backup_dir: Path | None
    backup_hour: int
    backup_keep_days: int
    log_dir: Path | None
    log_keep_days: int
    sandbox_url: str
    sandbox_timeout_seconds: int
    apis_path: Path | None
    apis_max_chars: int
    openrouter_dir: Path
    openrouter_catalog_ttl_seconds: int
    openrouter_history_chars: int
    announce_dir: Path
    announce_channel_ids: frozenset[int]
    announce_approved: str
    link_max_urls: int
    link_max_bytes: int
    link_max_chars: int
    link_timeout_seconds: int
    link_render_timeout_seconds: int
    link_screenshot_max_height: int
    link_preview_wait_seconds: float
    gemini_api_key: str
    gemini_model: str
    gemini_fallback_model: str
    gemini_timeout_seconds: int
    gemini_video_inline_max_bytes: int
    gemini_video_max_chars: int
    video_interim_after_seconds: float
    consolidate_hour: int
    consolidate_timezone: str
    consolidate_min_remaining_percent: int
    consolidate_max_input_bytes: int
    consolidate_schema_path: Path
    tracking_enabled: bool
    tracking_db_path: Path
    tracking_interval_seconds: int
    tracking_min_remaining_percent: int
    tracking_classify_interval_minutes: int
    tracking_keep_days: int
    tracking_max_per_user: int
    tracking_schema_path: Path
    tracking_reasoning_effort: str
    youtube_api_key: str
    twitch_client_id: str
    twitch_client_secret: str


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
        # Spare backend for spent ChatGPT quota or temporary Codex model capacity, written like a
        # stored model ("<backend>:<family>|<effort>"). Empty = report the failure instead.
        codex_fallback_model=values.get(
            "CODEX_FALLBACK_MODEL", "agy:gemini-3.8-flash|medium"
        ).strip(),
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
        # OpenRouter backend (third backend, free models only): no key = provider not offered.
        # Conversations are Bot-kept transcripts under OPENROUTER_DIR, replayed within a budget.
        openrouter_api_key=values.get("OPENROUTER_API_KEY", "").strip(),
        # OrcaRouter: same shape (OpenAI-compatible, free = -free ids); shares the transcript
        # dir, catalog TTL and history budget above.
        orcarouter_api_key=values.get("ORCAROUTER_API_KEY", "").strip(),
        # Operator alerts by DM: repeated failures / lost login. Empty id = the application owner.
        alert_user_id=int(values.get("ALERT_USER_ID", "").strip() or 0),
        alert_after_failures=_positive_int(values, "ALERT_AFTER_FAILURES", 3),
        alert_cooldown_minutes=_positive_int(values, "ALERT_COOLDOWN_MINUTES", 30),
        alert_login_check_minutes=_positive_int(values, "ALERT_LOGIN_CHECK_MINUTES", 60),
        # Web search tool: a keyed API first (SEARCH_API=brave + key) when configured, then the
        # self-hosted SearXNG on the compose network. Empty SEARCH_URL disables the fallback.
        search_url=values.get("SEARCH_URL", "http://searxng:8080").strip(),
        search_api=values.get("SEARCH_API", "").strip().lower(),
        search_api_key=values.get("SEARCH_API_KEY", "").strip(),
        search_max_results=_bounded_int(values, "SEARCH_MAX_RESULTS", 5, 1, 10),
        # The 👍 "remember" button uses this custom emoji when the guild has one of that name.
        remember_emoji_name=values.get("REMEMBER_EMOJI_NAME", "114514").strip(),
        # Daily backup of the Bot's own state into a bind-mounted host dir; empty = off.
        backup_dir=Path(values["BACKUP_DIR"]) if values.get("BACKUP_DIR", "").strip() else None,
        backup_hour=_bounded_int(values, "BACKUP_HOUR", 3, 0, 23),
        backup_keep_days=_positive_int(values, "BACKUP_KEEP_DAYS", 14),
        # The Bot's own log into a bind-mounted host dir, rotated at local midnight and kept
        # for LOG_KEEP_DAYS. `docker logs` only ever holds the *current* container, so without
        # this a rebuild erases the evidence of any incident. Empty LOG_DIR = stderr only.
        log_dir=Path(values["LOG_DIR"]) if values.get("LOG_DIR", "").strip() else None,
        log_keep_days=_positive_int(values, "LOG_KEEP_DAYS", 14),
        # Sandbox sidecar for <run> snippets; empty SANDBOX_URL = the tool is not offered.
        sandbox_url=values.get("SANDBOX_URL", "http://sandbox:8070").strip(),
        sandbox_timeout_seconds=_bounded_int(values, "SANDBOX_TIMEOUT_SECONDS", 30, 1, 120),
        # Registered data APIs the model may call with <api/> (config/apis.json baked in).
        apis_path=Path(values.get("APIS_FILE", "/opt/discord-codex/apis.json")),
        apis_max_chars=_positive_int(values, "APIS_MAX_CHARS", 60_000),  # schedules are long
        openrouter_dir=Path(
            values.get("OPENROUTER_DIR", "").strip()
            or str(Path(values.get("CODEX_HOME", "/var/lib/codex")) / "openrouter")
        ),
        openrouter_catalog_ttl_seconds=_positive_int(
            values, "OPENROUTER_CATALOG_TTL_SECONDS", 3600
        ),
        openrouter_history_chars=_positive_int(values, "OPENROUTER_HISTORY_CHARS", 60_000),
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
        # Discord attaches link embeds shortly after a message; wait this long once for them.
        link_preview_wait_seconds=_positive_int(values, "LINK_PREVIEW_WAIT_SECONDS", 2),
        # Gemini free tier as a video-understanding tool (no billing): empty key = disabled.
        gemini_api_key=values.get("GEMINI_API_KEY", "").strip(),
        # Lite first: it writes clean descriptions (the thinking model leaks its reasoning into
        # the text); the fuller model is the fallback when lite errors or refuses.
        gemini_model=values.get("GEMINI_MODEL", "").strip() or "gemini-3.1-flash-lite",
        gemini_fallback_model=values.get("GEMINI_FALLBACK_MODEL", "").strip()
        or "gemini-flash-latest",
        gemini_timeout_seconds=_positive_int(values, "GEMINI_TIMEOUT_SECONDS", 180),
        gemini_video_inline_max_bytes=_positive_int(
            values, "GEMINI_VIDEO_INLINE_MAX_BYTES", 12_000_000
        ),
        gemini_video_max_chars=_positive_int(values, "GEMINI_VIDEO_MAX_CHARS", 4000),
        # A video whose understanding runs past this gets a "still working" interim reply.
        video_interim_after_seconds=_positive_int(values, "VIDEO_INTERIM_AFTER_SECONDS", 8),
        # Daily memory consolidation: every scope of every guild, gated on 5h quota remaining.
        consolidate_hour=_bounded_int(values, "CONSOLIDATE_HOUR", 2, 0, 23),
        consolidate_timezone=values.get("CONSOLIDATE_TIMEZONE", "").strip() or "Asia/Taipei",
        # 0 = no gate: a spent subscription now falls back to CODEX_FALLBACK_MODEL instead of
        # failing, so holding quota back from the nightly job only delays it for no gain. Raise
        # it again to keep that much of the five-hour window for members' own questions.
        consolidate_min_remaining_percent=_bounded_int(
            values, "CONSOLIDATE_MIN_REMAINING_PERCENT", 0, 0, 100
        ),
        consolidate_max_input_bytes=_positive_int(values, "CONSOLIDATE_MAX_INPUT_BYTES", 100_000),
        consolidate_schema_path=Path(
            values.get("CONSOLIDATE_SCHEMA_FILE", "/opt/discord-codex/consolidate-schema.json")
        ),
        # Optional social-source tracking. Provider credentials stay in the Bot process; Codex
        # child processes receive the allowlisted environment from codex._safe_environment.
        tracking_enabled=_boolean(values, "TRACKING_ENABLED"),
        tracking_db_path=Path(
            values.get("TRACKING_DB_FILE", "").strip()
            or str(Path(values.get("CODEX_HOME", "/var/lib/codex")) / "tracking.sqlite3")
        ),
        tracking_interval_seconds=_positive_int(values, "TRACKING_INTERVAL_MINUTES", 15) * 60,
        # 0 = no gate, for the same reason as CONSOLIDATE_MIN_REMAINING_PERCENT above.
        tracking_min_remaining_percent=_bounded_int(
            values, "TRACKING_MIN_REMAINING_PERCENT", 0, 0, 100
        ),
        # Fetching every source stays on TRACKING_INTERVAL_MINUTES because HTTP is free; this is
        # how often a single watch may spend a classification, which is what costs quota. Each
        # watch stores its own and members can change it in words.
        tracking_classify_interval_minutes=_positive_int(
            values, "TRACKING_CLASSIFY_INTERVAL_MINUTES", 60
        ),
        # How long the judgement history is kept. Items themselves are never deleted — their
        # (source, external_id) row is what stops old content being seen as new again.
        tracking_keep_days=_positive_int(values, "TRACKING_KEEP_DAYS", 90),
        tracking_max_per_user=_positive_int(values, "TRACKING_MAX_PER_USER", 10),
        tracking_schema_path=Path(
            values.get("TRACKING_SCHEMA_FILE", "/opt/discord-codex/tracking-schema.json")
        ),
        tracking_reasoning_effort=_effort(values, "TRACKING_REASONING_EFFORT", "high"),
        youtube_api_key=values.get("YOUTUBE_API_KEY", "").strip(),
        twitch_client_id=values.get("TWITCH_CLIENT_ID", "").strip(),
        twitch_client_secret=values.get("TWITCH_CLIENT_SECRET", "").strip(),
    )
