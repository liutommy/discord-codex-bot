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

    assert sweep_stale(config.attachment_dir, max_age_seconds=60, prefix=REQUEST_DIR_PREFIX) == 1
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


def test_sweep_without_prefix_covers_generated_image_dirs(tmp_path: Path) -> None:
    stale = tmp_path / "thread-old"
    stale.mkdir()
    os.utime(stale, (0, 0))
    (tmp_path / "file.txt").write_bytes(b"x")
    assert sweep_stale(tmp_path, max_age_seconds=60) == 1
    assert not stale.exists()
    assert (tmp_path / "file.txt").exists()


def test_validate_attachment_classifies_images_documents_and_rejects_the_rest(config) -> None:
    from discord_codex_bot.attachments import validate_attachment

    assert validate_attachment("image/png", "x.png", 10, config) == ("image", ".png")
    assert validate_attachment("application/pdf", "c.pdf", 10, config) == ("document", ".pdf")
    text_plain = validate_attachment("text/plain; charset=utf-8", "n.txt", 10, config)
    assert text_plain == ("document", ".txt")
    by_suffix = validate_attachment("application/octet-stream", "m.py", 10, config)
    assert by_suffix == ("document", ".py")
    assert validate_attachment("application/octet-stream", "blob.bin", 10, config)[0] == ""
    assert validate_attachment("video/mp4", "v.mp4", 10, config)[0] == ""
    too_big = config.max_attachment_bytes + 1
    kind, reason = validate_attachment("text/plain", "big.txt", too_big, config)
    assert kind == "" and "必須小於" in reason


def _pdf_with_text(text: str) -> bytes:
    objects = [
        b"<</Type/Catalog/Pages 2 0 R>>",
        b"<</Type/Pages/Kids[3 0 R]/Count 1>>",
        b"<</Type/Page/Parent 2 0 R/MediaBox[0 0 200 100]/Contents 4 0 R"
        b"/Resources<</Font<</F1 5 0 R>>>>>>",
        None,  # content stream, filled below
        b"<</Type/Font/Subtype/Type1/BaseFont/Helvetica>>",
    ]
    stream = f"BT /F1 12 Tf 10 50 Td ({text}) Tj ET".encode()
    objects[3] = b"<</Length " + str(len(stream)).encode() + b">>stream\n" + stream + b"\nendstream"
    out = bytearray(b"%PDF-1.4\n")
    offsets = []
    for number, body in enumerate(objects, start=1):
        offsets.append(len(out))
        out += f"{number} 0 obj\n".encode() + body + b"\nendobj\n"
    xref = len(out)
    out += f"xref\n0 {len(objects) + 1}\n0000000000 65535 f \n".encode()
    for offset in offsets:
        out += f"{offset:010d} 00000 n \n".encode()
    out += f"trailer<</Size {len(objects) + 1}/Root 1 0 R>>\nstartxref\n{xref}\n%%EOF\n".encode()
    return bytes(out)


def test_extract_text_handles_text_binary_and_pdf(tmp_path) -> None:
    from discord_codex_bot.attachments import extract_text

    (tmp_path / "a.txt").write_text("第一行\n第二行", "utf-8")
    assert extract_text(tmp_path / "a.txt", 1000) == "第一行\n第二行"
    assert extract_text(tmp_path / "a.txt", 3).startswith("第一行\n[已截斷至 3 字]")
    (tmp_path / "b.bin").write_bytes(b"\x00\x01\x02")
    assert extract_text(tmp_path / "b.bin", 1000) == "（這不是文字檔，讀不出內容）"
    (tmp_path / "e.txt").write_text("   ", "utf-8")
    assert extract_text(tmp_path / "e.txt", 1000) == "（檔案沒有可讀文字）"
    pdf = _pdf_with_text("Hello PDF")
    (tmp_path / "c.pdf").write_bytes(pdf)
    assert "Hello PDF" in extract_text(tmp_path / "c.pdf", 1000)
    (tmp_path / "d.pdf").write_bytes(b"not really a pdf")
    assert extract_text(tmp_path / "d.pdf", 1000) == "（PDF 讀不出文字：可能是掃描圖檔或加密）"
