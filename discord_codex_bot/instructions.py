"""The operator's persona and default output style, changeable while the Bot runs.

Both ship inside the image, whose root filesystem is mounted read-only, so an uploaded copy lives
in the Codex volume and wins for as long as it is there. Nothing is seeded: with no upload the
image copies are used exactly as before, so editing the repo and rebuilding keeps working until
someone uploads, and a reset is just deleting the uploaded file.

The persona reaches Codex as project instructions — AGENTS.md in its working directory — which
Codex reads once when a thread starts and never re-reads on resume. That composition used to
happen at image build time, which is the only reason it could not be changed at runtime; here it
happens at process start and after every change instead. Changing it must also retire every live
thread, or old threads keep answering with the old persona: see `instructions_version`.
"""

from __future__ import annotations

import logging
import os
import time
from pathlib import Path

from .config import Config

LOGGER = logging.getLogger(__name__)

PERSONA = "persona"
OUTPUT_STYLE = "output-style"
KINDS = (PERSONA, OUTPUT_STYLE)
LABELS = {PERSONA: "人設", OUTPUT_STYLE: "預設輸出風格"}
_FILES = {PERSONA: "persona.md", OUTPUT_STYLE: "output-style.md"}
# An operator file is larger than a member's personal style: the shipped persona alone is about
# 4000 characters, so that member-facing limit would reject the Bot's own default.
MAX_CHARS = 20_000
MAX_UPLOAD_BYTES = MAX_CHARS * 4 + 1024  # worst-case UTF-8 for that many characters
SUFFIXES = (".md", ".markdown")


def uploaded_path(config: Config, kind: str) -> Path:
    return config.codex_home / "instructions" / _FILES[kind]


def image_persona(config: Config) -> str:
    """Every persona file baked into the image, in name order. `README.md` is the directory's own
    documentation and `*.example.md` is the sample a public clone edits, so neither is persona."""
    try:
        files = sorted(config.persona_dir.glob("*.md"))
    except OSError:
        return ""
    parts = []
    for path in files:
        if path.name == "README.md" or path.name.endswith(".example.md"):
            continue
        try:
            parts.append(path.read_text("utf-8").strip())
        except OSError:
            LOGGER.warning("persona file unreadable: %s", path)
    return "\n\n".join(part for part in parts if part)


def persona_text(config: Config) -> str:
    try:
        return uploaded_path(config, PERSONA).read_text("utf-8").strip()
    except OSError:
        return image_persona(config)


def style_path(config: Config) -> Path:
    """Where the default output style is read from — the uploaded file while one exists."""
    uploaded = uploaded_path(config, OUTPUT_STYLE)
    return uploaded if uploaded.is_file() else config.output_style_path


def style_text(config: Config) -> str:
    try:
        return style_path(config).read_text("utf-8").strip()
    except OSError:
        return ""


def current_text(config: Config, kind: str) -> str:
    return persona_text(config) if kind == PERSONA else style_text(config)


def is_uploaded(config: Config, kind: str) -> bool:
    return uploaded_path(config, kind).is_file()


def parse_upload(filename: str, data: bytes, limit: int = MAX_CHARS) -> str:
    """The text of an uploaded instruction file. Raises ValueError carrying the sentence the
    operator should see: every rejection here is something they can fix and re-upload."""
    if not filename.lower().endswith(SUFFIXES):
        raise ValueError("只收 .md 檔。")
    try:
        text = data.decode("utf-8").strip()
    except UnicodeDecodeError:
        raise ValueError("檔案不是 UTF-8 文字，存成 UTF-8 再上傳。") from None
    if not text:
        raise ValueError("檔案是空的。")
    if len(text) > limit:
        raise ValueError(f"檔案 {len(text)} 字，超過上限 {limit} 字。")
    return text


def _write(path: Path, text: str) -> None:
    """Atomic. A thread starting mid-write must never read half an AGENTS.md."""
    path.parent.mkdir(parents=True, exist_ok=True)
    scratch = path.with_name(path.name + ".tmp")
    scratch.write_text(text, "utf-8")
    os.replace(scratch, path)


def backup(config: Config, kind: str, now: float | None = None) -> Path | None:
    """Keep what is in force right now, before it is replaced. The persona in force may be several
    image files concatenated, so the composed text is what gets kept, not whichever file it came
    from — that is the thing a rollback needs."""
    if not config.backup_dir:
        return None
    text = current_text(config, kind)
    if not text:
        return None
    stamp = time.strftime("%Y%m%d-%H%M%S", time.localtime(now if now else time.time()))
    folder = config.backup_dir / "instructions"
    target = folder / f"{kind}-{stamp}.md"
    try:
        folder.mkdir(parents=True, exist_ok=True)
        # Upload-then-reset inside one second would otherwise land on the same name and the
        # second backup would destroy the first — which is the one holding the older text.
        serial = 1
        while target.exists():
            target = folder / f"{kind}-{stamp}-{serial}.md"
            serial += 1
        target.write_text(text + "\n", "utf-8")
    except OSError:
        LOGGER.error("could not back up %s to %s", kind, target)
        return None
    return target


def save(config: Config, kind: str, text: str) -> Path | None:
    """Back up what is in force, then put the upload in its place. Returns the backup."""
    kept = backup(config, kind)
    _write(uploaded_path(config, kind), text.strip() + "\n")
    return kept


def reset(config: Config, kind: str) -> tuple[bool, Path | None]:
    """Drop the uploaded copy so the image version is used again."""
    path = uploaded_path(config, kind)
    if not path.is_file():
        return False, None
    kept = backup(config, kind)
    path.unlink(missing_ok=True)
    return True, kept


def compose_workspaces(config: Config) -> bool:
    """Write both Codex working directories from the shipped rules plus the persona in force.
    `/workspace-plain` is the rules alone — what a member gets after turning the persona off."""
    try:
        rules = config.codex_rules_path.read_text("utf-8").strip()
    except OSError:
        LOGGER.error(
            "runtime rules missing at %s; Codex working directories left as they are",
            config.codex_rules_path,
        )
        return False
    persona = persona_text(config)
    try:
        _write(config.codex_workspace_plain / "AGENTS.md", rules + "\n")
        body = f"{rules}\n\n\n{persona}\n" if persona else rules + "\n"
        _write(config.codex_workspace / "AGENTS.md", body)
    except OSError:
        LOGGER.error("could not write the Codex working directories under %s", config.codex_home)
        return False
    return True
