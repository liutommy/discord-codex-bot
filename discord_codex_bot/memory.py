from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import date
from pathlib import Path

SCOPES = {"user": "個人", "guild": "伺服器"}
INDEX_FILE = "MEMORY.md"
ARCHIVE_FILE = "MEMORY-archive.md"
TOPIC_DIR = "topics"
LIST_NAME = "list"
_SLUG = re.compile(r"[^\w-]+", re.UNICODE)
_INDEX_LINE = re.compile(r"^- \[(?P<name>[^\]]+)\]\((?P<file>[^)]+)\) — (?P<hook>.*)$")

# Mode B: the model appends these after an answer when it noticed a durable fact.
MEMORY_TAG = re.compile(
    r'<memory\s+scope="(user|guild)"\s+name="([^"]{1,60})">\s*(.*?)\s*</memory>', re.S
)
# Claude-style on-demand read: the model asks for a topic file (or the archive list) and the Bot
# feeds it back into the same thread.
RECALL_TAG = re.compile(r'<recall\s+scope="(user|guild)"\s+name="([^"]{1,80})"\s*/>')


@dataclass(frozen=True, slots=True)
class MemoryLimits:
    index_max_lines: int
    index_max_bytes: int
    user_max_bytes: int
    guild_max_bytes: int
    recall_max_bytes: int


@dataclass(frozen=True, slots=True)
class Entry:
    name: str
    file: str
    hook: str

    def line(self) -> str:
        return f"- [{self.name}]({self.file}) — {self.hook}"


def slugify(name: str) -> str:
    slug = _SLUG.sub("-", name.strip()).strip("-").lower()[:40]
    return slug or "memory"


class MemoryStore:
    """Two-tier long-term memory per scope, modelled on Claude Code auto memory.

    <root>/<guild>/guild/            server-wide scope
    <root>/<guild>/users/<user>/     one directory per member
        MEMORY.md                    index: one line per memory, injected into every prompt
        MEMORY-archive.md            index lines that fell off the injected window
        topics/<slug>.md             the memory itself, read on demand via <recall/>
    """

    def __init__(self, root: Path, limits: MemoryLimits) -> None:
        self._root = root
        self._limits = limits

    # ----- paths -----------------------------------------------------------------------------

    def scope_dir(self, scope: str, guild_id: int | None, user_id: int | None) -> Path:
        base = self._root / str(guild_id)
        return base / "guild" if scope == "guild" else base / "users" / str(user_id)

    def capacity(self, scope: str) -> int:
        return self._limits.guild_max_bytes if scope == "guild" else self._limits.user_max_bytes

    def usage_bytes(self, scope: str, guild_id: int | None, user_id: int | None) -> int:
        directory = self.scope_dir(scope, guild_id, user_id)
        return sum(p.stat().st_size for p in directory.rglob("*") if p.is_file())

    # ----- index -----------------------------------------------------------------------------

    def entries(self, scope: str, guild_id: int | None, user_id: int | None) -> list[Entry]:
        return self._read_index(self.scope_dir(scope, guild_id, user_id) / INDEX_FILE)

    def index_text(self, scope: str, guild_id: int | None, user_id: int | None) -> str:
        """The injected window: the first index_max_lines / index_max_bytes of MEMORY.md."""
        lines = [entry.line() for entry in self.entries(scope, guild_id, user_id)]
        kept: list[str] = []
        size = 0
        for line in lines[: self._limits.index_max_lines]:
            size += len(line.encode("utf-8")) + 1
            if size > self._limits.index_max_bytes:
                break
            kept.append(line)
        return "\n".join(kept)

    def render(self, guild_id: int | None, user_id: int) -> str:
        sections = []
        for scope, label in SCOPES.items():
            text = self.index_text(scope, guild_id, user_id)
            if text:
                sections.append(f"[{label}記憶索引]\n{text}")
        return "\n\n".join(sections)

    # ----- write -----------------------------------------------------------------------------

    def add(
        self, scope: str, guild_id: int | None, user_id: int | None, name: str, text: str
    ) -> str:
        """Store one memory; returns the index line, or a user-facing reason when refused."""
        text = text.strip()
        if not text:
            return "記憶內容不能是空的。"
        directory = self.scope_dir(scope, guild_id, user_id)
        body = f"# {name.strip()}\n\n{date.today().isoformat()}\n\n{text}\n"
        if self.usage_bytes(scope, guild_id, user_id) + len(body.encode("utf-8")) > self.capacity(
            scope
        ):
            return f"{SCOPES[scope]}記憶已達容量上限，請先用 /forget 清理。"
        entries = self._read_index(directory / INDEX_FILE)
        slug = slugify(name)
        existing = {entry.file for entry in entries} | {
            entry.file for entry in self._read_index(directory / ARCHIVE_FILE)
        }
        file = f"{slug}.md"
        counter = 2
        while file in existing:
            file = f"{slug}-{counter}.md"
            counter += 1
        (directory / TOPIC_DIR).mkdir(parents=True, exist_ok=True)
        (directory / TOPIC_DIR / file).write_text(body, "utf-8")
        hook = " ".join(text.split())
        hook = hook if len(hook) <= 80 else f"{hook[:80]}…"
        entries.append(Entry(name.strip(), file, hook))
        self._write_index(directory, entries)
        return entries[-1].line()

    def forget(self, scope: str, guild_id: int | None, user_id: int | None, name: str) -> bool:
        directory = self.scope_dir(scope, guild_id, user_id)
        removed = False
        for index_name in (INDEX_FILE, ARCHIVE_FILE):
            entries = self._read_index(directory / index_name)
            keep = [e for e in entries if e.name != name and e.file != name]
            for entry in entries:
                if entry not in keep:
                    (directory / TOPIC_DIR / entry.file).unlink(missing_ok=True)
                    removed = True
            if len(keep) != len(entries):
                self._write_file(directory / index_name, [e.line() for e in keep])
        return removed

    # ----- read on demand --------------------------------------------------------------------

    def recall(self, scope: str, guild_id: int | None, user_id: int | None, name: str) -> str:
        directory = self.scope_dir(scope, guild_id, user_id)
        if name == LIST_NAME:
            lines = [e.line() for e in self._read_index(directory / ARCHIVE_FILE)]
            return "\n".join(lines) or "（沒有索引以外的記憶）"
        entries = self._read_index(directory / INDEX_FILE) + self._read_index(
            directory / ARCHIVE_FILE
        )
        match = next((e for e in entries if e.name == name or e.file == name), None)
        if match is None:
            return f"（找不到記憶「{name}」）"
        raw = (directory / TOPIC_DIR / match.file).read_bytes()[: self._limits.recall_max_bytes]
        return raw.decode("utf-8", errors="ignore")

    # ----- internals -------------------------------------------------------------------------

    def _read_index(self, path: Path) -> list[Entry]:
        try:
            lines = path.read_text("utf-8").splitlines()
        except OSError:
            return []
        entries = []
        for line in lines:
            match = _INDEX_LINE.match(line)
            if match:
                entries.append(Entry(match["name"], match["file"], match["hook"]))
        return entries

    def _write_index(self, directory: Path, entries: list[Entry]) -> None:
        # Keep the injected window honest: once MEMORY.md would exceed its limits, the oldest
        # lines move to the archive, where <recall name="list"/> still finds them.
        overflow: list[Entry] = []
        while entries:
            lines = [e.line() for e in entries]
            within_lines = len(lines) <= self._limits.index_max_lines
            within_bytes = len("\n".join(lines).encode("utf-8")) <= self._limits.index_max_bytes
            if within_lines and within_bytes:
                break
            overflow.append(entries.pop(0))
        if overflow:
            archived = self._read_index(directory / ARCHIVE_FILE) + overflow
            self._write_file(directory / ARCHIVE_FILE, [e.line() for e in archived])
        self._write_file(directory / INDEX_FILE, [e.line() for e in entries])

    @staticmethod
    def _write_file(path: Path, lines: list[str]) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("\n".join(lines) + ("\n" if lines else ""), "utf-8")


def extract_memory_tags(answer: str) -> tuple[str, list[tuple[str, str, str]]]:
    """Split an answer into (visible text, [(scope, name, fact), ...])."""
    found = [(s, n, f) for s, n, f in MEMORY_TAG.findall(answer) if f.strip()]
    return MEMORY_TAG.sub("", answer).strip(), found


def extract_recall_tags(answer: str) -> list[tuple[str, str]]:
    return RECALL_TAG.findall(answer)
