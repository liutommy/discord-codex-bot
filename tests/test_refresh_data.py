"""scripts/refresh_data.py (the compose `data-refresh` service) writes the generated data into
the volume atomically, only after the Bot's own parser accepts it, and keeps the ClearURLs and
name-table failure semantics; compose runs it with the Bot's hardening and none of its state."""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from scripts import build_lol_names, fetch_clearurls, refresh_data

ROOT = Path(__file__).resolve().parents[1]
RULES = json.dumps({"providers": {"x": {"urlPattern": "^https://x"}}}) + "\n"
NAMES = {
    "topics/英雄譯名對照.md": "| 逆命 | 崔斯特 |\n",
    "topics/海克斯譯名對照.md": "| 靈光一閃 | 尤里卡 |\n",
    "topics/裝備譯名對照.md": "| 無盡之刃 | 无尽之刃 |\n",
    "lol-names.json": json.dumps({"names": {"崔斯特": "逆命"}, "paths": {"hero/逆命": "hero/4"}}),
}


@pytest.fixture
def sources(monkeypatch):
    """Both fetchers stubbed; tests swap in failures."""
    state = {"rules": (RULES, "1 providers (0 dropped)"), "names": (dict(NAMES), "champions 1")}

    def build(key):
        def run():
            value = state[key]
            if isinstance(value, Exception):
                raise value
            return value

        return run

    monkeypatch.setattr(fetch_clearurls, "build", build("rules"))
    monkeypatch.setattr(build_lol_names, "build", build("names"))
    return state


def test_a_first_run_writes_everything_and_a_second_changes_nothing(tmp_path, sources) -> None:
    code, line = refresh_data.refresh(tmp_path)
    assert code == 0 and line.split()[1] == "changed"
    assert "clearurls.json" in line and "lol-names.json" in line
    assert (tmp_path / "clearurls.json").read_text("utf-8") == RULES
    assert (tmp_path / "topics" / "英雄譯名對照.md").read_text("utf-8") == NAMES[
        "topics/英雄譯名對照.md"
    ]
    inode = (tmp_path / "clearurls.json").stat().st_ino
    code, line = refresh_data.refresh(tmp_path)
    assert (code, line.split()[1]) == (0, "unchanged;")
    assert (tmp_path / "clearurls.json").stat().st_ino == inode  # not even rewritten


def test_a_change_replaces_the_file_atomically(tmp_path, sources, monkeypatch) -> None:
    refresh_data.refresh(tmp_path)
    replaced: list[tuple[Path, Path]] = []
    real = os.replace
    monkeypatch.setattr(
        "discord_codex_bot.memory.os.replace",
        lambda src, dst: (replaced.append((Path(src), Path(dst))), real(src, dst)),
    )
    new_rules = RULES.replace("x", "y")
    sources["rules"] = (new_rules, "1 providers (0 dropped)")
    code, line = refresh_data.refresh(tmp_path)
    assert code == 0 and line.startswith("data-refresh: changed clearurls.json;")
    [(scratch, target)] = replaced
    assert target == tmp_path / "clearurls.json" and scratch.parent == target.parent
    assert not scratch.exists() and target.read_text("utf-8") == new_rules
    assert sorted(p.name for p in tmp_path.iterdir()) == [
        "clearurls.json",
        "lol-names.json",
        "topics",
    ]


def test_a_failed_clearurls_fetch_keeps_the_old_rules_and_still_builds_names(
    tmp_path, sources
) -> None:
    (tmp_path / "clearurls.json").write_text(RULES, "utf-8")
    sources["rules"] = OSError("network down")
    code, line = refresh_data.refresh(tmp_path)
    assert code == 0 and line.split()[1] == "changed"
    assert "clearurls failed, last good copy kept (OSError: network down)" in line
    assert (tmp_path / "clearurls.json").read_text("utf-8") == RULES
    assert (tmp_path / "lol-names.json").exists()


def test_a_volume_that_cannot_be_written_is_a_hard_failure(tmp_path, sources, monkeypatch) -> None:
    def deny(path, text):
        raise PermissionError(30, "Read-only file system")

    monkeypatch.setattr(refresh_data, "atomic_write", deny)
    code, line = refresh_data.refresh(tmp_path)
    assert code == 1 and line.startswith("data-refresh: failed;")
    assert "Read-only" in line


def test_rules_the_bot_would_refuse_are_never_written(tmp_path, sources) -> None:
    (tmp_path / "clearurls.json").write_text(RULES, "utf-8")
    sources["rules"] = ('{"providers": [', "?")  # truncated
    code, line = refresh_data.refresh(tmp_path)
    assert code == 0 and "clearurls failed" in line
    assert (tmp_path / "clearurls.json").read_text("utf-8") == RULES


def test_a_failed_name_build_fails_the_run_and_writes_no_table(tmp_path, sources) -> None:
    sources["names"] = ValueError("empty table: champions 0")
    code, line = refresh_data.refresh(tmp_path)
    assert code == 1 and line.startswith("data-refresh: failed; name tables: ValueError")
    assert not (tmp_path / "lol-names.json").exists() and not (tmp_path / "topics").exists()
    files = dict(NAMES, **{"lol-names.json": '{"names": {}, "paths": {}}'})
    sources["names"] = (files, "champions 1")  # a map the Bot would refuse
    assert refresh_data.refresh(tmp_path)[0] == 1
    assert not (tmp_path / "lol-names.json").exists()


def test_main_prints_the_summary_last_and_exits_by_it(tmp_path, sources, capsys) -> None:
    assert refresh_data.main(["--out", str(tmp_path)]) == 0
    assert capsys.readouterr().out.splitlines()[-1].startswith("data-refresh: changed ")
    sources["names"] = ValueError("x")
    assert refresh_data.main(["--out", str(tmp_path)]) == 1


def _service(name: str) -> str:
    """One service's block from compose.yaml, as text (the repo has no YAML parser)."""
    compose = (ROOT / "compose.yaml").read_text()
    block = compose.split(f"\n  {name}:\n", 1)[1]
    lines = []
    for line in block.splitlines():
        if line and not line.startswith("    "):
            break
        lines.append(line)
    return "\n".join(lines)


def test_compose_runs_the_refresh_one_shot_with_the_bots_hardening() -> None:
    refresh = _service("data-refresh")
    for line in (
        'profiles: ["refresh"]',
        "image: discord-codex-bot:local",
        'restart: "no"',
        'user: "1000:1000"',
        'entrypoint: ["python", "-m", "scripts.refresh_data"]',
        "read_only: true",
        "- ALL",
        "- no-new-privileges:true",
        "driver: json-file",
        "source: generated-data\n        target: /var/lib/discord-codex/data\n    read_only",
    ):
        assert line in refresh, line
    # nothing of the Bot's: no secrets, no state, no extra network
    assert "env_file" not in refresh and "codex-home" not in refresh and "agy-home" not in refresh
    assert refresh.split("networks:\n", 1)[1].strip() == "- default"
    bot = _service("bot")
    mount = "source: generated-data\n        target: /var/lib/discord-codex/data\n"
    assert mount + "        read_only: true" in bot
    compose = (ROOT / "compose.yaml").read_text()
    assert (
        "\n  generated-data:\n    name: ${BOT_CONTAINER_NAME:-tommy_test}_generated_data" in compose
    )


def test_the_image_seeds_a_volume_the_refresh_can_write() -> None:
    dockerfile = (ROOT / "Dockerfile").read_text()
    assert "mkdir -p /var/lib/codex /var/lib/discord-codex/data" in dockerfile
    assert "chown -R 1000:1000 /var/lib/codex /var/lib/discord-codex " in dockerfile
    # the git-tracked copies stay baked in as the floor
    assert "config/lol-names.json config/clearurls.json" in dockerfile
    assert not (ROOT / "scripts" / "daily_rebuild.sh").exists()
