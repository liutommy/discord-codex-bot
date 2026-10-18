"""deploy/orbstack dcb-guard: the wake request lists every problem kind seen on each run for
DCB_WAKE_AFTER seconds, and nothing while .hold exists. The rest of dcb-guard needs root, iptables
and /run, so the bookkeeping functions are exercised on their own."""

from __future__ import annotations

import re
import subprocess
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
GUARD = ROOT / "deploy" / "orbstack" / "machine" / "usr" / "local" / "sbin" / "dcb-guard"


def _bookkeeping() -> str:
    text = GUARD.read_text()
    start = text.index('wake_dir="$DCB_STATEDIR/problems"')
    end = text.index("\n}\n", text.index("wake_update() {")) + 3
    return text[start:end]


def _run(tmp_path: Path, commands: str, *, hold: bool = False) -> str | None:
    state, repo = tmp_path / "state", tmp_path / "repo"
    state.mkdir(exist_ok=True)
    repo.mkdir(exist_ok=True)
    (repo / ".hold").unlink(missing_ok=True)
    if hold:
        (repo / ".hold").touch()
    env = {
        "PATH": "/usr/bin:/bin",
        "DCB_STATEDIR": str(state),
        "DCB_REPO": str(repo),
        "DCB_WAKE_AFTER": "600",
    }
    script = "set -uo pipefail\n" + _bookkeeping() + "\n" + commands
    subprocess.run(["bash", "-c", script], env=env, check=True, capture_output=True, text=True)
    request = state / "wake-request"
    return request.read_text() if request.exists() else None


def _seen_since(tmp_path: Path, kind: str, minutes: int) -> None:
    problems = tmp_path / "state" / "problems"
    problems.mkdir(parents=True, exist_ok=True)
    (problems / kind).write_text(f"{int(time.time()) - minutes * 60}\n")


def test_a_new_problem_waits_before_it_requests_a_wake(tmp_path) -> None:
    assert _run(tmp_path, 'problem firewall "dcb-firewall: FAILED: x"; wake_update') is None
    assert (tmp_path / "state" / "problems" / "firewall").exists()


def test_a_problem_seen_for_ten_minutes_requests_a_wake_until_it_clears(tmp_path) -> None:
    _seen_since(tmp_path, "firewall", 11)
    request = _run(tmp_path, 'problem firewall "dcb-firewall: FAILED: x"; wake_update')
    assert request == "firewall 11min dcb-firewall: FAILED: x\n"

    assert _run(tmp_path, "no_problem firewall; wake_update") is None
    assert not (tmp_path / "state" / "problems" / "firewall").exists()


def test_a_kind_not_checked_this_run_keeps_its_time_but_is_not_listed(tmp_path) -> None:
    _seen_since(tmp_path, "firewall", 12)
    _seen_since(tmp_path, "gateway", 30)
    request = _run(tmp_path, 'problem firewall "fw"; wake_update')
    assert request == "firewall 12min fw\n"
    assert (tmp_path / "state" / "problems" / "gateway").exists()

    request = _run(tmp_path, 'problem firewall "fw"; problem gateway "5 lines"; wake_update')
    assert request == "firewall 12min fw\ngateway 30min 5 lines\n"

    # The next run exits before the gateway check: last run's gateway evidence is not reused.
    assert _run(tmp_path, 'problem firewall "fw"; wake_update') == "firewall 12min fw\n"


def test_no_wake_request_while_held(tmp_path) -> None:
    _seen_since(tmp_path, "gave-up", 60)
    (tmp_path / "state").mkdir(exist_ok=True)
    (tmp_path / "state" / "wake-request").write_text("gave-up 58min x\n")
    assert _run(tmp_path, 'problem gave-up "since x"; wake_update', hold=True) is None
    assert list((tmp_path / "state" / "problems").iterdir()) == []


def test_every_exec_and_exit_after_the_checks_updates_the_wake_request_first() -> None:
    lines = GUARD.read_text().splitlines()
    start = next(i for i, line in enumerate(lines) if line.startswith("wake_update() {"))
    for i in range(start + 1, len(lines)):
        line = lines[i]
        if re.search(r"\bexec /usr/local/sbin/dcb-up\b|\bexit 0\b", line):
            context = " ".join(lines[max(0, i - 1) : i + 1])
            assert "wake_update" in context, f"line {i + 1}: {line.strip()}"
