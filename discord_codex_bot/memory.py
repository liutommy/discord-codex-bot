from __future__ import annotations

import re
import shutil
from dataclasses import dataclass
from datetime import date
from pathlib import Path

SCOPES = {"user": "個人", "guild": "伺服器"}
INDEX_FILE = "MEMORY.md"
ARCHIVE_FILE = "MEMORY-archive.md"
TOPIC_DIR = "topics"
STYLE_FILE = "style.md"
MODEL_FILE = "model.txt"
LIST_NAME = "list"
_SLUG = re.compile(r"[^\w-]+", re.UNICODE)
_INDEX_LINE = re.compile(r"^- \[(?P<name>[^\]]+)\]\((?P<file>[^)]+)\) — (?P<hook>.*)$")

# Mode B: the model appends these after an answer when it noticed a durable fact.
MEMORY_TAG = re.compile(
    r'<memory\s+scope="(user|guild)"\s+name="([^"]{1,60})">\s*(.*?)\s*</memory>', re.S
)
# On-demand reads, executed by the Bot (Codex has no file tools here). Modelled on pi-context:
# snippet-first search, then a paged read of one note.
# Models sometimes drop the "/" or add a closing tag; accept `/>`, `>` and `></search>` alike.
SEARCH_TAG = re.compile(
    r'<search\s+scope="(user|guild|permanent)"\s+query="([^"]{1,200})"\s*/?>(?:\s*</search>)?'
)
RECALL_TAG = re.compile(
    r'<recall\s+scope="(user|guild|permanent)"\s+name="([^"]{1,80})"'
    r'(?:\s+offset="(\d+)")?(?:\s+lines="(\d+)")?\s*/?>(?:\s*</recall>)?'
)


@dataclass(frozen=True, slots=True)
class MemoryLimits:
    index_max_lines: int
    index_max_bytes: int
    user_max_bytes: int
    guild_max_bytes: int
    read_max_lines: int
    read_max_bytes: int
    search_max_matches: int
    search_context_lines: int


@dataclass(frozen=True, slots=True)
class Entry:
    name: str
    file: str
    hook: str

    def line(self) -> str:
        return f"- [{self.name}]({self.file}) — {self.hook}"


@dataclass(frozen=True, slots=True)
class Note:
    name: str
    date: str
    text: str


def _hook(text: str) -> str:
    hook = " ".join(text.split())
    return hook if len(hook) <= 80 else f"{hook[:80]}…"


def slugify(name: str) -> str:
    slug = _SLUG.sub("-", name.strip()).strip("-").lower()[:40]
    return slug or "memory"


class MemoryStore:
    """Two-tier long-term memory per scope, modelled on Claude Code auto memory.

    <root>/<guild>/guild/            server-wide scope
    <root>/<guild>/users/<user>/     one directory per member
        MEMORY.md                    index, one line per memory, injected into every prompt
        MEMORY-archive.md            index lines that fell off the injected window
        topics/<slug>.md             the memory itself, searched / read on demand
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
        return sum(
            p.stat().st_size
            for p in directory.rglob("*")
            if p.is_file() and ".backup" not in p.parts
        )

    # ----- index -----------------------------------------------------------------------------

    def entries(self, scope: str, guild_id: int | None, user_id: int | None) -> list[Entry]:
        return self._read_index(self.scope_dir(scope, guild_id, user_id) / INDEX_FILE)

    def all_entries(self, scope: str, guild_id: int | None, user_id: int | None) -> list[Entry]:
        directory = self.scope_dir(scope, guild_id, user_id)
        return self._read_index(directory / INDEX_FILE) + self._read_index(directory / ARCHIVE_FILE)

    def index_text(self, scope: str, guild_id: int | None, user_id: int | None) -> str:
        """The injected window: the first index_max_lines / index_max_bytes of MEMORY.md."""
        lines = [entry.line() for entry in self.entries(scope, guild_id, user_id)]
        kept: list[str] = []
        size = -1  # "\n".join(): one separator fewer than lines, same measure as _write_index
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
        """Store one memory and return its index line. A full scope evicts its oldest notes."""
        text = text.strip()
        if not text:
            return "記憶內容不能是空的。"
        directory = self.scope_dir(scope, guild_id, user_id)
        body = f"# {name.strip()}\n\n{date.today().isoformat()}\n\n{text}\n"
        needed = len(body.encode("utf-8")) + 120
        while self.usage_bytes(scope, guild_id, user_id) + needed > self.capacity(
            scope
        ) and self._evict_oldest(directory):
            pass
        entries = self._read_index(directory / INDEX_FILE)
        slug = slugify(name)
        existing = {e.file for e in self.all_entries(scope, guild_id, user_id)}
        file = f"{slug}.md"
        counter = 2
        while file in existing:
            file = f"{slug}-{counter}.md"
            counter += 1
        (directory / TOPIC_DIR).mkdir(parents=True, exist_ok=True)
        (directory / TOPIC_DIR / file).write_text(body, "utf-8")
        entries.append(Entry(name.strip(), file, _hook(text)))
        self._write_index(directory, entries)
        return entries[-1].line()

    # ----- consolidation ---------------------------------------------------------------------

    def guild_ids(self) -> list[int]:
        if not self._root.is_dir():
            return []
        return sorted(int(p.name) for p in self._root.iterdir() if p.name.isdigit())

    def user_ids(self, guild_id: int) -> list[int]:
        users = self._root / str(guild_id) / "users"
        if not users.is_dir():
            return []
        return sorted(int(p.name) for p in users.iterdir() if p.name.isdigit())

    def notes(self, scope: str, guild_id: int | None, user_id: int | None) -> list[Note]:
        """Every note of a scope, oldest first (archive before index), as (name, date, text)."""
        directory = self.scope_dir(scope, guild_id, user_id)
        notes = []
        for entry in self._read_index(directory / ARCHIVE_FILE) + self._read_index(
            directory / INDEX_FILE
        ):
            try:
                raw = (directory / TOPIC_DIR / entry.file).read_text("utf-8")
            except OSError:
                continue
            parts = raw.split("\n\n", 2)
            note_date = parts[1].strip() if len(parts) > 1 else ""
            text = parts[2].strip() if len(parts) > 2 else raw.strip()
            notes.append(Note(entry.name, note_date, text))
        return notes

    def rewrite(
        self, scope: str, guild_id: int | None, user_id: int | None, notes: list[Note]
    ) -> None:
        """Replace a scope's notes wholesale; the previous state is kept in `.backup/`."""
        directory = self.scope_dir(scope, guild_id, user_id)
        backup = directory / ".backup"
        if backup.exists():
            shutil.rmtree(backup)
        backup.mkdir(parents=True)
        for name in (INDEX_FILE, ARCHIVE_FILE):
            if (directory / name).exists():
                shutil.move(str(directory / name), str(backup / name))
        if (directory / TOPIC_DIR).exists():
            shutil.move(str(directory / TOPIC_DIR), str(backup / TOPIC_DIR))
        (directory / TOPIC_DIR).mkdir(parents=True, exist_ok=True)
        entries: list[Entry] = []
        used: set[str] = set()
        for note in notes:
            slug = slugify(note.name)
            file = f"{slug}.md"
            counter = 2
            while file in used:
                file = f"{slug}-{counter}.md"
                counter += 1
            used.add(file)
            body = f"# {note.name}\n\n{note.date}\n\n{note.text}\n"
            (directory / TOPIC_DIR / file).write_text(body, "utf-8")
            entries.append(Entry(note.name, file, _hook(note.text)))
        self._write_index(directory, entries)

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

    def search(self, scope: str, guild_id: int | None, user_id: int | None, query: str) -> str:
        """Snippet-first search over every note, definition lines first."""
        directory = self.scope_dir(scope, guild_id, user_id)
        sources = []
        for entry in self.all_entries(scope, guild_id, user_id):
            try:
                lines = (directory / TOPIC_DIR / entry.file).read_text("utf-8").splitlines()
            except OSError:
                continue
            sources.append((entry.name, entry.file, lines))
        return search_snippets(sources, query, self._limits, f"（「{query}」沒有命中任何記憶）")

    def recall(
        self,
        scope: str,
        guild_id: int | None,
        user_id: int | None,
        name: str,
        offset: int = 1,
        lines: int | None = None,
    ) -> str:
        """Paged read of one note; the header tells the model how much is left."""
        directory = self.scope_dir(scope, guild_id, user_id)
        if name == LIST_NAME:
            listed = [e.line() for e in self._read_index(directory / ARCHIVE_FILE)]
            return self._truncate("\n".join(listed) or "（沒有索引以外的記憶）")
        match = next(
            (e for e in self.all_entries(scope, guild_id, user_id) if name in (e.name, e.file)),
            None,
        )
        if match is None:
            return f"（找不到記憶「{name}」）"
        try:
            all_lines = (directory / TOPIC_DIR / match.file).read_text("utf-8").splitlines()
        except OSError:
            return f"（記憶「{name}」的檔案遺失）"
        page = min(lines or self._limits.read_max_lines, self._limits.read_max_lines)
        start = max(1, offset)
        chunk = all_lines[start - 1 : start - 1 + page]
        body = "\n".join(f"{i:>4}: {line}" for i, line in enumerate(chunk, start))
        if not chunk:
            return f"[{match.file} 共 {len(all_lines)} 行；offset {start} 已超過檔尾]"
        header = f"[{match.file} 第 {start}–{start + len(chunk) - 1} 行，共 {len(all_lines)} 行]"
        return self._truncate(f"{header}\n{body}")

    # ----- personal output style -------------------------------------------------------------

    def style_path(self, guild_id: int | None, user_id: int) -> Path:
        return self.scope_dir("user", guild_id, user_id) / STYLE_FILE

    def get_style(self, guild_id: int | None, user_id: int) -> str:
        try:
            return self.style_path(guild_id, user_id).read_text("utf-8").strip()
        except OSError:
            return ""

    def set_style(self, guild_id: int | None, user_id: int, text: str) -> None:
        path = self.style_path(guild_id, user_id)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text.strip() + "\n", "utf-8")

    def clear_style(self, guild_id: int | None, user_id: int) -> bool:
        path = self.style_path(guild_id, user_id)
        existed = path.exists()
        path.unlink(missing_ok=True)
        return existed

    # ----- personal model choice -------------------------------------------------------------

    def get_model(self, guild_id: int | None, user_id: int) -> str:
        try:
            path = self.scope_dir("user", guild_id, user_id) / MODEL_FILE
            return path.read_text("utf-8").strip()
        except OSError:
            return ""

    def set_model(self, guild_id: int | None, user_id: int, value: str) -> None:
        path = self.scope_dir("user", guild_id, user_id) / MODEL_FILE
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(value.strip() + "\n", "utf-8")

    def clear_model(self, guild_id: int | None, user_id: int) -> bool:
        path = self.scope_dir("user", guild_id, user_id) / MODEL_FILE
        existed = path.exists()
        path.unlink(missing_ok=True)
        return existed

    # ----- internals -------------------------------------------------------------------------

    def _truncate(self, text: str) -> str:
        return _truncate(text, self._limits.read_max_bytes)

    def _evict_oldest(self, directory: Path) -> bool:
        for index_name in (ARCHIVE_FILE, INDEX_FILE):
            entries = self._read_index(directory / index_name)
            if entries:
                oldest = entries.pop(0)
                (directory / TOPIC_DIR / oldest.file).unlink(missing_ok=True)
                self._write_file(directory / index_name, [e.line() for e in entries])
                return True
        return False

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
        # lines move to the archive, where <recall name="list"/> and <search/> still find them.
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


class PermanentMemory:
    """Operator-managed tier: a directory of hand-written files baked into the image.

    <root>/MEMORY.md      injected into every prompt verbatim — no line/byte window, never evicted
    <root>/topics/*.md    read on demand by file stem via <search/> and <recall/>
    Nothing in the Bot writes here; there is no slash command for it.
    """

    def __init__(self, root: Path, limits: MemoryLimits) -> None:
        self._root = root
        self._limits = limits

    def index_text(self) -> str:
        try:
            return (self._root / INDEX_FILE).read_text("utf-8").strip()
        except OSError:
            return ""

    def topic_count(self) -> int:
        return len(self._topics())

    def _topics(self) -> list[Path]:
        topics = self._root / TOPIC_DIR
        return sorted(p for p in topics.glob("*.md") if p.is_file()) if topics.is_dir() else []

    def search(self, query: str) -> str:
        sources = [
            (path.stem, path.name, path.read_text("utf-8", errors="ignore").splitlines())
            for path in self._topics()
        ]
        return search_snippets(sources, query, self._limits, f"（「{query}」沒有命中任何永久記憶）")

    def recall(self, name: str, offset: int = 1, lines: int | None = None) -> str:
        if name == LIST_NAME:
            return "\n".join(f"- {p.stem}" for p in self._topics()) or "（沒有永久記憶檔）"
        match = next((p for p in self._topics() if name in (p.stem, p.name)), None)
        if match is None:
            return f"（找不到永久記憶「{name}」）"
        all_lines = match.read_text("utf-8", errors="ignore").splitlines()
        page = min(lines or self._limits.read_max_lines, self._limits.read_max_lines)
        start = max(1, offset)
        chunk = all_lines[start - 1 : start - 1 + page]
        body = "\n".join(f"{i:>4}: {line}" for i, line in enumerate(chunk, start))
        if not chunk:
            return f"[{match.name} 共 {len(all_lines)} 行；offset {start} 已超過檔尾]"
        header = f"[{match.name} 第 {start}–{start + len(chunk) - 1} 行，共 {len(all_lines)} 行]"
        return _truncate(f"{header}\n{body}", self._limits.read_max_bytes)


def query_pattern(query: str) -> re.Pattern[str]:
    """Words separated by "|", each matched literally (case-insensitive). The query comes from the
    model (and therefore indirectly from members), so it is never compiled as a regex: a
    catastrophic pattern against member-written notes would stall the whole event loop."""
    words = [w.strip() for w in query.split("|") if w.strip()]
    if not words:
        return re.compile(r"(?!x)x")  # matches nothing
    return re.compile("|".join(re.escape(w) for w in words), re.I)


def _term_score(line: str, query: str, pattern: re.Pattern[str]) -> int:
    """3 = the line is the term itself, 2 = the line starts with it, 1 = mentioned, 0 = no."""
    bare = re.sub(r"^[\s#*>\-\d.:|]+|[\s*|]+$", "", line).casefold()
    if not pattern.search(line):
        return 0
    best = 1
    for word in (w.strip().casefold() for w in query.split("|") if w.strip()):
        if bare == word:
            return 3
        if bare.startswith(word) and len(bare) <= len(word) + 12:
            best = 2
    return best


def search_snippets(
    sources: list[tuple[str, str, list[str]]], query: str, limits: MemoryLimits, none: str
) -> str:
    """Rank hits so a line that *is* the term (a glossary/character entry) comes first with its
    definition block, then lines that start with it, then mere mentions with ± context. Without
    this a term mentioned dozens of times in a long article buries its own definition."""
    pattern = query_pattern(query)
    context = limits.search_context_lines
    hits: list[tuple[int, int, int, str]] = []
    for order, (name, file, lines) in enumerate(sources):
        for number, line in enumerate(lines, 1):
            score = _term_score(line, query, pattern)
            if not score:
                continue
            if score >= 2:
                # definition block: the term line plus what follows until the next blank line
                end = number
                while end < len(lines) and lines[end].strip() and end - number < 8:
                    end += 1
                start = number - 1
            else:
                start, end = max(0, number - 1 - context), min(len(lines), number + context)
            block = "\n".join(
                f"{'>' if i == number else ' '} {i:>4}: {lines[i - 1]}"
                for i in range(start + 1, end + 1)
            )
            hits.append((-score, order, number, f"## {name} ({file}) line {number}\n{block}"))
    if not hits:
        return none
    hits.sort()
    shown = hits[: limits.search_max_matches]
    text = "\n\n".join(h[3] for h in shown)
    if len(hits) > len(shown):
        text += f"\n\n[顯示 {len(shown)} / {len(hits)} 個命中，已依相關度排序；請縮小查詢]"
    return _truncate(text, limits.read_max_bytes)


def _truncate(text: str, max_bytes: int) -> str:
    data = text.encode("utf-8")
    if len(data) <= max_bytes:
        return text
    cut = data[:max_bytes].decode("utf-8", errors="ignore")
    return f"{cut}\n[已截斷至 {max_bytes} bytes，用 offset 繼續讀]"


def extract_memory_tags(answer: str) -> tuple[str, list[tuple[str, str, str]]]:
    """Split an answer into (visible text, [(scope, name, fact), ...])."""
    found = [(s, n, f) for s, n, f in MEMORY_TAG.findall(answer) if f.strip()]
    return MEMORY_TAG.sub("", answer).strip(), found


def extract_read_requests(answer: str) -> list[tuple[str, str, str, int, int | None]]:
    """[(kind, scope, target, offset, lines)] for every <search/> and <recall/> in an answer."""
    requests: list[tuple[str, str, str, int, int | None]] = [
        ("search", scope, query, 1, None) for scope, query in SEARCH_TAG.findall(answer)
    ]
    for scope, name, offset, lines in RECALL_TAG.findall(answer):
        requests.append(("recall", scope, name, int(offset or 1), int(lines) if lines else None))
    return requests
