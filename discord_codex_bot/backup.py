"""Backups and exports of what the Bot keeps for people. Daily, the Bot's own state (member and
server memories, router transcripts, thread map, reminders) is tarred into BACKUP_DIR — a
bind-mounted host directory — and old archives are pruned; a member can also export their own
memories with /<prefix>-export. Codex's own files (sessions, auth.json) are not ours to copy."""

from __future__ import annotations

import asyncio
import io
import logging
import sqlite3
import sys
import tarfile
import tempfile
import time
import zipfile
from datetime import datetime
from pathlib import Path

from .clock import sleep_for
from .config import Config
from .consolidate import seconds_until
from .memory import is_scratch

LOGGER = logging.getLogger(__name__)
# Relative to codex_home: everything the Bot itself wrote and would miss after a lost volume.
BACKUP_MEMBERS = (
    "memory",
    "openrouter",
    "discord_threads.json",
    "reminders.json",
    "announced.json",
)
TRACKING_DB = "tracking.sqlite3"
ARCHIVE_PREFIX = "discord-codex-bot-"


def _add_tree(tar: tarfile.TarFile, path: Path, arcname: str) -> None:
    """tar.add, but a file replaced or removed while the archive is written is skipped instead
    of failing the whole backup, and memory's scratch files are left out. Consolidation may be
    rewriting memory/ when a long VM pause makes both nightly jobs start together."""
    paths = [path, *sorted(path.rglob("*"))] if path.is_dir() else [path]
    for item in paths:
        if is_scratch(item):
            continue
        name = arcname if item == path else f"{arcname}/{item.relative_to(path).as_posix()}"
        try:
            if item.is_file() and not item.is_symlink():
                # Size and bytes from one open file: tar.add stats the name and opens it again,
                # so an os.replace in between fails the archive (shorter) or truncates (longer).
                with open(item, "rb") as handle:
                    tar.addfile(tar.gettarinfo(arcname=name, fileobj=handle), handle)
            else:
                tar.add(item, arcname=name, recursive=False)
        except FileNotFoundError:
            continue


def make_backup(config: Config, now: float | None = None) -> Path | None:
    """One tar.gz of BACKUP_MEMBERS under BACKUP_DIR; None when there is nothing or no dir."""
    if not config.backup_dir:
        return None
    present = [name for name in BACKUP_MEMBERS if (config.codex_home / name).exists()]
    tracking = config.tracking_db_path
    if not present and not tracking.exists():
        return None
    config.backup_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.fromtimestamp(now or time.time()).strftime("%Y%m%d-%H%M%S")
    target = config.backup_dir / f"{ARCHIVE_PREFIX}{stamp}.tar.gz"
    partial = target.with_suffix(".tmp")
    with tempfile.TemporaryDirectory(dir=config.backup_dir) as scratch:
        snapshot = Path(scratch) / TRACKING_DB
        if tracking.exists():
            with sqlite3.connect(tracking) as source, sqlite3.connect(snapshot) as destination:
                source.backup(destination)
        with tarfile.open(partial, "w:gz") as tar:
            for name in present:
                _add_tree(tar, config.codex_home / name, name)
            if snapshot.exists():
                tar.add(snapshot, arcname=TRACKING_DB)
    partial.replace(target)
    return target


def prune_backups(config: Config, now: float | None = None) -> int:
    """Delete archives older than BACKUP_KEEP_DAYS; return how many went."""
    if not config.backup_dir or not config.backup_dir.is_dir():
        return 0
    cutoff = (now or time.time()) - config.backup_keep_days * 86400
    removed = 0
    for path in config.backup_dir.glob(f"{ARCHIVE_PREFIX}*.tar.gz"):
        if path.stat().st_mtime < cutoff:
            path.unlink(missing_ok=True)
            removed += 1
    return removed


def run_backup(config: Config) -> str:
    target = make_backup(config)
    removed = prune_backups(config)
    if target is None:
        return "backup skipped: no backup dir or nothing to back up"
    size = target.stat().st_size
    return f"backup written: {target.name} ({size // 1024} KB); pruned {removed} old"


async def backup_forever(config: Config) -> None:
    """Daily at BACKUP_HOUR (consolidation's timezone), after consolidation has run."""
    if not config.backup_dir:
        LOGGER.info("Backups disabled (no BACKUP_DIR)")
        return
    while True:
        await sleep_for(seconds_until(config.backup_hour, config.consolidate_timezone))
        try:
            LOGGER.info("%s", await asyncio.to_thread(run_backup, config))
        except Exception:
            LOGGER.exception("Backup failed")


def export_memory_zip(root: Path, label: str) -> bytes | None:
    """A member's memory directory (index, archive, topics) as zip bytes; None when empty."""
    if not root.is_dir():
        return None
    buffer = io.BytesIO()
    count = 0
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as archive:
        for path in sorted(root.rglob("*")):
            if ".backup" in path.parts or is_scratch(path) or not path.is_file():
                continue
            try:
                data = path.read_bytes()  # one read: stat-then-open could mix two versions
            except FileNotFoundError:  # replaced or removed while zipping
                continue
            archive.writestr(f"{label}/{path.relative_to(root)}", data)
            count += 1
    return buffer.getvalue() if count else None


if __name__ == "__main__":
    from .config import load_config

    logging.basicConfig(level=logging.INFO, stream=sys.stderr)
    print(run_backup(load_config()))
