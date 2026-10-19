"""deploy/orbstack dcb-data-refresh: the daily data refresh runs the one-shot data-refresh service
behind the same firewall and probe gates as dcb-up, and never rebuilds or restarts the bot."""

from __future__ import annotations

import json
import os
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "deploy" / "orbstack" / "machine" / "usr" / "local" / "sbin" / "dcb-data-refresh"

pytestmark = pytest.mark.skipif(shutil.which("jq") is None, reason="dcb-data-refresh needs jq")

CONFIG = {
    "services": {
        "bot": {"restart": "no", "networks": {"default": None}},
        "data-refresh": {"restart": "no", "networks": {"default": None}},
    },
    "networks": {"default": {"name": "proj_default"}},
}

STUBS = {
    "docker": (
        'echo "docker $*" >> "$CALLS"\n'
        'case "$*" in\n'
        '  *" config --format json") [ -n "$CONFIG_WARN" ] && echo "WARN[0000] obsolete" >&2\n'
        '    cat "$COMPOSE_CONFIG" ;;\n'
        '  "network inspect "*) [ "$3" = proj_default ] || [ "$3" = proj_backend ] ;;\n'
        '  *" run --rm data-refresh")\n'
        '    echo "lol-names unchanged"; echo "${RUN_OUT:-data-refresh: changed}" ;;\n'
        "esac\n"
    ),
    "flock": "exit 0\n",
}
SBIN = {
    "dcb-firewall": 'echo "firewall" >> "$CALLS"; exit "${FW_RC:-0}"\n',
    "dcb-probe": 'echo "probe $*" >> "$CALLS"; exit "${PROBE_RC:-0}"\n',
}


def _write_stubs(directory: Path, stubs: dict[str, str]) -> None:
    directory.mkdir(parents=True)
    for name, body in stubs.items():
        (directory / name).write_text("#!/bin/sh\n" + body)
        (directory / name).chmod(0o755)


def _run(
    tmp_path: Path, *, hold=False, config=None, wrap_jq=False, **env_extra
) -> tuple[int, list[str], str]:
    repo, logs = tmp_path / "repo", tmp_path / "logs"
    repo.mkdir()
    if hold:
        (repo / ".hold").touch()
    _write_stubs(tmp_path / "bin", STUBS)
    _write_stubs(tmp_path / "sbin", SBIN)
    if wrap_jq:  # fails like a broken jq when JQ_FAIL is set, else the real one
        (tmp_path / "bin" / "jq").write_text(
            '#!/bin/sh\n[ -n "$JQ_FAIL" ] && exit 5\nexec "$REAL_JQ" "$@"\n'
        )
        (tmp_path / "bin" / "jq").chmod(0o755)
    (tmp_path / "compose.json").write_text(json.dumps(config or CONFIG))
    (tmp_path / "dcb").write_text(
        f"DCB_REPO={repo}\nDCB_PROJECT=proj\nDCB_LOGDIR={logs}\n"
        f"DCB_SBIN={tmp_path / 'sbin'}\nDCB_LOCK_FILE={tmp_path / 'lock'}\n"
    )
    calls = tmp_path / "calls"
    calls.touch()
    env = {
        **os.environ,
        "PATH": f"{tmp_path / 'bin'}:{os.environ['PATH']}",
        "CALLS": str(calls),
        "COMPOSE_CONFIG": str(tmp_path / "compose.json"),
        "DCB_CONFIG": str(tmp_path / "dcb"),
        **env_extra,
    }
    out = subprocess.run(["bash", str(SCRIPT)], env=env, capture_output=True, text=True)
    log = (logs / "data-refresh.log").read_text() if (logs / "data-refresh.log").exists() else ""
    return out.returncode, calls.read_text().splitlines(), log + out.stderr


def test_it_runs_the_refresh_service_behind_the_firewall_and_the_probe(tmp_path) -> None:
    rc, calls, log = _run(tmp_path)
    repo = tmp_path / "repo"
    run = (
        f"docker compose --project-directory {repo} -p proj --profile refresh run --rm data-refresh"
    )
    assert rc == 0, log
    assert calls.index("firewall") < calls.index("probe proj_default") < calls.index(run), calls
    assert not [c for c in calls if " build" in c or " restart" in c or " up " in c], calls
    assert log.splitlines()[-1].endswith("data-refresh: changed"), log


def test_under_hold_it_does_nothing(tmp_path) -> None:
    rc, calls, log = _run(tmp_path, hold=True)
    assert rc == 0, log
    assert calls == [], calls
    assert ".hold present" in log


@pytest.mark.parametrize(
    ("env", "config", "expect"),
    [
        ({"FW_RC": "1"}, None, "firewall FAILED"),
        ({"PROBE_RC": "1"}, None, "probe failed"),
        ({"PROBE_RC": "2"}, None, "probe failed"),
        (
            {},
            {
                **CONFIG,
                "services": {"data-refresh": {"restart": "always", "networks": {"default": None}}},
            },
            "for: data-refresh=always",
        ),
        (
            {},
            {
                **CONFIG,
                "services": {"data-refresh": {"restart": "no", "networks": {"other": None}}},
            },
            "network other does not exist",
        ),
    ],
    ids=["firewall-fails", "probe-leak", "probe-inconclusive", "restart-policy", "network-missing"],
)
def test_a_failed_gate_skips_the_run(tmp_path, env, config, expect) -> None:
    rc, calls, log = _run(tmp_path, config=config, **env)
    assert rc == 1, log
    assert expect in log, log
    assert not [c for c in calls if " run " in c], calls


def _with_service(name: str, **service) -> dict:
    return {**CONFIG, "services": {**CONFIG["services"], name: service}}


@pytest.mark.parametrize("restart", ["always", "unless-stopped", "on-failure", None])
def test_any_service_of_the_profile_expanded_config_needs_restart_no(tmp_path, restart) -> None:
    # The profiled data-refresh itself is fine; another service (here the bot) is not.
    config = {
        **CONFIG,
        "services": {
            "bot": {"networks": {"default": None}, **({"restart": restart} if restart else {})},
            "data-refresh": {"restart": "no", "networks": {"default": None}},
        },
    }
    rc, calls, log = _run(tmp_path, config=config)
    assert rc == 1, log
    assert "restart policy is not" in log and "bot=" in log, log
    assert not [c for c in calls if " run " in c or c.startswith("probe")], calls


def test_a_third_service_with_a_restart_policy_blocks_the_run(tmp_path) -> None:
    config = _with_service("xsearch", restart="always", networks={"default": None})
    rc, calls, log = _run(tmp_path, config=config)
    assert rc == 1 and "xsearch=always" in log, log
    assert not [c for c in calls if " run " in c], calls


def test_the_probe_covers_every_compose_network_not_only_the_refresh_one(tmp_path) -> None:
    config = {
        "services": {
            "bot": {"restart": "no", "networks": {"backend": None}},
            "data-refresh": {"restart": "no", "networks": {"default": None}},
        },
        "networks": {"default": {"name": "proj_default"}, "backend": {"name": "proj_backend"}},
    }
    rc, calls, log = _run(tmp_path, config=config)
    assert rc == 0, log
    assert "probe proj_backend proj_default" in calls, calls


@pytest.mark.parametrize("probe_rc", ["1", "2", "127"])
def test_a_probe_result_other_than_ok_never_reaches_the_run(tmp_path, probe_rc) -> None:
    rc, calls, log = _run(tmp_path, PROBE_RC=probe_rc)
    assert rc == 1 and "probe failed" in log, log
    assert "firewall" in calls and not [c for c in calls if " run " in c], calls


@pytest.mark.parametrize(
    ("out", "ok"),
    [
        ("data-refresh: unchanged; 2 sources, 0 updated", True),
        ("data-refresh: changed lol-names", True),
        ("data-refresh: failed; one source down", False),
        ("nothing to report", False),
        ("not data-refresh: changed", False),
    ],
)
def test_only_a_changed_or_unchanged_summary_counts_as_a_refresh(tmp_path, out, ok) -> None:
    rc, _, log = _run(tmp_path, RUN_OUT=out)
    assert (rc == 0) is ok, log
    if ok:
        assert log.splitlines()[-1].endswith(out), log
    else:
        assert "without a changed/unchanged summary" in log, log


@pytest.mark.parametrize(
    ("network", "expect"),
    [
        ({"name": "proj_default", "external": True}, "default(external=true driver=bridge)"),
        ({"name": "proj_default", "driver": "macvlan"}, "default(external=false driver=macvlan)"),
        ({"name": "proj_default", "driver": "ipvlan"}, "driver=ipvlan"),
    ],
    ids=["external", "macvlan", "ipvlan"],
)
def test_a_non_bridge_or_external_network_skips_the_run(tmp_path, network, expect) -> None:
    rc, calls, log = _run(tmp_path, config={**CONFIG, "networks": {"default": network}})
    assert rc == 1 and "external or not driver bridge" in log and expect in log, log
    assert not [c for c in calls if " run " in c or c.startswith("probe")], calls


def test_an_explicit_bridge_network_is_fine(tmp_path) -> None:
    config = {**CONFIG, "networks": {"default": {"name": "proj_default", "driver": "bridge"}}}
    rc, _, log = _run(tmp_path, config=config)
    assert rc == 0, log


def test_a_jq_that_fails_never_reads_as_no_findings(tmp_path) -> None:
    rc, calls, log = _run(tmp_path, JQ_FAIL="1", REAL_JQ=shutil.which("jq"), wrap_jq=True)
    assert rc == 1 and "cannot read the compose config" in log, log
    assert not [c for c in calls if " run " in c or c.startswith("probe")], calls


def test_a_service_with_network_mode_host_skips_the_run(tmp_path) -> None:
    services = {**CONFIG["services"], "data-refresh": {"restart": "no", "network_mode": "host"}}
    rc, calls, log = _run(tmp_path, config={**CONFIG, "services": services})
    assert rc == 1 and "data-refresh(network_mode=host)" in log, log
    assert not [c for c in calls if " run " in c or c.startswith("probe")], calls


def test_a_compose_warning_on_stderr_does_not_skip_the_run(tmp_path) -> None:
    rc, calls, log = _run(tmp_path, CONFIG_WARN="1")
    assert rc == 0, log
    assert log.splitlines()[-1].endswith("data-refresh: changed"), log
