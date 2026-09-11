import os
from dataclasses import replace
from pathlib import Path

from discord_codex_bot.attachments import (
    REQUEST_DIR_PREFIX,
    remove_request_dir,
    sweep_stale,
    validate_image,
)
from discord_codex_bot.codex import _arguments
from discord_codex_bot.config import Config


def test_validate_image_accepts_known_types_and_rejects_others(config: Config) -> None:
    assert validate_image("image/png", 10, config) == ".png"
    assert validate_image("image/jpeg; charset=binary", 10, config) == ".jpg"
    assert not validate_image("text/plain", 10, config).startswith(".")
    assert not validate_image(None, 10, config).startswith(".")
    assert not validate_image("image/png", config.max_attachment_bytes + 1, config).startswith(".")


def test_image_flags_precede_stdin_marker(config: Config) -> None:
    args = _arguments(config, [Path("/tmp/discord-codex/req-a/image.png")])
    assert args[-4:] == ("-i", "/tmp/discord-codex/req-a/image.png", "--", "-")
    assert _arguments(config)[-2:] == ("--", "-")


def test_sweep_removes_only_stale_request_dirs(tmp_path: Path, config: Config) -> None:
    config = replace(config, attachment_dir=tmp_path)
    stale = tmp_path / f"{REQUEST_DIR_PREFIX}stale"
    fresh = tmp_path / f"{REQUEST_DIR_PREFIX}fresh"
    other = tmp_path / "unrelated"
    for directory in (stale, fresh, other):
        directory.mkdir()
        (directory / "image.png").write_bytes(b"x")
    os.utime(stale, (0, 0))
    os.utime(other, (0, 0))

    assert sweep_stale(config.attachment_dir, max_age_seconds=60) == 1
    assert not stale.exists()
    assert fresh.exists()
    assert other.exists()


def test_remove_request_dir_deletes_whole_request(tmp_path: Path) -> None:
    request_dir = tmp_path / f"{REQUEST_DIR_PREFIX}x"
    request_dir.mkdir()
    image = request_dir / "image.png"
    image.write_bytes(b"x")
    remove_request_dir(image)
    assert not request_dir.exists()
