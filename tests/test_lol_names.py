from pathlib import Path

from discord_codex_bot.apis import load_registry
from scripts.build_lol_names import (
    NOT_ON_HEXDATA,
    augment_rows,
    champion_rows,
    hexdata_paths,
    item_rows,
    render,
)

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
        "alternateNames": ["亚索", "快乐风男"],
        "url": "https://hexdata.com.cn/hero/157-yasuo",
    },
    {
        "id": "4",
        "alternateNames": ["崔斯特", "TF"],
        "url": "https://hexdata.com.cn/hero/4-twistedfate",
    },
]


def test_champion_rows_join_on_the_numeric_key_and_unswap_the_cn_fields() -> None:
    rows = champion_rows(TW, CN, ALIASES)
    assert [r[0] for r in rows] == ["4", "157", "999"]
    assert rows[0] == ["4", "逆命", "卡牌大師", "崔斯特", "卡牌大师", "4-twistedfate", "崔斯特、TF"]
    assert rows[2][5] == "" and rows[2][6] == ""  # not on hexdata yet: no guessed path


def test_hexdata_paths_take_the_slug_from_the_page_and_tolerate_no_slug() -> None:
    html = (
        '<a href="/augment/1030-eureka">x</a>'
        ' <a href="/augment/1004-back-to-basics">y</a>'
        ' <a href="/item/1001">z</a>'
    )
    assert hexdata_paths(html, "augment") == {
        "1030": "augment/1030-eureka",
        "1004": "augment/1004-back-to-basics",
    }
    assert hexdata_paths(html, "item") == {"1001": "item/1001"}


def test_augment_and_item_rows_keep_only_what_hexdata_lists() -> None:
    aug_tw = [
        {"id": 1030, "nameTRA": "靈光一閃", "rarity": "kPrismatic"},
        {"id": 1004, "nameTRA": "基本功夫", "rarity": "kPrismatic"},
        {"id": 2000, "nameTRA": "不在池裡", "rarity": "kSilver"},
    ]
    aug_cn = [
        {"id": 1030, "nameTRA": "尤里卡"},
        {"id": 1004, "nameTRA": "回归基本功"},
        {"id": 2000, "nameTRA": "x"},
    ]
    rows = augment_rows(
        aug_tw, aug_cn, {"1004": "augment/1004-back-to-basics", "1030": "augment/1030-eureka"}
    )
    assert rows == [
        ["1004", "基本功夫", "回归基本功", "棱彩", "augment/1004-back-to-basics"],
        ["1030", "靈光一閃", "尤里卡", "棱彩", "augment/1030-eureka"],
    ]
    item_tw = {"3031": {"name": "無盡之刃"}, "1001": {"name": "鞋子"}}
    item_cn = {"3031": {"name": "无尽之刃"}, "1001": {"name": "鞋子"}}
    rows = item_rows(
        item_tw, item_cn, {"3031": "item/3031", "1001": "item/1001", "7777": "item/7777"}
    )
    assert rows == [
        ["1001", "鞋子", "鞋子", "item/1001"],
        ["3031", "無盡之刃", "无尽之刃", "item/3031"],
    ]


def test_render_is_one_table_row_per_entry_and_marks_a_missing_hexdata_path() -> None:
    text = render(
        "標題",
        "說明",
        ["key", "台服名", "路徑", "暱稱"],
        [["4", "逆命", "4-twistedfate", "TF"], ["999", "洛克", "", ""]],
    )
    assert text.startswith("# 標題\n\n說明\n\n| key | 台服名 | 路徑 | 暱稱 |\n|---|---|---|---|\n")
    assert "| 4 | 逆命 | 4-twistedfate | TF |" in text
    assert f"| 999 | 洛克 | {NOT_ON_HEXDATA} |  |" in text
    assert text.count("\n| ") == 3  # header + 2 rows, nothing else in table form


def test_cn_to_tw_skips_identical_and_single_character_names() -> None:
    from scripts.build_lol_names import cn_to_tw

    names = cn_to_tw(
        [("崔斯特", "逆命"), ("易", "易大師"), ("凯尔", "凱爾")],
        [("回响施放", "共鳴施放"), ("同名", "同名")],
    )
    assert names == {"崔斯特": "逆命", "凯尔": "凱爾", "回响施放": "共鳴施放"}


def test_shipped_registry_loads_and_documents_hexdata() -> None:
    # The registry is plain JSON baked into the image; a typo there registers nothing and the
    # model silently loses every data API. This is the only test that reads the real file.
    registry = load_registry(Path(__file__).resolve().parents[1] / "config" / "apis.json")
    assert {"lolesports", "leaguepedia", "hexdata"} <= set(registry)
    assert all(api.base.startswith("https://") and api.doc for api in registry.values())
    hexdata = registry["hexdata"]
    assert hexdata.base == "https://hexdata.com.cn/"
    assert "data/ai-summary.json" in hexdata.doc and "hero/" in hexdata.doc
    for table in ("英雄譯名對照", "海克斯譯名對照", "裝備譯名對照"):
        assert table in hexdata.doc  # the model is told where each TW/CN table lives
    # the shipped map is the mechanical fallback for names the model would otherwise mistranslate
    assert hexdata.names["崔斯特"] == "逆命" and hexdata.names["回响施放"] == "共鳴施放"
    assert "易" not in hexdata.names and len(hexdata.names) > 300
