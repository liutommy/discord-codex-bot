from pathlib import Path

from discord_codex_bot.apis import load_registry
from scripts.build_champion_names import join_names, render

TW = {
    "Yasuo": {"key": "157", "name": "犽宿", "title": "放逐浪人"},
    "TwistedFate": {"key": "4", "name": "逆命", "title": "卡牌大師"},
    "Rock": {"key": "999", "name": "洛克", "title": "新英雄"},
}
CN = {
    "Yasuo": {"key": "157", "name": "疾风剑豪", "title": "亚索"},
    "TwistedFate": {"key": "4", "name": "卡牌大师", "title": "崔斯特"},
    "Rock": {"key": "999", "name": "岩石", "title": "洛克"},
}
ALIASES = [
    {
        "id": "157",
        "name": "疾风剑豪",
        "alternateNames": ["亚索", "快乐风男"],
        "url": "https://hexdata.com.cn/hero/157-yasuo",
    },
    {
        "id": "4",
        "name": "卡牌大师",
        "alternateNames": ["崔斯特", "TF"],
        "url": "https://hexdata.com.cn/hero/4-twistedfate",
    },
]


def test_rows_join_on_the_numeric_key_and_unswap_the_cn_fields() -> None:
    rows = join_names(TW, CN, ALIASES)
    assert [r["key"] for r in rows] == [4, 157, 999]
    tf = rows[0]
    assert tf["tw_name"] == "逆命" and tf["cn_name"] == "崔斯特" and tf["cn_title"] == "卡牌大师"
    assert tf["path"] == "4-twistedfate" and tf["nicknames"] == "崔斯特、TF"
    assert rows[2]["path"] == "" and rows[2]["nicknames"] == ""  # not on hexdata yet: no guess


def test_render_is_one_table_row_per_champion_with_the_hexdata_path() -> None:
    text = render(join_names(TW, CN, ALIASES), "16.18.1", "16.18", "2026-09-12")
    assert "| 4 | 逆命 | 卡牌大師 | 崔斯特 | 卡牌大师 | 4-twistedfate | 崔斯特、TF |" in text
    assert "| 999 | 洛克 | 新英雄 | 洛克 | 岩石 | （hexdata 尚無此英雄） |  |" in text
    assert "Data Dragon 16.18.1" in text and "Patch 16.18" in text
    assert text.count("\n| ") == 4  # header + 3 champions, nothing else in table form


def test_shipped_registry_loads_and_documents_hexdata() -> None:
    # The registry is plain JSON baked into the image; a typo there registers nothing and
    # the model silently loses every data API. This is the only test that reads the real file.
    registry = load_registry(Path(__file__).resolve().parents[1] / "config" / "apis.json")
    assert {"lolesports", "leaguepedia", "hexdata"} <= set(registry)
    assert all(api.base.startswith("https://") and api.doc for api in registry.values())
    hexdata = registry["hexdata"]
    assert hexdata.base == "https://hexdata.com.cn/"
    assert "data/ai-summary.json" in hexdata.doc and "hero/" in hexdata.doc
    assert "英雄譯名對照" in hexdata.doc  # the model is told where the TW/CN table lives
