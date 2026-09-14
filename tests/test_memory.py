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
    hits = store.search("guild", 1, None, "fact 1|fact 3")
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


def test_memory_store_recall_offset_past_end_reports_it(tmp_path: Path) -> None:
    store = MemoryStore(tmp_path, LIMITS)
    store.add("user", 1, 2, "note", "l1\nl2\nl3")
    path = tmp_path / "1" / "users" / "2" / "topics" / "note.md"
    total = len(path.read_text("utf-8").splitlines())
    assert store.recall("user", 1, 2, "note", offset=total + 5) == (
        f"[note.md 共 {total} 行；offset {total + 5} 已超過檔尾]"
    )


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


def test_search_ranks_definition_lines_before_mentions() -> None:
    from discord_codex_bot.memory import search_snippets

    article = ["TDN 是元兇。", "很多人提到 TDN。", "", "TDN 又出現了。"]
    glossary = [
        "## 人物",
        "TDN",
        "    一切的元兇。本篇役名「三浦」。",
        "    被要求學狗叫。",
        "",
        "DB",
        "    追尾的人。",
    ]
    sources = [("文章", "a.md", article), ("角色", "b.md", glossary)]
    out = search_snippets(sources, "TDN", LIMITS, "none")
    first = out.split("\n\n")[0]
    assert first.startswith("## 角色 (b.md) line 2") and "一切的元兇" in first and "學狗叫" in first
    assert "DB" not in first
    assert search_snippets([("x", "x.md", ["nothing"])], "TDN", LIMITS, "none") == "none"


# ----- paging, index window, search fallback, rewrite slugs --------------------------------------

from dataclasses import replace  # noqa: E402

from discord_codex_bot.memory import (  # noqa: E402
    Note,
    PermanentMemory,
    query_pattern,
    search_snippets,
)


def test_permanent_recall_pages_and_truncates(tmp_path: Path) -> None:
    (tmp_path / "topics").mkdir()
    (tmp_path / "topics" / "f.md").write_text("l1\nl2\nl3\nl4\nl5\n", "utf-8")
    perm = PermanentMemory(tmp_path, LIMITS)  # read_max_lines=2
    page = perm.recall("f", offset=2, lines=5)
    assert page == "[f.md 第 2–3 行，共 5 行]\n   2: l2\n   3: l3"  # lines capped at the limit
    assert perm.recall("f.md", offset=5, lines=1) == "[f.md 第 5–5 行，共 5 行]\n   5: l5"
    assert perm.recall("f", offset=0, lines=1) == "[f.md 第 1–1 行，共 5 行]\n   1: l1"
    assert perm.recall("f", offset=9) == "[f.md 共 5 行；offset 9 已超過檔尾]"  # past the end
    small = PermanentMemory(tmp_path, replace(LIMITS, read_max_bytes=30))
    cut = small.recall("f")
    head, note = cut.split("\n[已截斷至 ")
    assert len(head.encode("utf-8")) <= 30 and note == "30 bytes，用 offset 繼續讀]"
    assert perm.recall("f").startswith(head)


def test_index_text_window_stops_at_line_and_byte_limits(tmp_path: Path) -> None:
    lines = [f"- [n{i}](n{i}.md) — hook {i}" for i in range(4)]
    directory = tmp_path / "1" / "guild"
    directory.mkdir(parents=True)
    (directory / INDEX_FILE).write_text("\n".join(lines) + "\n", "utf-8")
    two = len("\n".join(lines[:2]).encode("utf-8"))  # measured like _write_index writes it
    store = MemoryStore(tmp_path, replace(LIMITS, index_max_lines=200, index_max_bytes=two))
    assert store.index_text("guild", 1, None) == "\n".join(lines[:2])
    store = MemoryStore(tmp_path, replace(LIMITS, index_max_lines=200, index_max_bytes=two - 1))
    assert store.index_text("guild", 1, None) == lines[0]
    store = MemoryStore(tmp_path, replace(LIMITS, index_max_lines=1, index_max_bytes=25_000))
    assert store.index_text("guild", 1, None) == lines[0]
    assert len(store.entries("guild", 1, None)) == 4  # the window hides, it does not delete


def test_search_treats_queries_as_literal_words_never_regex() -> None:
    sources = [("x", "x.md", ["see a[1] here", "plain a1", "nothing", "(unclosed"])]
    out = search_snippets(sources, "a[", LIMITS, "none")
    assert "## x (x.md) line 1" in out and "line 2" not in out
    assert search_snippets(sources, "a.", LIMITS, "none") == "none"  # "." is not a wildcard
    assert search_snippets(sources, "(unclosed", LIMITS, "none").startswith("## x (x.md) line 4")
    both = search_snippets(sources, " a1 | NOTHING ", LIMITS, "none")
    assert "line 2" in both and "line 3" in both and "line 1" not in both
    assert search_snippets(sources, " | ", LIMITS, "none") == "none"
    assert query_pattern("").search("anything") is None


def test_search_truncates_and_reports_hidden_hits() -> None:
    sources = [("x", "x.md", [f"term {i}" for i in range(5)])]
    out = search_snippets(sources, "term", replace(LIMITS, search_max_matches=2), "none")
    assert out.count("## x (x.md)") == 2
    assert out.endswith("[顯示 2 / 5 個命中，已依相關度排序；請縮小查詢]")
    cut = search_snippets(sources, "term", replace(LIMITS, read_max_bytes=20), "none")
    assert cut.endswith("[已截斷至 20 bytes，用 offset 繼續讀]")


def test_rewrite_keeps_slugs_unique_and_backs_up_the_previous_state(tmp_path: Path) -> None:
    store = MemoryStore(tmp_path, LIMITS)
    store.add("guild", 1, None, "old", "x")
    directory = tmp_path / "1" / "guild"
    store.rewrite(
        "guild",
        1,
        None,
        [
            Note("綠茶", "2026-09-01", "a"),
            Note("綠茶", "2026-09-02", "b"),
            Note("綠茶!", "2026-09-03", "c"),
        ],
    )
    entries = store.entries("guild", 1, None)
    assert [e.file for e in entries] == ["綠茶.md", "綠茶-2.md", "綠茶-3.md"]
    assert [e.name for e in entries] == ["綠茶", "綠茶", "綠茶!"]
    assert store.notes("guild", 1, None) == [
        Note("綠茶", "2026-09-01", "a"),
        Note("綠茶", "2026-09-02", "b"),
        Note("綠茶!", "2026-09-03", "c"),
    ]
    assert (directory / ".backup" / "topics" / "old.md").exists()
    assert not (directory / "topics" / "old.md").exists()
    assert store.recall("guild", 1, None, "綠茶-2.md").startswith("[綠茶-2.md 第 1–2 行")
    store.rewrite("guild", 1, None, [Note("n", "2026-09-04", "t")])
    assert (directory / ".backup" / "topics" / "綠茶-3.md").exists()
    assert not (directory / ".backup" / "topics" / "old.md").exists()  # one backup generation
    assert (
        store.usage_bytes("guild", 1, None)
        == sum(p.stat().st_size for p in (directory / "topics").iterdir())
        + (directory / INDEX_FILE).stat().st_size
    )


def test_add_avoids_slugs_already_in_the_archive(tmp_path: Path) -> None:
    store = MemoryStore(tmp_path, LIMITS)  # index_max_lines=3
    for _ in range(4):
        store.add("guild", 1, None, "同名", "x")
    files = [e.file for e in store.all_entries("guild", 1, None)]  # index first, then archive
    assert files == ["同名-2.md", "同名-3.md", "同名-4.md", "同名.md"]
    assert store.forget("guild", 1, None, "同名.md")  # archived entry, by file name
    assert not (tmp_path / "1" / "guild" / "topics" / "同名.md").exists()
    assert "同名.md" not in (tmp_path / "1" / "guild" / ARCHIVE_FILE).read_text("utf-8")


def test_catastrophic_regex_query_is_literal_and_fast() -> None:
    import time

    sources = [("x", "x.md", ["a" * 40 + "b", "(a+)+$ literally", "aaaa"])]
    started = time.perf_counter()
    out = search_snippets(sources, "(a+)+$", LIMITS, "none")
    assert time.perf_counter() - started < 0.5
    assert out.startswith("## x (x.md) line 2") and "line 1" not in out and "line 3" not in out
    assert search_snippets(sources, "(a+)+$|zzz", LIMITS, "none").count("## x") == 1
    assert search_snippets([("x", "x.md", ["a" * 40 + "b"])], "(a+)+$", LIMITS, "none") == "none"
