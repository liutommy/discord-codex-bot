from pathlib import Path

from discord_codex_bot.memory import (
    ARCHIVE_FILE,
    INDEX_FILE,
    MemoryLimits,
    MemoryStore,
    extract_memory_tags,
    extract_read_requests,
    slugify,
)

LIMITS = MemoryLimits(
    index_max_lines=3,
    index_max_bytes=25_000,
    user_max_bytes=600,
    guild_max_bytes=200_000_000,
    read_max_lines=2,
    read_max_bytes=50_000,
    search_max_matches=50,
    search_context_lines=1,
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
    page = store.recall("guild", 1, None, "n0")
    assert page.startswith("[n0.md 第 1–2 行，共 5 行]") and "   1: # n0" in page
    assert "   3: 2026" in store.recall("guild", 1, None, "n0", offset=3, lines=1)
    hits = store.search("guild", 1, None, "fact [13]")
    assert "## n1 (n1.md) line 5" in hits and "## n3 (n3.md) line 5" in hits and "n2" not in hits
    assert store.search("guild", 1, None, "zzz").startswith("（「zzz」沒有命中")


def test_capacity_evicts_oldest_and_forget_deletes(tmp_path: Path) -> None:
    store = MemoryStore(tmp_path, LIMITS)
    assert store.add("user", 1, 2, "a", "x" * 150).startswith("- [a]")
    assert store.add("user", 1, 2, "b", "y" * 150).startswith("- [b]")
    assert store.add("user", 1, 2, "c", "z" * 150).startswith("- [c]")
    names = [e.name for e in store.all_entries("user", 1, 2)]
    assert "a" not in names and "c" in names  # oldest evicted to make room
    assert not (tmp_path / "1" / "users" / "2" / "topics" / "a.md").exists()
    assert store.forget("user", 1, 2, "b")
    assert not store.forget("user", 1, 2, "b")
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
    assert extract_read_requests(
        '<search scope="guild" query="開團"/>'
        '<recall scope="user" name="綠茶" offset="3" lines="10"/>'
    ) == [("search", "guild", "開團", 1, None), ("recall", "user", "綠茶", 3, 10)]
    assert extract_read_requests('<recall scope="user" name="綠茶"/>') == [
        ("recall", "user", "綠茶", 1, None)
    ]
    assert extract_read_requests("plain answer") == []
    assert extract_read_requests('<search scope="guild" query="吉祥物|名字">') == [
        ("search", "guild", "吉祥物|名字", 1, None)
    ]


def test_personal_style_set_get_clear(tmp_path: Path) -> None:
    store = MemoryStore(tmp_path, LIMITS)
    assert store.get_style(1, 2) == ""
    store.set_style(1, 2, "  條列、少於 100 字  ")
    assert store.get_style(1, 2) == "條列、少於 100 字"
    assert store.get_style(1, 3) == ""
    assert store.clear_style(1, 2)
    assert not store.clear_style(1, 2)
    assert store.get_style(1, 2) == ""


def test_permanent_memory_is_read_only_whole_index_and_searchable(tmp_path: Path) -> None:
    from discord_codex_bot.memory import PermanentMemory

    perm = PermanentMemory(tmp_path, LIMITS)
    assert perm.index_text() == "" and perm.search("x").startswith("（「x」沒有命中")
    (tmp_path / "MEMORY.md").write_text("\n".join(f"- line {i}" for i in range(500)), "utf-8")
    (tmp_path / "topics").mkdir()
    (tmp_path / "topics" / "house-rules.md").write_text("# 規則\n\n第三條：不可以洗頻\n", "utf-8")
    assert perm.index_text().count("\n") == 499  # no window: index_max_lines=3 does not apply
    assert "## house-rules (house-rules.md) line 3" in perm.search("洗頻")
    assert perm.recall("house-rules").startswith("[house-rules.md 第 1–2 行，共 3 行]")
    assert perm.recall("list") == "- house-rules"
    assert perm.recall("nope").startswith("（找不到永久記憶")
    assert extract_read_requests('<recall scope="permanent" name="house-rules"/>') == [
        ("recall", "permanent", "house-rules", 1, None)
    ]
