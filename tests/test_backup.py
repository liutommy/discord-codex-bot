from __future__ import annotations

import io
import sqlite3
import tarfile
import time
import zipfile
from dataclasses import replace
from pathlib import Path

from discord_codex_bot.backup import (
    ARCHIVE_PREFIX,
    export_memory_zip,
    make_backup,
    prune_backups,
    run_backup,
)


def _home(tmp_path: Path) -> Path:
    home = tmp_path / "codex"
    (home / "memory" / "1" / "users" / "2" / "topics").mkdir(parents=True)
    (home / "memory" / "1" / "users" / "2" / "MEMORY.md").write_text("- [x](x.md) — y", "utf-8")
    (home / "memory" / "1" / "users" / "2" / "topics" / "x.md").write_text("# x\n\nbody", "utf-8")
    (home / "openrouter").mkdir()
    (home / "openrouter" / "or-1.json").write_text("{}", "utf-8")
    (home / "discord_threads.json").write_text("{}", "utf-8")
    with sqlite3.connect(home / "tracking.sqlite3") as connection:
        connection.execute("CREATE TABLE watches(id INTEGER PRIMARY KEY)")
        connection.execute("INSERT INTO watches VALUES (1)")
    (home / "auth.json").write_text("SECRET", "utf-8")  # Codex's own: must never be copied
    return home


def test_make_backup_archives_the_bots_state_only(tmp_path: Path, config) -> None:
    home = _home(tmp_path)
    cfg = replace(
        config,
        codex_home=home,
        backup_dir=tmp_path / "backups",
        tracking_db_path=home / "tracking.sqlite3",
    )
    target = make_backup(cfg, now=1_700_000_000)
    assert target is not None and target.name.startswith(ARCHIVE_PREFIX)
    with tarfile.open(target) as tar:
        names = sorted(tar.getnames())
    assert "memory/1/users/2/topics/x.md" in names and "openrouter/or-1.json" in names
    assert "discord_threads.json" in names and not any("auth" in n for n in names)
    assert "tracking.sqlite3" in names
    assert not list((tmp_path / "backups").glob("*.tmp"))
    assert make_backup(replace(cfg, backup_dir=None)) is None
    empty = tmp_path / "empty"
    empty_config = replace(
        cfg, codex_home=empty, tracking_db_path=empty / "tracking.sqlite3"
    )
    assert make_backup(empty_config) is None


def test_prune_keeps_recent_archives(tmp_path: Path, config) -> None:
    cfg = replace(config, backup_dir=tmp_path / "b", backup_keep_days=2)
    cfg.backup_dir.mkdir()
    old = cfg.backup_dir / f"{ARCHIVE_PREFIX}old.tar.gz"
    new = cfg.backup_dir / f"{ARCHIVE_PREFIX}new.tar.gz"
    other = cfg.backup_dir / "unrelated.tar.gz"
    for p in (old, new, other):
        p.write_bytes(b"x")
    now = time.time()
    import os

    os.utime(old, (now - 3 * 86400, now - 3 * 86400))
    os.utime(other, (now - 30 * 86400, now - 30 * 86400))
    assert prune_backups(cfg, now=now) == 1
    assert not old.exists() and new.exists() and other.exists()
    assert prune_backups(replace(cfg, backup_dir=None)) == 0


def test_run_backup_reports(tmp_path: Path, config) -> None:
    home = _home(tmp_path)
    cfg = replace(
        config,
        codex_home=home,
        backup_dir=tmp_path / "b",
        tracking_db_path=home / "tracking.sqlite3",
    )
    assert run_backup(cfg).startswith("backup written: ")
    assert "skipped" in run_backup(replace(cfg, backup_dir=None))


def test_export_memory_zip_packs_a_members_files(tmp_path: Path) -> None:
    home = _home(tmp_path)
    root = home / "memory" / "1" / "users" / "2"
    (root / ".backup").mkdir()
    (root / ".backup" / "old.md").write_text("x", "utf-8")
    data = export_memory_zip(root, "memory-1-2")
    with zipfile.ZipFile(io.BytesIO(data)) as archive:
        assert sorted(archive.namelist()) == ["memory-1-2/MEMORY.md", "memory-1-2/topics/x.md"]
    assert export_memory_zip(tmp_path / "nope", "l") is None
    empty = tmp_path / "empty"
    empty.mkdir()
    assert export_memory_zip(empty, "l") is None
