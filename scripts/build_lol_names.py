"""Build the Taiwan/mainland name tables the model recalls before asking hexdata.

Members write Taiwan-server names (犽宿, 靈光一閃, 無盡之刃); hexdata.com.cn indexes everything
by the mainland names (亚索, 尤里卡, 无尽之刃) and by Riot's numeric ids. 126 of 173 champion
names still differ after a script conversion (measured 2026-09-15), so these are lookup tables,
not transliterations. Three topics, one per kind:

- champions: Riot Data Dragon zh_TW / zh_CN `champion.json` joined on the numeric key. The two
  locales use the fields the other way round (zh_TW `name` = champion name, `title` = epithet;
  zh_CN the reverse). hexdata's `ai-summary.json` `heroAliases` adds its page path and nicknames.
- augments (海克斯): CommunityDragon `cherry-augments.json` zh_tw / zh_cn joined on `id`, kept
  only for the augments hexdata lists (the ARAM Mayhem pool, ~210 of 552).
- items: Data Dragon zh_TW / zh_CN `item.json`, kept only for the items hexdata lists (~120).

    uv run python scripts/build_lol_names.py

writes permanent/topics/{英雄,海克斯,裝備}譯名對照.md. permanent/ is operator-managed and baked
into the image (permanent/README.md), so rebuild afterwards. Re-run when a patch adds content;
the summary line prints what hexdata lists that the sources lack, so drift is visible.
"""

from __future__ import annotations

import json
import re
import sys
import urllib.request
from pathlib import Path

DDRAGON = "https://ddragon.leagueoflegends.com"
CDRAGON = "https://raw.communitydragon.org/latest/plugins/rcp-be-lol-game-data/global"
HEXDATA = "https://hexdata.com.cn"
TOPICS = Path(__file__).resolve().parents[1] / "permanent" / "topics"
NAMES_JSON = Path(__file__).resolve().parents[1] / "config" / "lol-names.json"
USER_AGENT = "discord-codex-bot/1.0 (build_lol_names)"
NOT_ON_HEXDATA = "（hexdata 尚無）"
RARITY = {"kSilver": "銀", "kGold": "金", "kPrismatic": "棱彩", "kEventChoice": "活動"}
_HREF = r'href="/{kind}/(\d+)(-[a-z0-9-]+)?"'


def fetch(url: str) -> str:
    request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    with urllib.request.urlopen(request, timeout=30) as response:
        return response.read().decode("utf-8", errors="replace")


def fetch_json(url: str):
    return json.loads(fetch(url))


def hexdata_paths(html: str, kind: str) -> dict[str, str]:
    """{id: "<kind>/<id>[-slug]"} for every link of that kind on a hexdata listing page. The
    slug is taken from the page, never derived: a guessed path would 404 quietly."""
    return {id_: f"{kind}/{id_}{slug}" for id_, slug in re.findall(_HREF.format(kind=kind), html)}


def champion_rows(tw: dict, cn: dict, aliases: list[dict]) -> list[list[str]]:
    by_key = {str(alias["id"]): alias for alias in aliases}
    rows = []
    for champion_id, t in tw.items():
        c = cn[champion_id]
        key = str(t["key"])
        hx = by_key.get(key)
        rows.append(
            [
                key,
                t["name"],
                t["title"],
                c["title"],  # zh_CN carries the name in `title`...
                c["name"],  # ...and the epithet in `name`
                hx["url"].rsplit("/", 1)[1] if hx else "",
                "、".join(hx["alternateNames"]) if hx else "",
            ]
        )
    return sorted(rows, key=lambda row: int(row[0]))


def augment_rows(tw: list[dict], cn: list[dict], paths: dict[str, str]) -> list[list[str]]:
    cn_name = {str(a["id"]): a["nameTRA"] for a in cn}
    rows = []
    for a in tw:
        id_ = str(a["id"])
        if id_ in paths:
            rows.append(
                [
                    id_,
                    a["nameTRA"],
                    cn_name.get(id_, ""),
                    RARITY.get(a["rarity"], a["rarity"]),
                    paths[id_],
                ]
            )
    return sorted(rows, key=lambda row: int(row[0]))


def item_rows(tw: dict, cn: dict, paths: dict[str, str]) -> list[list[str]]:
    rows = [
        [id_, tw[id_]["name"], cn[id_]["name"], path] for id_, path in paths.items() if id_ in tw
    ]
    return sorted(rows, key=lambda row: int(row[0]))


def cn_to_tw(*pairs: list[tuple[str, str]]) -> dict[str, str]:
    """{mainland name: Taiwan name} across every table, for rewriting hexdata's text before the
    model sees it. Identical names need no entry; one-character names (易, 彗) are left out
    because they would match inside unrelated words."""
    out: dict[str, str] = {}
    for table in pairs:
        for cn, tw in table:
            if cn != tw and len(cn) > 1 and tw:
                out.setdefault(cn, tw)
    return out


def render(heading: str, intro: str, columns: list[str], rows: list[list[str]]) -> str:
    lines = [
        f"# {heading}",
        "",
        intro,
        "",
        "| " + " | ".join(columns) + " |",
        "|" + "---|" * len(columns),
    ]
    lines += [
        "| "
        + " | ".join(
            cell or NOT_ON_HEXDATA if i == len(row) - 2 else cell for i, cell in enumerate(row)
        )
        + " |"
        for row in rows
    ]
    return "\n".join(lines) + "\n"


def main() -> int:
    version = fetch_json(f"{DDRAGON}/api/versions.json")[0]
    summary = fetch_json(f"{HEXDATA}/data/ai-summary.json")
    patch, day = summary["reportPatch"], summary["reportDate"]
    source = (
        f"hexdata.com.cn（Patch {patch}，{day}）。由 scripts/build_lol_names.py 產生，勿手改。"
        " 名稱全是查表對照：成員用台服名問，hexdata 只認陸服名與數字 id。"
    )
    TOPICS.mkdir(parents=True, exist_ok=True)

    tw = fetch_json(f"{DDRAGON}/cdn/{version}/data/zh_TW/champion.json")["data"]
    cn = fetch_json(f"{DDRAGON}/cdn/{version}/data/zh_CN/champion.json")["data"]
    champions = champion_rows(tw, cn, summary["heroAliases"])
    (TOPICS / "英雄譯名對照.md").write_text(
        render(
            "英雄譯名對照（台服 ↔ 陸服）",
            f"來源：Riot Data Dragon {version}（zh_TW／zh_CN）＋ {source} "
            "查到列後，hexdata 英雄頁路徑就是 `hero/<hexdata 路徑>`"
            "（例：逆命 → `hero/4-twistedfate`）。"
            "陸服暱稱是玩家俗稱，成員提問裡也可能出現。",
            ["key", "台服名", "台服稱號", "陸服名", "陸服稱號", "hexdata 路徑", "陸服暱稱"],
            champions,
        ),
        "utf-8",
    )

    aug_paths = hexdata_paths(fetch(f"{HEXDATA}/augments"), "augment")
    aug_tw = fetch_json(f"{CDRAGON}/zh_tw/v1/cherry-augments.json")
    aug_cn = fetch_json(f"{CDRAGON}/zh_cn/v1/cherry-augments.json")
    augments = augment_rows(aug_tw, aug_cn, aug_paths)
    (TOPICS / "海克斯譯名對照.md").write_text(
        render(
            "海克斯（強化符文）譯名對照（台服 ↔ 陸服）",
            f"來源：CommunityDragon cherry-augments（zh_tw／zh_cn）＋ {source} "
            "只列 hexdata 有統計的海克斯。hexdata 英雄頁的「推荐海克斯」用陸服名，"
            "回答成員時對照成台服名；符文頁路徑是「hexdata 路徑」欄。"
            "稀有度：銀／金／棱彩。",
            ["id", "台服名", "陸服名", "稀有度", "hexdata 路徑"],
            augments,
        ),
        "utf-8",
    )

    item_paths = hexdata_paths(fetch(f"{HEXDATA}/items"), "item")
    item_tw = fetch_json(f"{DDRAGON}/cdn/{version}/data/zh_TW/item.json")["data"]
    item_cn = fetch_json(f"{DDRAGON}/cdn/{version}/data/zh_CN/item.json")["data"]
    items = item_rows(item_tw, item_cn, item_paths)
    (TOPICS / "裝備譯名對照.md").write_text(
        render(
            "裝備譯名對照（台服 ↔ 陸服）",
            f"來源：Riot Data Dragon {version} item.json（zh_TW／zh_CN）＋ {source} "
            "只列 hexdata 有統計的裝備。hexdata 英雄頁的裝備適配度用陸服名，"
            "回答成員時對照成台服名。",
            ["id", "台服名", "陸服名", "hexdata 路徑"],
            items,
        ),
        "utf-8",
    )

    # The same tables the other way round, as a machine map: apis.json points hexdata at it and
    # call_api rewrites 陸服名 to 台服名（陸服名） in every hexdata reply. The model was told to
    # look names up in the tables and still answered with mainland names 4 times out of 4.
    names = cn_to_tw(
        [(row[3], row[1]) for row in champions],
        [(row[2], row[1]) for row in augments],
        [(row[2], row[1]) for row in items],
    )
    NAMES_JSON.write_text(json.dumps(names, ensure_ascii=False, indent=0) + "\n", "utf-8")

    no_champion = [row[1] for row in champions if not row[5]]
    no_augment = sorted(set(aug_paths) - {row[0] for row in augments}, key=int)
    no_item = sorted(set(item_paths) - {row[0] for row in items}, key=int)
    print(
        f"{TOPICS}: champions {len(champions)}"
        f" (not on hexdata: {'、'.join(no_champion) or 'none'}),"
        f" augments {len(augments)}/{len(aug_paths)}"
        f" (hexdata ids missing in CDragon: {no_augment or 'none'}),"
        f" items {len(items)}/{len(item_paths)}"
        f" (hexdata ids missing in Data Dragon: {no_item or 'none'});"
        f" Data Dragon {version}, hexdata Patch {patch} ({day});"
        f" {NAMES_JSON.name}: {len(names)} names"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
