"""scripts/daily_rebuild.sh bakes a data change into the image and never commits it: a local
commit on the deployment checkout diverged it from origin."""

from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

STUBS = {
    # uv run python scripts/<name>.py: the ClearURLs fetch changes its file
    "uv": '[ "${3##*/}" = fetch_clearurls.py ] && echo changed >> config/clearurls.json; echo ok\n',
    "docker": (
        'echo "docker $*" >> "$CALLS"\n'
        'case "$*" in "compose ps -q bot") echo cid ;; inspect*) echo healthy ;; esac\n'
    ),
    "git": 'echo "git $*" >> "$CALLS"\n',
    "sleep": "",
}


def _run(tmp_path: Path, hold: bool = False) -> list[str]:
    repo, home = tmp_path / "repo", tmp_path / "home"
    (repo / "scripts").mkdir(parents=True)
    (repo / "config").mkdir()
    shutil.copy(ROOT / "scripts" / "daily_rebuild.sh", repo / "scripts")
    for name in ("lol-names.json", "clearurls.json"):
        (repo / "config" / name).write_text("{}\n")
    (repo / "permanent" / "topics").mkdir(parents=True)
    (repo / "permanent" / "topics" / "英雄譯名對照.md").write_text("x\n")
    if hold:
        (repo / ".hold").touch()
    bin_dir = home / ".local" / "bin"
    bin_dir.mkdir(parents=True)
    for name, body in STUBS.items():
        (bin_dir / name).write_text("#!/bin/sh\n" + body)
        (bin_dir / name).chmod(0o755)
    calls = tmp_path / "calls"
    calls.touch()
    env = {**os.environ, "HOME": str(home), "CALLS": str(calls)}
    out = subprocess.run(
        ["bash", str(repo / "scripts" / "daily_rebuild.sh")],
        env=env,
        capture_output=True,
        text=True,
        check=True,
    )
    assert "data changed" in out.stdout, out.stdout + out.stderr
    return calls.read_text().splitlines()


def test_a_data_change_rebuilds_without_committing(tmp_path) -> None:
    calls = _run(tmp_path)
    assert "docker compose up -d --build bot" in calls
    assert not [call for call in calls if call.startswith("git ")], calls


def test_under_hold_it_only_builds_and_still_does_not_commit(tmp_path) -> None:
    calls = _run(tmp_path, hold=True)
    assert "docker compose build bot" in calls
    assert not [call for call in calls if call.startswith(("git ", "docker compose up"))], calls
