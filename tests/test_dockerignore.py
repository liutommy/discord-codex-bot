"""The build context leaves out the host's own files (sign-ins, DBs, backups, logs) and nothing
the image needs: every COPY source, the operator's gitignored persona/permanent/output-style
files among them, still reaches the build."""

from __future__ import annotations

import re
from fnmatch import fnmatch
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def _patterns() -> list[str]:
    lines = (ROOT / ".dockerignore").read_text().splitlines()
    return [line.strip() for line in lines if line.strip() and not line.startswith("#")]


def _ignored(path: str) -> bool:
    """Docker's rule: a pattern matches a path or one of its parent directories; **/ any depth."""
    parts = path.split("/")
    prefixes = ["/".join(parts[: i + 1]) for i in range(len(parts))]
    for pattern in _patterns():
        tail = pattern.removeprefix("**/")
        for prefix in prefixes:
            if fnmatch(prefix, pattern):
                return True
            if tail != pattern and any(
                fnmatch("/".join(prefix.split("/")[j:]), tail) for j in range(prefix.count("/") + 1)
            ):
                return True
    return False


def _copy_sources() -> list[str]:
    dockerfile = (ROOT / "Dockerfile").read_text().replace("\\\n", " ")
    sources = []
    for line in dockerfile.splitlines():
        words = line.split()
        if not words or words[0] != "COPY" or any(w.startswith("--from") for w in words):
            continue
        sources += [w for w in words[1:-1] if not w.startswith("--")]
    return sources


def test_host_local_files_stay_out_of_the_build_context() -> None:
    for path in (
        "state/codex-home/auth.json",
        "state/xsearch-home/auth.json",
        "backups/2026-10-18.tar",
        "logs/bot.log",
        "compose.override.yaml",
        ".hold",
        "dcb-state.tar.age",
        "migrate/dot-env.age",
        ".env",
    ):
        assert _ignored(path), path


def test_everything_the_image_copies_still_reaches_the_build() -> None:
    sources = _copy_sources()
    assert "persona" in sources and "permanent" in sources  # the parser saw the real COPYs
    for source in sources:
        assert not _ignored(source), source
    for operator_file in (
        "persona/AGENTS.md",
        "permanent/MEMORY.md",
        "permanent/topics/champions.md",
        "config/output-style.md",
    ):
        assert not _ignored(operator_file), operator_file


def test_the_matcher_is_not_vacuous() -> None:
    # a plain name matches only at the top, as Docker reads it
    assert _ignored("tests/test_x.py") and not _ignored("discord_codex_bot/tests.py")
    assert re.fullmatch(r"\*\*/\*\.age", _patterns()[-1])
