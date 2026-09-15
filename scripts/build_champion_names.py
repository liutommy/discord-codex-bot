"""Build the Taiwan/mainland champion-name table the model recalls before asking hexdata.

Members write Taiwan-server names (犽宿, 逆命, 好運姐); hexdata.com.cn indexes champions by the
mainland names (亚索, 崔斯特, 厄运小姐) and by Riot's numeric key. 126 of 173 names still differ
after a script conversion (measured 2026-09-15), so this is a lookup table, not a transliteration.

Sources, joined on the numeric key:
- Riot Data Dragon zh_TW and zh_CN `champion.json`. The two locales use the fields the other way
  round: in zh_TW `name` is the champion's name and `title` the epithet; in zh_CN `name` is the
  epithet and `title` the name.
- hexdata's `ai-summary.json` `heroAliases`, for its own page path (`hero/<key>-<slug>`) and the
  nicknames players use.

    uv run python scripts/build_champion_names.py

writes permanent/topics/英雄譯名對照.md. permanent/ is operator-managed and baked into the image
(permanent/README.md), so rebuild afterwards. Re-run when a patch adds a champion.
"""

from __future__ import annotations

import json
import sys
import urllib.request
from pathlib import Path

DDRAGON = "https://ddragon.leagueoflegends.com"
HEXDATA_SUMMARY = "https://hexdata.com.cn/data/ai-summary.json"
OUT = Path(__file__).resolve().parents[1] / "permanent" / "topics" / "英雄譯名對照.md"
USER_AGENT = "discord-codex-bot/1.0 (build_champion_names)"
NOT_ON_HEXDATA = "（hexdata 尚無此英雄）"


def fetch_json(url: str) -> dict | list:
    request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    with urllib.request.urlopen(request, timeout=30) as response:
        return json.load(response)


def join_names(tw: dict, cn: dict, aliases: list[dict]) -> list[dict]:
    """One row per champion, sorted by Riot key. `tw` / `cn` are Data Dragon `data` maps keyed
    by champion id (Yasuo); `aliases` is hexdata's heroAliases list. A champion hexdata does not
    list yet gets an empty path rather than a guessed one."""
    by_key = {str(alias["id"]): alias for alias in aliases}
    rows = []
    for champion_id, t in tw.items():
        c = cn[champion_id]
        key = str(t["key"])
        hx = by_key.get(key)
        rows.append(
            {
                "key": int(key),
                "tw_name": t["name"],
                "tw_title": t["title"],
                "cn_name": c["title"],  # zh_CN carries the name in `title`
                "cn_title": c["name"],  # ...and the epithet in `name`
                "path": hx["url"].rsplit("/", 1)[1] if hx else "",
                "nicknames": "、".join(hx["alternateNames"]) if hx else "",
            }
        )
    return sorted(rows, key=lambda row: row["key"])


def render(rows: list[dict], version: str, patch: str, report_date: str) -> str:
    lines = [
        "# 英雄譯名對照（台服 ↔ 陸服）",
        "",
        f"來源：Riot Data Dragon {version}（zh_TW／zh_CN）＋ hexdata.com.cn ai-summary"
        f"（Patch {patch}，{report_date}）。由 scripts/build_champion_names.py 產生，勿手改。",
        "",
        "用法：成員用台服名問，hexdata 只認陸服名與數字 key。查到列後，hexdata 英雄頁路徑就是"
        " `hero/<hexdata 路徑>`（例：逆命 → `hero/4-twistedfate`）。陸服暱稱是玩家俗稱，"
        "成員提問裡也可能出現。",
        "",
        "| key | 台服名 | 台服稱號 | 陸服名 | 陸服稱號 | hexdata 路徑 | 陸服暱稱 |",
        "|---|---|---|---|---|---|---|",
    ]
    for r in rows:
        cells = (
            str(r["key"]),
            r["tw_name"],
            r["tw_title"],
            r["cn_name"],
            r["cn_title"],
            r["path"] or NOT_ON_HEXDATA,
            r["nicknames"],
        )
        lines.append("| " + " | ".join(cells) + " |")
    return "\n".join(lines) + "\n"


def main() -> int:
    version = fetch_json(f"{DDRAGON}/api/versions.json")[0]
    tw = fetch_json(f"{DDRAGON}/cdn/{version}/data/zh_TW/champion.json")["data"]
    cn = fetch_json(f"{DDRAGON}/cdn/{version}/data/zh_CN/champion.json")["data"]
    summary = fetch_json(HEXDATA_SUMMARY)
    rows = join_names(tw, cn, summary["heroAliases"])
    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(render(rows, version, summary["reportPatch"], summary["reportDate"]), "utf-8")
    missing = [r["tw_name"] for r in rows if not r["path"]]
    print(
        f"{OUT}: {len(rows)} champions, Data Dragon {version}, hexdata Patch "
        f"{summary['reportPatch']} ({summary['reportDate']}); not on hexdata: "
        f"{'、'.join(missing) or 'none'}"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
