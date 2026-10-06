from __future__ import annotations

import io
import os
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
    empty_config = replace(cfg, codex_home=empty, tracking_db_path=empty / "tracking.sqlite3")
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


def test_backup_survives_memory_being_rewritten_meanwhile(
    tmp_path: Path, config, monkeypatch
) -> None:
    # After a long VM pause consolidation (02:00) and the backup (03:00) start together; a file
    # replaced between the walk and its read must not fail the whole archive.
    home = _home(tmp_path)
    topic_dir = home / "memory" / "1" / "users" / "2" / "topics"
    (topic_dir / ".x.md.0a1b2c3d.tmp").write_text("half a rewrite", "utf-8")
    (topic_dir / "gone.md").write_text("replaced meanwhile", "utf-8")
    cfg = replace(
        config,
        codex_home=home,
        backup_dir=tmp_path / "backups",
        tracking_db_path=home / "tracking.sqlite3",
    )
    from discord_codex_bot import backup

    def opener(name, *args, **kwargs):  # gone.md is replaced after the walk listed it
        if Path(name).name == "gone.md":
            raise FileNotFoundError(name)
        return open(name, *args, **kwargs)

    monkeypatch.setattr(backup, "open", opener, raising=False)
    target = make_backup(cfg, now=1_700_000_000)
    assert target is not None
    with tarfile.open(target) as tar:
        names = tar.getnames()
    assert "memory/1/users/2/topics/x.md" in names and "openrouter/or-1.json" in names
    assert not any(n.endswith(".tmp") or n.endswith("gone.md") for n in names)


def test_memory_export_leaves_out_scratch_files(tmp_path: Path) -> None:
    root = tmp_path / "m"
    (root / "topics").mkdir(parents=True)
    (root / "MEMORY.md").write_text("- [x](x.md) — y", "utf-8")
    (root / "topics" / ".x.md.0a1b2c3d.tmp").write_text("half", "utf-8")
    with zipfile.ZipFile(io.BytesIO(export_memory_zip(root, "me"))) as archive:
        assert archive.namelist() == ["me/MEMORY.md"]


def test_backup_takes_size_and_bytes_from_the_same_file(
    tmp_path: Path, config, monkeypatch
) -> None:
    # os.replace between tarfile's stat of the name and its open: the old code archived the
    # new file under the old size ("unexpected end of data", or a truncated copy).
    home = _home(tmp_path)
    topic = home / "memory" / "1" / "users" / "2" / "topics" / "x.md"
    longer = tmp_path / "longer.md"
    longer.write_text("# x\n\nbody rewritten by consolidation, longer than before", "utf-8")
    real_lstat = os.lstat

    def lstat(path, *args, **kwargs):  # the name still pointed at the old, shorter file
        return real_lstat(longer if Path(path) == topic else path, *args, **kwargs)

    monkeypatch.setattr(tarfile.os, "lstat", lstat)
    cfg = replace(
        config,
        codex_home=home,
        backup_dir=tmp_path / "backups",
        tracking_db_path=home / "tracking.sqlite3",
    )
    target = make_backup(cfg, now=1_700_000_000)
    with tarfile.open(target) as tar:
        member = tar.extractfile("memory/1/users/2/topics/x.md").read()
    assert member == topic.read_bytes()


def test_backup_includes_grok_transcripts_wherever_grok_dir_is(tmp_path: Path, config) -> None:
    # Grok is the default provider; its gk- conversations live in GROK_DIR, which may sit
    # outside CODEX_HOME. Missing them means a restore drops every Grok thread (Codex on PR #3).
    home = _home(tmp_path)
    grok_dir = tmp_path / "elsewhere" / "grok"
    grok_dir.mkdir(parents=True)
    (grok_dir / "gk-1.json").write_text("{}", "utf-8")
    cfg = replace(
        config,
        codex_home=home,
        grok_dir=grok_dir,
        backup_dir=tmp_path / "backups",
        tracking_db_path=home / "tracking.sqlite3",
    )
    target = make_backup(cfg, now=1_700_000_000)
    with tarfile.open(target) as tar:
        assert "grok/gk-1.json" in tar.getnames()
