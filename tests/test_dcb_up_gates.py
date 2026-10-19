"""deploy/orbstack dcb-up: the gates that read tool output (jq, docker inspect) fail closed, and
only non-external bridge networks are allowed (macvlan/ipvlan/external bypass DOCKER-USER)."""

from __future__ import annotations

import json
import os
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "deploy" / "orbstack" / "machine" / "usr" / "local" / "sbin" / "dcb-up"


pytestmark = pytest.mark.skipif(shutil.which("jq") is None, reason="dcb-up needs jq")

CONFIG = {
    "services": {"bot": {"restart": "no", "networks": {"default": None}}},
    "networks": {"default": {"name": "proj_default"}},
}

STUBS = {
    # JQ_FAIL makes jq fail like a broken install; otherwise it is the real jq.
    "jq": '[ -n "$JQ_FAIL" ] && exit 5\nexec "$REAL_JQ" "$@"\n',
    "flock": "exit 0\n",
    "docker": (
        'echo "docker $*" >> "$CALLS"\n'
        'case "$*" in\n'
        '  *" config --format json") [ -n "$CONFIG_WARN" ] && echo "WARN[0000] obsolete" >&2\n'
        '    cat "$COMPOSE_CONFIG" ;;\n'
        '  "ps -q "*) echo "$RUNNING" ;;\n'
        '  "ps -aq "*) echo cid1 ;;\n'
        '  "network ls "*) [ -n "$NETLS_WARN" ] && echo "WARNING: context" >&2\n'
        '    [ -n "$NETLS_FAIL" ] && { echo proj_partial; exit 1; }\n'
        "    echo proj_default ;;\n"
        '  "inspect -f "*) [ -n "$INSPECT_FAIL" ] && exit 1; echo "cid1 /bot ${POLICY:-no}" ;;\n'
        "esac\n"
    ),
}
SBIN = {
    "dcb-firewall": 'echo "firewall" >> "$CALLS"; echo "firewall unchanged"\n',
    "dcb-probe": 'echo "probe $*" >> "$CALLS"; exit 0\n',
}


def _write_stubs(directory: Path, stubs: dict[str, str]) -> None:
    directory.mkdir(parents=True)
    for name, body in stubs.items():
        (directory / name).write_text("#!/bin/sh\n" + body)
        (directory / name).chmod(0o755)


def _run(tmp_path: Path, config=None, **env_extra) -> tuple[int, list[str], str]:
    repo, logs = tmp_path / "repo", tmp_path / "logs"
    repo.mkdir()
    _write_stubs(tmp_path / "bin", STUBS)
    _write_stubs(tmp_path / "sbin", SBIN)
    (tmp_path / "compose.json").write_text(json.dumps(config or CONFIG))
    (tmp_path / "dcb").write_text(
        f"DCB_REPO={repo}\nDCB_PROJECT=proj\nDCB_LOGDIR={logs}\nDCB_STATEDIR={tmp_path / 'state'}\n"
        f"DCB_INCONCLUSIVE_STOP_AFTER=900\nDCB_SBIN={tmp_path / 'sbin'}\n"
        f"DCB_RUNDIR={tmp_path / 'run'}\nDCB_LOCK_FILE={tmp_path / 'lock'}\n"
    )
    calls = tmp_path / "calls"
    calls.touch()
    env = {
        **os.environ,
        "PATH": f"{tmp_path / 'bin'}:{os.environ['PATH']}",
        "REAL_JQ": shutil.which("jq") or "",
        "CALLS": str(calls),
        "COMPOSE_CONFIG": str(tmp_path / "compose.json"),
        "DCB_CONFIG": str(tmp_path / "dcb"),
        "RUNNING": "",
        **env_extra,
    }
    out = subprocess.run(["bash", str(SCRIPT)], env=env, capture_output=True, text=True)
    log_file = logs / "dcb-up.log"
    log = log_file.read_text() if log_file.exists() else ""
    return out.returncode, calls.read_text().splitlines(), log + out.stderr


def _started(calls: list[str]) -> list[str]:
    return [c for c in calls if " up " in c or c.endswith(" up") or " restart " in c]


def _stopped(calls: list[str]) -> bool:
    return any(c.startswith("docker stop ") for c in calls)


def test_a_clean_stack_comes_up(tmp_path) -> None:
    rc, calls, log = _run(tmp_path)
    assert rc == 0, log
    assert any(c.endswith(" up -d") for c in calls), calls


def test_a_failing_jq_does_not_pass_the_restart_policy_gate(tmp_path) -> None:
    rc, calls, log = _run(tmp_path, JQ_FAIL="1", RUNNING="cid1")
    assert rc == 1, log
    assert "cannot read the compose config (jq failed)" in log, log
    assert not _started(calls), calls
    assert _stopped(calls), calls  # a running stack is stopped, not left on an unverified config


def test_a_failing_docker_inspect_does_not_pass_the_container_gate(tmp_path) -> None:
    rc, calls, log = _run(tmp_path, INSPECT_FAIL="1", RUNNING="cid1")
    assert rc == 1, log
    assert "cannot read the containers' restart policies" in log, log
    assert _stopped(calls), calls


def test_a_container_restart_policy_other_than_no_still_stops_the_stack(tmp_path) -> None:
    rc, calls, log = _run(tmp_path, POLICY="always", RUNNING="cid1")
    assert rc == 1 and 'container restart policy is not "no"' in log, log
    assert _stopped(calls), calls


@pytest.mark.parametrize(
    ("network", "expect"),
    [
        ({"name": "proj_default", "external": True}, "default(external=true driver=bridge)"),
        ({"name": "proj_default", "driver": "macvlan"}, "default(external=false driver=macvlan)"),
        ({"name": "proj_default", "driver": "ipvlan"}, "driver=ipvlan"),
    ],
    ids=["external", "macvlan", "ipvlan"],
)
def test_an_external_or_non_bridge_network_refuses_to_start(tmp_path, network, expect) -> None:
    rc, calls, log = _run(
        tmp_path, config={**CONFIG, "networks": {"default": network}}, RUNNING="cid1"
    )
    assert rc == 1, log
    assert "external or not driver bridge" in log and expect in log, log
    assert not _started(calls), calls
    assert not [c for c in calls if c.startswith("probe")], calls
    assert _stopped(calls), calls


@pytest.mark.parametrize(
    "network", [{"name": "proj_default"}, {"name": "x", "driver": "bridge"}, {}]
)
def test_default_and_bridge_networks_proceed(tmp_path, network) -> None:
    rc, calls, log = _run(tmp_path, config={**CONFIG, "networks": {"default": network}})
    assert rc == 0, log
    assert any(c.endswith(" up -d") for c in calls), calls


@pytest.mark.parametrize("mode", ["host", "container:other", "none"])
def test_a_service_with_network_mode_refuses_to_start(tmp_path, mode) -> None:
    services = {"bot": {"restart": "no", "network_mode": mode}}
    rc, calls, log = _run(tmp_path, config={**CONFIG, "services": services}, RUNNING="cid1")
    assert rc == 1 and f"bot(network_mode={mode})" in log, log
    assert not _started(calls), calls
    assert _stopped(calls), calls


def test_a_network_list_that_fails_is_an_inconclusive_probe_never_a_partial_one(tmp_path) -> None:
    rc, calls, log = _run(tmp_path, NETLS_FAIL="1", RUNNING="cid1")
    assert rc == 1 and "probe inconclusive" in log and "cannot list the project networks" in log, (
        log
    )
    assert not [c for c in calls if c.startswith("probe")], calls  # proj_partial is never probed
    assert not any(c.endswith(" up -d") for c in calls), calls
    assert (tmp_path / "run" / "probe-inconclusive-since").exists()  # timed like any inconclusive


def test_a_warning_on_stderr_is_never_probed_as_a_network(tmp_path) -> None:
    rc, calls, log = _run(tmp_path, NETLS_WARN="1")
    assert rc == 0, log
    assert [c for c in calls if c.startswith("probe")] == ["probe proj_default"], calls


def test_a_compose_warning_on_stderr_does_not_stop_the_stack(tmp_path) -> None:
    rc, calls, log = _run(tmp_path, CONFIG_WARN="1", RUNNING="cid1")
    assert rc == 0, log
    assert not _stopped(calls), calls
