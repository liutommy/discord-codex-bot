"""Refresh the generated data the Bot reads while it runs: the compose `data-refresh` service.

    docker compose --profile refresh run --rm data-refresh
    uv run python -m scripts.refresh_data --out <dir>      # anywhere else, by hand

Writes into DATA_DIR (default /var/lib/discord-codex/data, the generated-data volume):
clearurls.json (scripts/fetch_clearurls.py), lol-names.json and topics/*譯名對照.md
(scripts/build_lol_names.py). Each file is checked with the parser the Bot itself uses and
replaced atomically, and only when its content changed; the Bot notices the new copy within
seconds and reloads it, without a restart or an image rebuild.

A failed ClearURLs fetch or validation keeps the last good rules and does not stop the name
tables; a failed name build, or any error writing into the volume (read-only, full), fails the
run (exit 1). The last stdout line is a one-line summary whose second word
is `changed` (files listed) or `unchanged` on success, `failed` on failure."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from discord_codex_bot import apis, clearurls, datafiles
from discord_codex_bot.memory import atomic_write
from scripts import build_lol_names, fetch_clearurls


def publish(out: Path, files: dict[str, str]) -> list[str]:
    """Write each file whose content differs from what is there; the names that changed. Each
    write goes through a scratch file in the same directory and os.replace, so the Bot reads
    the old copy or the new one, never half of one."""
    changed = []
    for name, text in files.items():
        target = out / name
        try:
            same = target.read_text("utf-8") == text
        except (OSError, ValueError):
            same = False
        if not same:
            atomic_write(target, text)
            changed.append(name)
    return changed


MIN_TABLE_ROWS = 20  # the smallest real table (items) has about 120 rows


def check_topics(files: dict[str, str]) -> None:
    """The name-table topics have no parser in the Bot (permanent memory reads them as text), so
    check here that each still is a table: an empty or cut-off topic would otherwise displace the
    image's copy. Raises ValueError."""
    for name, text in files.items():
        if not name.endswith(".md"):
            continue
        rows = [line for line in text.splitlines() if line.startswith("|")]
        body = max(len(rows) - 2, 0)  # minus the header and the separator line
        if body < MIN_TABLE_ROWS:
            raise ValueError(f"{name}: {body} table rows, expected {MIN_TABLE_ROWS}+")


def _detail(error: BaseException) -> str:
    text = " ".join(str(error).split()) or "no detail"
    return f"{type(error).__name__}: {text}"[:300]


def refresh(out: Path) -> tuple[int, str]:
    """(exit code, summary line)."""
    changed: list[str] = []
    notes: list[str] = []
    try:
        return _refresh(out, changed, notes)
    except OSError as error:  # a read-only or full volume: nothing can be published, not soft
        return 1, f"data-refresh: failed; cannot write {out}: {_detail(error)}; " + "; ".join(notes)


def _refresh(out: Path, changed: list[str], notes: list[str]) -> tuple[int, str]:
    # Anything going wrong here (network, upstream format, our own parser) keeps the last good
    # rules: the Bot's built-in blocklist still applies, and the name tables still run.
    try:
        text, note = fetch_clearurls.build()
        clearurls.Rules.parse(text)
    except Exception as error:
        notes.append(f"clearurls failed, last good copy kept ({_detail(error)})")
    else:
        changed += publish(out, {"clearurls.json": text})  # an OSError here is a hard failure
        notes.append(f"clearurls {note}")
    try:
        files, note = build_lol_names.build()
        apis.parse_names(files[build_lol_names.NAMES_JSON.name])
        check_topics(files)
    except Exception as error:
        return 1, f"data-refresh: failed; name tables: {_detail(error)}; " + "; ".join(notes)
    changed += publish(out, files)
    notes.append(note)
    head = f"changed {', '.join(changed)}" if changed else "unchanged"
    return 0, f"data-refresh: {head}; " + "; ".join(notes)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--out", type=Path, default=datafiles.data_dir(), help="data directory")
    args = parser.parse_args(argv)
    code, summary = refresh(args.out)
    print(summary, flush=True)
    return code


if __name__ == "__main__":
    sys.exit(main())
