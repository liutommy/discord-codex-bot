from pathlib import Path

from discord_codex_bot.memory import (
    ARCHIVE_FILE,
    INDEX_FILE,
    MemoryLimits,
    MemoryStore,
    extract_memory_tags,
    extract_recall_tags,
    slugify,
)

LIMITS = MemoryLimits(
    index_max_lines=3,
    index_max_bytes=25_000,
    user_max_bytes=600,
    guild_max_bytes=200_000_000,
    recall_max_bytes=20,
)


def test_add_creates_topic_file_and_index_line(tmp_path: Path) -> None:
    store = MemoryStore(tmp_path, LIMITS)
    line = store.add("user", 1, 2, "綠茶", "使用者最喜歡綠茶")
    assert line == "- [綠茶](綠茶.md) — 使用者最喜歡綠茶"
    assert (tmp_path / "1" / "users" / "2" / "topics" / "綠茶.md").exists()
    assert "綠茶" in store.render(1, 2)
    assert store.render(1, 3) == ""  # another member sees nothing personal
    store.add("guild", 1, None, "規則", "週五晚上開團")
    assert "[伺服器記憶索引]" in store.render(1, 3)


def test_index_overflow_moves_oldest_to_archive_and_stays_recallable(tmp_path: Path) -> None:
    store = MemoryStore(tmp_path, LIMITS)
    for i in range(5):
        store.add("guild", 1, None, f"n{i}", f"fact {i}")
    index = (tmp_path / "1" / "guild" / INDEX_FILE).read_text("utf-8")
    archive = (tmp_path / "1" / "guild" / ARCHIVE_FILE).read_text("utf-8")
    assert [e.name for e in store.entries("guild", 1, None)] == ["n2", "n3", "n4"]
    assert "n0" in archive and "n1" in archive and "n0" not in index
    assert "n0" in store.recall("guild", 1, None, "list")
    assert store.recall("guild", 1, None, "n0").startswith("# n0")
    assert len(store.recall("guild", 1, None, "n4").encode("utf-8")) <= LIMITS.recall_max_bytes


def test_capacity_refuses_and_forget_frees(tmp_path: Path) -> None:
    store = MemoryStore(tmp_path, LIMITS)
    assert store.add("user", 1, 2, "a", "x" * 150).startswith("- [a]")
    assert store.add("user", 1, 2, "b", "y" * 150).startswith("- [b]")
    assert "容量上限" in store.add("user", 1, 2, "c", "z" * 150)
    assert store.forget("user", 1, 2, "a")
    assert not store.forget("user", 1, 2, "a")
    assert store.add("user", 1, 2, "c", "z" * 150).startswith("- [c]")
    assert store.recall("user", 1, 2, "nope").startswith("（找不到")


def test_slug_and_tag_parsing() -> None:
    assert slugify("Hello World!") == "hello-world"
    assert slugify("") == "memory"
    text, facts = extract_memory_tags(
        '好的。<memory scope="user" name="綠茶">喜歡綠茶</memory>'
        '<memory scope="guild" name="開團">週五開團</memory>'
    )
    assert text == "好的。"
    assert facts == [("user", "綠茶", "喜歡綠茶"), ("guild", "開團", "週五開團")]
    assert extract_recall_tags('<recall scope="user" name="綠茶"/>') == [("user", "綠茶")]
    assert extract_recall_tags("plain answer") == []
