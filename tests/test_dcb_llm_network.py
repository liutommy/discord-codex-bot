"""The host relays' machine side: compose.yaml's llm network, the DCB-INPUT rules dcb-firewall adds
for DCB_LLM_RELAY_PORT and DCB_IMG_RELAY_PORT, and dcb-probe's subnet check. dcb-firewall and
dcb-probe need root, iptables and docker, so the pieces are exercised on their own."""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
SBIN = ROOT / "deploy" / "orbstack" / "machine" / "usr" / "local" / "sbin"
FIREWALL = SBIN / "dcb-firewall"
PROBE = SBIN / "dcb-probe"
DEFAULTS = ROOT / "deploy" / "orbstack" / "dcb.default.example"

BRIDGE, SUBNET, GATEWAY = "br-dcb-llm", "172.31.254.0/24", "172.31.254.1"


def _function(text: str, name: str) -> str:
    start = text.index(f"{name}() {{")
    return text[start : text.index("\n}\n", start) + 3]


def _firewall_rules(port: str | None, img: str | None = None) -> subprocess.CompletedProcess:
    text = FIREWALL.read_text()
    relay = text[text.index("# --- host service relays ---") : text.index("# --- WAN IP ---")]
    script = (
        "set -uo pipefail\n"
        + text[text.index("die() {") : text.index("\n", text.index("die() {")) + 1]
        + (f"DCB_LLM_RELAY_PORT={port}\n" if port is not None else "")
        + (f"DCB_IMG_RELAY_PORT={img}\n" if img is not None else "")
        + 'BLOCK4="10.0.0.0/8 172.16.0.0/12"\nwan=203.0.113.9\n'
        + "DCB_POOL_NETS=172.30.0.0/16\nDCB_BIP_NET=172.31.255.0/24\n"
        + relay
        + _function(text, "rules4")
        + "rules4 DOCKER-USER DCB-INPUT\n"
    )
    return subprocess.run(
        ["bash", "-c", script], env={"PATH": "/usr/bin:/bin"}, capture_output=True, text=True
    )


def _input_chain(out: str) -> list[str]:
    return [line for line in out.splitlines() if line.startswith("-A DCB-INPUT ")]


def test_the_relay_port_opens_only_the_gateway_port_on_the_llm_bridge() -> None:
    out = _firewall_rules("18080")
    assert out.returncode == 0, out.stderr
    rules = _input_chain(out.stdout)
    allow = (
        f"-A DCB-INPUT -i {BRIDGE} -d {GATEWAY} -p tcp --dport 18080 "
        "-m conntrack --ctstate NEW -j RETURN"
    )
    reject = "-A DCB-INPUT -i br-+ -m conntrack --ctstate NEW -j REJECT"
    assert allow in rules, rules
    i_reject = next(i for i, r in enumerate(rules) if r.startswith(reject))
    assert rules.index(allow) < i_reject, rules
    # It is the only rule that lets anything through before the rejects.
    assert [r for r in rules if r.endswith("-j RETURN")] == [allow, "-A DCB-INPUT -j RETURN"]


@pytest.mark.parametrize("port", [None, ""])
def test_without_a_relay_port_the_llm_bridge_stays_rejected(port) -> None:
    out = _firewall_rules(port)
    assert out.returncode == 0, out.stderr
    rules = _input_chain(out.stdout)
    assert not [r for r in rules if BRIDGE in r], rules
    assert rules[-1] == "-A DCB-INPUT -j RETURN" and len(rules) == 3, rules


@pytest.mark.parametrize("port", ["80", "1023", "65536", "08080", "18080x", "1 2", "-1"])
def test_a_bad_relay_port_fails_the_firewall(port) -> None:
    out = _firewall_rules(f"'{port}'")
    assert out.returncode == 1, out.stdout
    assert "DCB_LLM_RELAY_PORT must be an integer 1024-65535" in out.stderr, out.stderr
    assert "*filter" not in out.stdout


def _allow(port: str) -> str:
    return (
        f"-A DCB-INPUT -i {BRIDGE} -d {GATEWAY} -p tcp --dport {port} "
        "-m conntrack --ctstate NEW -j RETURN"
    )


def test_the_image_relay_port_adds_a_second_rule_before_the_rejects() -> None:
    out = _firewall_rules("18080", "18082")
    assert out.returncode == 0, out.stderr
    rules = _input_chain(out.stdout)
    i_reject = next(i for i, r in enumerate(rules) if r.startswith("-A DCB-INPUT -i br-+ "))
    assert rules.index(_allow("18080")) < rules.index(_allow("18082")) < i_reject, rules
    # The two relay ports are the only rules that let anything through before the rejects.
    returns = [r for r in rules if r.endswith("-j RETURN")]
    assert returns == [_allow("18080"), _allow("18082"), "-A DCB-INPUT -j RETURN"], rules


def test_the_image_relay_port_works_without_the_llm_relay_port() -> None:
    out = _firewall_rules("", "18082")
    assert out.returncode == 0, out.stderr
    rules = _input_chain(out.stdout)
    assert [r for r in rules if BRIDGE in r] == [_allow("18082")], rules


@pytest.mark.parametrize("img", [None, ""])
def test_without_an_image_relay_port_only_the_llm_rule_is_added(img) -> None:
    out = _firewall_rules("18080", img)
    assert out.returncode == 0, out.stderr
    rules = _input_chain(out.stdout)
    assert [r for r in rules if BRIDGE in r] == [_allow("18080")], rules
    assert not [r for r in rules if "18082" in r], rules


@pytest.mark.parametrize("port", ["80", "1023", "65536", "08082", "18082x", "1 2", "-1"])
def test_a_bad_image_relay_port_fails_the_firewall(port) -> None:
    out = _firewall_rules("18080", f"'{port}'")
    assert out.returncode == 1, out.stdout
    assert "DCB_IMG_RELAY_PORT must be an integer 1024-65535" in out.stderr, out.stderr
    assert "*filter" not in out.stdout


def test_the_image_relay_port_must_differ_from_the_llm_relay_port() -> None:
    out = _firewall_rules("18080", "18080")
    assert out.returncode == 1, out.stdout
    assert "DCB_IMG_RELAY_PORT must differ from DCB_LLM_RELAY_PORT" in out.stderr, out.stderr
    assert "*filter" not in out.stdout


def test_the_relay_ports_are_documented_and_empty_by_default() -> None:
    defaults = DEFAULTS.read_text()
    assert "\nDCB_LLM_RELAY_PORT=\n" in defaults
    assert "\nDCB_IMG_RELAY_PORT=\n" in defaults


def _top_level(compose: str, section: str) -> dict[str, list[str]]:
    """The entries of one top-level compose section, as their non-comment lines (no YAML parser)."""
    body = ("\n" + compose).split(f"\n{section}:\n", 1)[1]
    entries: dict[str, list[str]] = {}
    current = None
    for line in body.splitlines():
        if line and not line.startswith(" "):
            break
        if not line.strip() or line.strip().startswith("#"):
            continue
        if line.startswith("  ") and not line.startswith("   "):
            current = line.strip().rstrip(":")
            entries[current] = []
        elif current is not None:
            entries[current].append(line.split(" #", 1)[0].rstrip())
    return entries


def test_compose_llm_network_matches_the_firewall_and_probe() -> None:
    compose = (ROOT / "compose.yaml").read_text()
    assert _top_level(compose, "networks")["llm"] == [
        "    driver: bridge",
        "    driver_opts:",
        f"      com.docker.network.bridge.name: {BRIDGE}",
        "    ipam:",
        "      config:",
        f"        - subnet: {SUBNET}",
        f"          gateway: {GATEWAY}",
    ]
    services = _top_level(compose, "services")
    joined = [name for name, lines in services.items() if any("llm" in ln for ln in lines)]
    assert joined == ["bot"], joined
    bot = services["bot"]
    nets = bot[bot.index("    networks:") + 1 :]
    nets = nets[: next((i for i, ln in enumerate(nets) if not ln.startswith("      - ")), None)]
    assert nets == [f"      - {n}" for n in ("default", "sandbox_net", "xsearch_net", "llm")], nets
    # The scripts name the same bridge and addresses.
    assert f"relay_addr={GATEWAY} " in FIREWALL.read_text()
    assert f'{SUBNET}) [ "$2" = {BRIDGE} ]' in PROBE.read_text()


def _subnet_ok(subnet: str, bridge: str) -> bool:
    script = (
        "DCB_POOL_PREFIX=172.30.\n"
        + _function(PROBE.read_text(), "subnet_ok")
        + f'subnet_ok "{subnet}" "{bridge}"\n'
    )
    return subprocess.run(["bash", "-c", script], env={"PATH": "/usr/bin:/bin"}).returncode == 0


@pytest.mark.parametrize(
    ("subnet", "bridge", "ok"),
    [
        ("172.30.5.0/24", "", True),
        ("172.30.5.0/28", "br-1234", True),
        (SUBNET, BRIDGE, True),
        (SUBNET, "", False),  # the fixed subnet on any other bridge is still a leak
        (SUBNET, "br-other", False),
        ("172.31.253.0/24", BRIDGE, False),
        ("172.30.0.0/16", "", False),
        ("192.168.1.0/24", BRIDGE, False),
    ],
)
def test_the_probe_accepts_the_llm_subnet_only_on_its_bridge(subnet, bridge, ok) -> None:
    assert _subnet_ok(subnet, bridge) is ok
