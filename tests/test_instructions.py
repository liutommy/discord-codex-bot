from dataclasses import replace
from pathlib import Path

import pytest

from discord_codex_bot import instructions
from discord_codex_bot.config import Config


def _config(config: Config, tmp_path: Path) -> Config:
    """A config whose every instruction path is writable, shaped like the container's."""
    image = tmp_path / "image"
    (image / "persona").mkdir(parents=True)
    (image / "rules").mkdir(parents=True)
    (image / "rules" / "AGENTS.md").write_text("RULES\n", "utf-8")
    (image / "output-style.md").write_text("image style\n", "utf-8")
    return replace(
        config,
        codex_home=tmp_path / "vol",
        codex_workspace=tmp_path / "vol" / "workspace",
        codex_workspace_plain=tmp_path / "vol" / "workspace-plain",
        codex_rules_path=image / "rules" / "AGENTS.md",
        persona_dir=image / "persona",
        output_style_path=image / "output-style.md",
        backup_dir=tmp_path / "backups",
    )


def test_image_persona_skips_the_readme_and_the_sample(config: Config, tmp_path: Path) -> None:
    cfg = _config(config, tmp_path)
    (cfg.persona_dir / "README.md").write_text("how to write a persona\n", "utf-8")
    (cfg.persona_dir / "AGENTS.example.md").write_text("sample persona\n", "utf-8")
    assert instructions.image_persona(cfg) == ""  # a fresh clone runs with no persona at all
    (cfg.persona_dir / "AGENTS.md").write_text("  前輩  \n", "utf-8")
    (cfg.persona_dir / "extra.md").write_text("附錄\n", "utf-8")
    assert instructions.image_persona(cfg) == "前輩\n\n附錄"  # name order, stripped, joined


def test_an_upload_wins_until_it_is_reset(config: Config, tmp_path: Path) -> None:
    cfg = _config(config, tmp_path)
    (cfg.persona_dir / "AGENTS.md").write_text("image persona\n", "utf-8")
    assert instructions.persona_text(cfg) == "image persona"
    assert instructions.style_path(cfg) == cfg.output_style_path
    assert not instructions.is_uploaded(cfg, instructions.PERSONA)

    kept = instructions.save(cfg, instructions.PERSONA, "uploaded persona")
    assert instructions.persona_text(cfg) == "uploaded persona"
    assert instructions.is_uploaded(cfg, instructions.PERSONA)
    assert kept is not None and kept.read_text("utf-8").strip() == "image persona"

    instructions.save(cfg, instructions.OUTPUT_STYLE, "uploaded style")
    assert instructions.style_path(cfg) == instructions.uploaded_path(cfg, "output-style")
    assert instructions.style_text(cfg) == "uploaded style"

    removed, kept = instructions.reset(cfg, instructions.PERSONA)
    assert removed and kept is not None
    assert kept.read_text("utf-8").strip() == "uploaded persona"  # what the reset threw away
    assert instructions.persona_text(cfg) == "image persona"
    assert instructions.reset(cfg, instructions.PERSONA) == (False, None)


def test_backup_is_skipped_when_no_directory_is_configured(config: Config, tmp_path) -> None:
    cfg = replace(_config(config, tmp_path), backup_dir=None)
    (cfg.persona_dir / "AGENTS.md").write_text("image persona\n", "utf-8")
    assert instructions.save(cfg, instructions.PERSONA, "x") is None
    assert instructions.persona_text(cfg) == "x"  # the upload still lands


def test_compose_writes_both_working_directories(config: Config, tmp_path: Path) -> None:
    cfg = _config(config, tmp_path)
    (cfg.persona_dir / "AGENTS.md").write_text("前輩\n", "utf-8")
    assert instructions.compose_workspaces(cfg) is True
    persona_agents = (cfg.codex_workspace / "AGENTS.md").read_text("utf-8")
    plain_agents = (cfg.codex_workspace_plain / "AGENTS.md").read_text("utf-8")
    assert persona_agents == "RULES\n\n\n前輩\n" and plain_agents == "RULES\n"
    assert not list(cfg.codex_workspace.glob("*.tmp"))  # written through a temp, then replaced

    instructions.save(cfg, instructions.PERSONA, "新人設")
    assert instructions.compose_workspaces(cfg) is True
    assert (cfg.codex_workspace / "AGENTS.md").read_text("utf-8") == "RULES\n\n\n新人設\n"
    assert (cfg.codex_workspace_plain / "AGENTS.md").read_text("utf-8") == "RULES\n"

    instructions.reset(cfg, instructions.PERSONA)
    (cfg.persona_dir / "AGENTS.md").unlink()
    assert instructions.compose_workspaces(cfg) is True
    assert (cfg.codex_workspace / "AGENTS.md").read_text("utf-8") == "RULES\n"  # no persona at all


def test_compose_leaves_the_old_directories_alone_when_the_rules_are_missing(
    config: Config, tmp_path: Path
) -> None:
    cfg = _config(config, tmp_path)
    assert instructions.compose_workspaces(cfg) is True
    cfg.codex_rules_path.unlink()
    assert instructions.compose_workspaces(cfg) is False
    # the previous composition must survive: a half-empty AGENTS.md is worse than a stale one
    assert (cfg.codex_workspace / "AGENTS.md").read_text("utf-8") == "RULES\n"


def test_parse_upload_accepts_markdown_and_names_every_rejection() -> None:
    assert instructions.parse_upload("A.MD", "  人設  \n".encode()) == "人設"
    for name, data, reason in (
        ("persona.txt", b"x", "只收"),
        ("persona.md", b"\xff\xfe\x00", "UTF-8"),
        ("persona.md", b"  \n", "空的"),
    ):
        with pytest.raises(ValueError, match=reason):
            instructions.parse_upload(name, data)
    over = "字" * (instructions.MAX_CHARS + 1)
    with pytest.raises(ValueError, match=f"{instructions.MAX_CHARS + 1} 字"):
        instructions.parse_upload("persona.md", over.encode())
    # the operator limit has to clear the shipped persona, which is far past a member's 4000
    assert instructions.MAX_CHARS > 4000


def test_two_backups_in_the_same_second_do_not_overwrite_each_other(
    config: Config, tmp_path: Path
) -> None:
    cfg = _config(config, tmp_path)
    (cfg.persona_dir / "AGENTS.md").write_text("原本的人設\n", "utf-8")
    frozen = 1_758_000_000.0  # same second for both calls, which is what an upload+reset does
    first = instructions.backup(cfg, instructions.PERSONA, now=frozen)
    instructions.save(cfg, instructions.PERSONA, "替換的人設")
    second = instructions.backup(cfg, instructions.PERSONA, now=frozen)
    assert first is not None and second is not None and first != second
    assert first.read_text("utf-8").strip() == "原本的人設"
    assert second.read_text("utf-8").strip() == "替換的人設"


def test_the_working_directories_are_left_read_only(config: Config, tmp_path: Path) -> None:
    import os
    import stat

    cfg = _config(config, tmp_path)
    (cfg.persona_dir / "AGENTS.md").write_text("前輩\n", "utf-8")
    assert instructions.compose_workspaces(cfg) is True
    for folder in (cfg.codex_workspace, cfg.codex_workspace_plain):
        agents = folder / "AGENTS.md"
        assert stat.S_IMODE(folder.stat().st_mode) == 0o555
        assert stat.S_IMODE(agents.stat().st_mode) == 0o444
        if os.geteuid() != 0:  # root ignores the mode; nobody in the container runs as root
            with pytest.raises(PermissionError):
                (folder / "pwned.txt").write_text("x", "utf-8")
            with pytest.raises(PermissionError):
                agents.write_text("tampered", "utf-8")

    # and the Bot can still replace them: a second composition must not need a hand
    instructions.save(cfg, instructions.PERSONA, "新人設")
    assert instructions.compose_workspaces(cfg) is True
    assert (cfg.codex_workspace / "AGENTS.md").read_text("utf-8") == "RULES\n\n\n新人設\n"
    for folder in (cfg.codex_workspace, cfg.codex_workspace_plain):
        folder.chmod(0o755)  # so pytest can clean its tmp dir up afterwards
        (folder / "AGENTS.md").chmod(0o644)
