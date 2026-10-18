"""Generated data reaches the running Bot without a restart: a replaced file is reloaded, a broken
one never displaces the last good copy, and the image's copy is the floor."""

from __future__ import annotations

import json
import logging
import os
from pathlib import Path

import pytest

from discord_codex_bot import apis, clearurls
from discord_codex_bot.datafiles import LiveFile, data_dir
from discord_codex_bot.memory import MemoryLimits, PermanentMemory


def _replace(path: Path, text: str) -> None:
    """How data-refresh writes: a scratch file renamed over the old one."""
    path.parent.mkdir(parents=True, exist_ok=True)
    scratch = path.with_name(f".{path.name}.tmp")
    scratch.write_text(text, "utf-8")
    os.replace(scratch, path)


def _parse(text: str) -> dict:
    data = json.loads(text)
    if not isinstance(data, dict):
        raise ValueError("not an object")
    return data


def _live(volume: Path, baked: Path, interval: float = 0) -> LiveFile[dict]:
    return LiveFile("test data", [volume, baked], _parse, {}, interval=interval)


def test_a_replaced_file_is_reloaded_without_a_restart(tmp_path: Path, caplog) -> None:
    volume, baked = tmp_path / "data" / "x.json", tmp_path / "image" / "x.json"
    _replace(volume, '{"v": 1}')
    live = _live(volume, baked)
    caplog.set_level(logging.INFO, logger="discord_codex_bot.datafiles")
    assert live.get() == {"v": 1}
    _replace(volume, '{"v": 2}')  # same size and, often, the same mtime: the inode differs
    assert live.get() == {"v": 2}
    assert live.get() == {"v": 2}
    loads = [r for r in caplog.records if "loaded" in r.getMessage()]
    assert len(loads) == 2  # one info line per new version, none for an unchanged file


def test_a_broken_file_keeps_the_last_good_copy_and_warns_once(tmp_path: Path, caplog) -> None:
    volume, baked = tmp_path / "data" / "x.json", tmp_path / "image" / "x.json"
    _replace(baked, '{"v": "baked"}')
    _replace(volume, '{"v": 1}')
    live = _live(volume, baked)
    assert live.get() == {"v": 1}
    caplog.set_level(logging.WARNING, logger="discord_codex_bot.datafiles")
    _replace(volume, '{"v": 2')  # truncated
    assert live.get() == {"v": 1}  # neither the broken copy nor the older baked one
    assert live.get() == {"v": 1}
    assert len([r for r in caplog.records if "unusable" in r.getMessage()]) == 1
    _replace(volume, '{"v": 3}')
    assert live.get() == {"v": 3}  # the next good version is taken up again


def test_the_baked_copy_is_the_floor(tmp_path: Path) -> None:
    volume, baked = tmp_path / "data" / "x.json", tmp_path / "image" / "x.json"
    _replace(baked, '{"v": "baked"}')
    live = _live(volume, baked)
    assert live.get() == {"v": "baked"}  # a fresh volume
    _replace(volume, "[1, 2]")  # wrong shape, and nothing loaded from the volume before
    assert live.get() == {"v": "baked"}
    _replace(volume, '{"v": 1}')
    assert live.get() == {"v": 1}
    volume.unlink()  # the volume copy disappears: the last good value still stands
    assert live.get() == {"v": 1}


def test_a_deleted_source_keeps_the_last_good_value_and_warns_once(tmp_path, caplog) -> None:
    volume, baked = tmp_path / "data" / "x.json", tmp_path / "image" / "x.json"
    _replace(baked, '{"v": "baked"}')
    _replace(volume, '{"v": 1}')
    live = _live(volume, baked)
    assert live.get() == {"v": 1}
    caplog.set_level(logging.WARNING, logger="discord_codex_bot.datafiles")
    volume.unlink()
    assert live.get() == {"v": 1} and live.get() == {"v": 1}
    assert len([r for r in caplog.records if "gone or unreadable" in r.getMessage()]) == 1
    _replace(volume, '{"v": 2}')  # the volume coming back is taken up
    assert live.get() == {"v": 2}


def test_an_unreadable_source_keeps_the_last_good_value(tmp_path, monkeypatch) -> None:
    volume, baked = tmp_path / "data" / "x.json", tmp_path / "image" / "x.json"
    _replace(baked, '{"v": "baked"}')
    _replace(volume, '{"v": 1}')
    live = _live(volume, baked)
    assert live.get() == {"v": 1}
    real = Path.read_text

    def deny(self, *args, **kwargs):
        if self == volume:
            raise PermissionError("denied")
        return real(self, *args, **kwargs)

    monkeypatch.setattr(Path, "read_text", deny)
    _replace(volume, '{"v": 2}')  # a new version nobody may read
    assert live.get() == {"v": 1}  # not the baked copy either
    monkeypatch.setattr(Path, "stat", _stat_denied_for(volume))
    assert live.get() == {"v": 1}


def _stat_denied_for(blocked: Path):
    real = Path.stat

    def stat(self, *args, **kwargs):
        if self == blocked:
            raise PermissionError("denied")
        return real(self, *args, **kwargs)

    return stat


def test_a_parser_that_raises_anything_else_is_a_bad_version_not_a_crash(tmp_path, caplog) -> None:
    volume, baked = tmp_path / "data" / "x.json", tmp_path / "image" / "x.json"

    def parse(text: str) -> dict:
        return {"v": json.loads(text)["v"] + 0}  # TypeError for a string

    live = LiveFile("test data", [volume, baked], parse, {}, interval=0)
    _replace(volume, '{"v": 1}')
    assert live.get() == {"v": 1}
    caplog.set_level(logging.WARNING, logger="discord_codex_bot.datafiles")
    _replace(volume, '{"v": "x"}')
    assert live.get() == {"v": 1} and live.get() == {"v": 1}
    assert len([r for r in caplog.records if "unusable" in r.getMessage()]) == 1


@pytest.mark.parametrize(
    "providers",
    [
        {"a": {"urlPattern": 5}},
        {"a": {"urlPattern": "^https://a", "rules": 5}},
        {"a": {"urlPattern": "^https://a", "rules": [1]}},
        {},
    ],
)
def test_malformed_rule_files_are_refused_with_value_error(providers) -> None:
    with pytest.raises(ValueError):
        clearurls.Rules.parse(json.dumps({"providers": providers}))


def test_a_malformed_rule_file_never_displaces_good_rules(tmp_path, monkeypatch, caplog) -> None:
    monkeypatch.setenv("CLEARURLS_FILE", str(tmp_path / "baked.json"))
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    monkeypatch.setattr(clearurls, "_RULES", None)
    monkeypatch.setattr("discord_codex_bot.datafiles.CHECK_SECONDS", 0)
    _replace(tmp_path / "clearurls.json", _rules("good"))
    assert [p.name for p in clearurls.rules().providers] == ["good"]
    caplog.set_level(logging.WARNING, logger="discord_codex_bot.datafiles")
    _replace(tmp_path / "clearurls.json", '{"providers": {"a": {"urlPattern": 5}}}')
    assert clearurls.clean("https://good.example/?tr=1&id=2") == "https://good.example/?id=2"
    assert clearurls.clean("https://good.example/?tr=1&id=2") == "https://good.example/?id=2"
    assert len([r for r in caplog.records if "unusable" in r.getMessage()]) == 1


def test_malformed_name_maps_are_refused_with_value_error() -> None:
    with pytest.raises(ValueError):
        apis.parse_names("[1]")
    with pytest.raises(ValueError):
        apis.parse_names('{"names": {"a": "b"}, "paths": 5}')


def test_nothing_usable_anywhere_gives_the_empty_value(tmp_path: Path, caplog) -> None:
    live = _live(tmp_path / "a.json", tmp_path / "b.json")
    caplog.set_level(logging.WARNING, logger="discord_codex_bot.datafiles")
    assert live.get() == {} and live.get() == {}
    assert len([r for r in caplog.records if "no usable file" in r.getMessage()]) == 1


def test_the_stat_is_throttled(tmp_path: Path) -> None:
    volume, baked = tmp_path / "data" / "x.json", tmp_path / "image" / "x.json"
    _replace(volume, '{"v": 1}')
    live = _live(volume, baked, interval=3600)
    assert live.get() == {"v": 1}
    _replace(volume, '{"v": 2}')
    assert live.get() == {"v": 1}  # not looked at again inside the interval


def test_data_dir_defaults_to_the_volume_mount() -> None:
    assert data_dir({}) == Path("/var/lib/discord-codex/data")
    assert data_dir({"DATA_DIR": "/elsewhere"}) == Path("/elsewhere")


def _rules(*names: str) -> str:
    providers = {n: {"urlPattern": rf"^https?://{n}\.example", "rules": ["tr"]} for n in names}
    return json.dumps({"providers": providers})


def test_clearurls_prefers_the_refreshed_rules_and_follows_them(tmp_path, monkeypatch) -> None:
    # CLEARURLS_FILE names the image's copy (.env.example sets it); the refreshed one still wins
    monkeypatch.setenv("CLEARURLS_FILE", str(tmp_path / "baked.json"))
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    monkeypatch.setattr(clearurls, "_RULES", None)
    monkeypatch.setattr("discord_codex_bot.datafiles.CHECK_SECONDS", 0)
    _replace(tmp_path / "baked.json", _rules("old"))
    assert [p.name for p in clearurls.rules().providers] == ["old"]
    _replace(tmp_path / "clearurls.json", _rules("new"))
    assert [p.name for p in clearurls.rules().providers] == ["new"]
    assert clearurls.clean("https://new.example/?tr=1&id=2") == "https://new.example/?id=2"
    _replace(tmp_path / "clearurls.json", '{"rules": []}')  # not a rule file
    assert [p.name for p in clearurls.rules().providers] == ["new"]


def test_api_name_maps_come_from_the_volume_and_follow_it(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr("discord_codex_bot.datafiles.CHECK_SECONDS", 0)
    image, volume = tmp_path / "image", tmp_path / "data"
    image.mkdir()
    spec = {"hx": {"base": "https://hx.example/", "names": "names.json"}}
    (image / "apis.json").write_text(json.dumps(spec), "utf-8")
    _replace(image / "names.json", json.dumps({"names": {"亚索": "犽宿"}, "paths": {}}))
    registry = apis.load_registry(image / "apis.json", volume)
    assert registry["hx"].name_maps() == ({"亚索": "犽宿"}, {})  # fresh volume: the image's copy
    table = {"names": {"亚索": "犽宿", "尤里卡": "靈光一閃"}, "paths": {"hero/逆命": "hero/4"}}
    _replace(volume / "names.json", json.dumps(table, ensure_ascii=False))
    names, paths = registry["hx"].name_maps()
    assert names["尤里卡"] == "靈光一閃" and paths == {"hero/逆命": "hero/4"}
    _replace(volume / "names.json", json.dumps({"names": {}, "paths": {}}))  # emptied
    assert registry["hx"].name_maps()[1] == {"hero/逆命": "hero/4"}


def test_generated_topics_win_over_the_baked_ones(tmp_path: Path) -> None:
    root, overlay = tmp_path / "permanent", tmp_path / "data" / "topics"
    (root / "topics").mkdir(parents=True)
    (root / "MEMORY.md").write_text("- [英雄譯名對照](topics/英雄譯名對照.md)\n", "utf-8")
    (root / "topics" / "英雄譯名對照.md").write_text("| 逆命 | 崔斯特 |\n", "utf-8")
    (root / "topics" / "house-rules.md").write_text("不可以洗頻\n", "utf-8")
    limits = MemoryLimits(3, 4096, 50_000_000, 200_000_000, 50, 4096, 50, 3)
    perm = PermanentMemory(root, limits, overlay)
    assert "崔斯特" in perm.recall("英雄譯名對照")  # no volume yet: the baked table
    _replace(overlay / "英雄譯名對照.md", "| 逆命 | 崔斯特 |\n| 犽宿 | 亚索 |\n")
    assert "亚索" in perm.recall("英雄譯名對照")  # the index's link reaches the refreshed copy
    assert "犽宿" in perm.search("亚索")
    assert perm.recall("list") == "- house-rules\n- 英雄譯名對照"  # one entry per name
    assert perm.recall("house-rules").endswith("不可以洗頻")
    assert perm.index_text().startswith("- [英雄譯名對照]")  # the operator's index, untouched
