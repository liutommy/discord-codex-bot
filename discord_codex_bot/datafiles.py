"""Generated data the Bot reads while it runs: the ClearURLs rule set and the hexdata name tables.

`python -m scripts.refresh_data` (the compose `data-refresh` service) writes them into the
generated-data volume, which the Bot mounts read-only at DATA_DIR. The image keeps the
git-tracked copies as the floor, so a fresh or broken volume still works. A reader checks the
file's identity again at most every CHECK_SECONDS and reloads it when it changed: a refresh needs
neither an image rebuild nor a restart."""

from __future__ import annotations

import logging
import os
import stat
import time
from collections.abc import Callable, Mapping, Sequence
from pathlib import Path

LOGGER = logging.getLogger(__name__)

DEFAULT_DIR = Path("/var/lib/discord-codex/data")
CHECK_SECONDS = 5.0


def data_dir(env: Mapping[str, str] | None = None) -> Path:
    values = os.environ if env is None else env
    return Path(values.get("DATA_DIR", "").strip() or DEFAULT_DIR)


def _version(path: Path) -> tuple[str, int, int, int] | None:
    """What identifies one copy of a file: os.replace gives the new copy a new inode even when
    its size and mtime match the old one. None when there is no regular file there."""
    try:
        st = path.stat()
    except OSError:
        return None
    if not stat.S_ISREG(st.st_mode):
        return None
    return str(path), st.st_mtime_ns, st.st_size, st.st_ino


class LiveFile[T]:
    """The parsed content of the first usable file among `paths` (the volume copy first, then
    the image's), kept current without a restart.

    A file that is missing is skipped. A file that fails to parse (a truncated write, a format
    change) or cannot be read is logged once per version and never replaces data already loaded:
    the last good copy stays in use, and only when nothing has loaded yet does the next path get
    its turn. Once something has loaded, the copy that supplied it staying gone or unreadable
    (a deleted file, a volume that vanished) keeps the last good value too, logged once; the
    paths behind it (the image's older copy) are never consulted again, while a copy ahead of it
    that newly appears is taken up. `parse` gets the file's text and raises ValueError for
    anything unusable; any other exception it raises is treated the same way."""

    def __init__(
        self,
        label: str,
        paths: Sequence[Path],
        parse: Callable[[str], T],
        empty: T,
        interval: float | None = None,
    ) -> None:
        self._label = label
        self._paths = tuple(paths)
        self._parse = parse
        self._value = empty
        self._interval = CHECK_SECONDS if interval is None else interval
        self._loaded: tuple[str, int, int, int] | None = None
        self._bad: set[tuple[str, int, int, int]] = set()
        self._checked: float | None = None
        self._warned_missing = False
        self._warned_gone: tuple[str, int, int, int] | None = None

    def get(self) -> T:
        now = time.monotonic()
        if self._checked is not None and now - self._checked < self._interval:
            return self._value
        self._checked = now
        paths = self._paths
        if self._loaded is not None:
            source = [str(p) for p in paths].index(self._loaded[0])
            paths = paths[: source + 1]
        for path in paths:
            version = _version(path)
            if version is None:
                continue
            if version == self._loaded:
                return self._value
            if version not in self._bad and self._load(path, version):
                return self._value
            if self._loaded is not None:
                return self._value  # a bad newer copy never displaces the last good one
        if self._loaded is not None and self._warned_gone != self._loaded:
            self._warned_gone = self._loaded
            LOGGER.warning(
                "%s: %s is gone or unreadable; keeping the last good copy",
                self._label,
                self._loaded[0],
            )
        if self._loaded is None and not self._warned_missing:
            self._warned_missing = True
            LOGGER.warning("%s: no usable file at %s", self._label, [str(p) for p in self._paths])
        return self._value

    def _load(self, path: Path, version: tuple[str, int, int, int]) -> bool:
        try:
            value = self._parse(path.read_text("utf-8"))
        except Exception as error:  # a parser's TypeError must not escape get() either
            self._bad.add(version)
            kept = "keeping the last good copy" if self._loaded else "trying the next copy"
            LOGGER.warning("%s: %s unusable (%s); %s", self._label, path, error, kept)
            return False
        self._value, self._loaded = value, version
        LOGGER.info("%s: loaded %s", self._label, path)
        return True
