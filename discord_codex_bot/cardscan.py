"""Client for the Yu-Gi-Oh card recognition service on the host (docs/card-scan.md).

The model asks with <cardscan/> (every image of this turn) or <cardscan image="N"/> (the Nth);
the Bot sends each image to the service, which splits it into cards (a Neuron deck screenshot by
its section headers, a photo by card outlines) and matches each card's art and face against an
index of every printed artwork. When a photo's outline search finds no card, or fewer than
Gemini sees, the Bot asks Gemini to mark the cards and scans again with those boxes.
"""

from __future__ import annotations

import asyncio
import base64
import logging
import math
import re
from pathlib import Path

import aiohttp

from . import gemini, tags
from .config import Config

LOGGER = logging.getLogger(__name__)

CARDSCAN_TAG = re.compile(r'<cardscan(?:\s+image="([1-9][0-9]?)")?\s*/?>(?:\s*</cardscan>)?')
# Images scanned in one answer. A deck photo is one image; more than a few in one question is
# rare, and each scan holds the host's GPU for about a second.
MAX_SCANS_PER_ANSWER = 8
# The service refuses bodies over 20 MB; base64 adds a third.
MAX_IMAGE_BYTES = 14 * 1024 * 1024
SECTION_NAMES = {"main": "主牌組", "extra": "額外牌組", "side": "副牌組"}
# One scan at a time from this Bot: the service serialises scans anyway, and a deck photo that
# also asks Gemini should not be overtaken by the next image of the same answer.
_TURN = asyncio.Semaphore(1)


class CardScanError(RuntimeError):
    """The service is off, unreachable, or refused the image."""


def enabled(config: Config) -> bool:
    return bool(config.cardscan_url)


def render_doc(config: Config) -> str:
    """The <cardscan> instructions in HELP; "" while CARDSCAN_URL is unset."""
    if not enabled(config):
        return ""
    return (
        "[遊戲王認卡]\n"
        "When an attached image shows Yu-Gi-Oh! cards (a deck list screenshot, a photo of cards,"
        " one card) and the member asks what they are, which deck it is or anything that needs"
        " the card names, reply with ONLY <cardscan/> (every image of this question) or"
        ' <cardscan image="N"/> (the Nth image, counted in the order you received them). The'
        " Bot recognises each card from its art against every printed artwork and returns the"
        " cards grouped by main, extra and side deck with copy counts, Japanese and English"
        " names, passcode and Konami cid; cards marked 不確定 come with their candidates, so"
        " check those against the image yourself. Use these names instead of reading card"
        f" names off the picture. At most {MAX_SCANS_PER_ANSWER} images per answer. For"
        " Traditional Chinese names and card text, look the passcode up on 巧巧屋 as described"
        " for ygocdb."
    )


def extract_requests(answer: str) -> list[int]:
    """Image numbers asked for, in order; 0 means every image."""
    seen: list[int] = []
    for number in tags.findall(CARDSCAN_TAG, answer):
        n = int(number) if number else 0
        if n not in seen:
            seen.append(n)
    return seen


def targets(requests: list[int], count: int) -> tuple[list[int], list[int]]:
    """(1-based image numbers to scan, numbers asked for that do not exist)."""
    wanted: list[int] = []
    missing: list[int] = []
    for n in requests:
        for i in range(1, count + 1) if n == 0 else [n]:
            if i > count:
                missing.append(i)
            elif i not in wanted:
                wanted.append(i)
    return wanted, missing


async def _post(config: Config, body: dict) -> dict:
    timeout = aiohttp.ClientTimeout(total=config.cardscan_timeout_seconds)
    try:
        async with aiohttp.ClientSession(timeout=timeout) as session:
            async with session.post(
                f"{config.cardscan_url.rstrip('/')}/scan", json=body
            ) as response:
                answer = await response.json(content_type=None)
                if response.status != 200 or not isinstance(answer, dict):
                    detail = answer.get("error", "") if isinstance(answer, dict) else ""
                    raise CardScanError(f"HTTP {response.status} {detail}".strip())
                return answer
    except (aiohttp.ClientError, TimeoutError, ValueError) as error:
        raise CardScanError(type(error).__name__) from error


async def scan(config: Config, path: Path) -> dict:
    """The service's result for one image, with Gemini's boxes merged in when the outline search
    found nothing or fewer cards than Gemini marks."""
    if not enabled(config):
        raise CardScanError("card recognition is not configured")
    try:
        raw = await asyncio.to_thread(path.read_bytes)
    except OSError as error:
        raise CardScanError(type(error).__name__) from error
    if len(raw) > MAX_IMAGE_BYTES:
        raise CardScanError(f"image over {MAX_IMAGE_BYTES // (1024 * 1024)} MB")
    body = {"image": base64.b64encode(raw).decode("ascii")}
    async with _TURN:
        result = await _post(config, body)
        if result.get("kind") not in ("photo", "whole") or not gemini.available(config):
            return result
        try:
            width, height = (int(v) for v in (result.get("image") or [0, 0])[:2])
            boxes = await gemini.card_boxes(path, config, width, height)
        except Exception:  # the fallback must not cost the result already in hand
            LOGGER.exception("card scan: Gemini boxes failed")
            return result
        found = len(result.get("cards") or []) if result.get("kind") == "photo" else 0
        if not boxes or len(boxes) <= found:
            return result
        LOGGER.info(
            "card scan: outlines found %d, Gemini marked %d; scanning again", found, len(boxes)
        )
        try:
            return await _post(config, body | {"boxes": boxes})
        except CardScanError as error:  # the first result still stands
            LOGGER.warning("card scan: rescan with Gemini boxes failed: %s", error)
            return result


def _text(value: object, limit: int = 120) -> str:
    """Service-supplied text for the RESULT block: on one line, bounded, and with no angle
    brackets, so a card name cannot close the block or pass for a tag of the Bot's."""
    text = " ".join(str(value).split())[:limit]
    return text.replace("<", "＜").replace(">", "＞")


def _int(value: object) -> int:
    return value if isinstance(value, int) and not isinstance(value, bool) else 0


def _score(value: object) -> float:
    ok = isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value)
    return float(value) if ok else 0.0


def _name(entry: dict) -> str:
    jp = _text(entry.get("jp_name") or "")
    en = _text(entry.get("name") or "")
    label = f"{jp}（{en}）" if jp and en else jp or en or "?"
    ids = f"密碼 {_text(entry.get('id', '?'), 20)}"
    if entry.get("cid"):
        ids += f"、cid {_text(entry['cid'], 20)}"
    return f"{label} [{ids}]"


def render(number: int, result: dict | None, error: str = "") -> str:
    """The RESULT block for one scanned image; names come from the service's card database."""
    try:
        return _render(number, result, error)
    except (AttributeError, TypeError, ValueError):  # a reply not in the documented shape
        LOGGER.warning("card scan: result not in the documented shape")
        return _render(number, None, "認卡服務的回應格式不符")


def _render(number: int, result: dict | None, error: str) -> str:
    head = f'<RESULT kind="cardscan" image="{number}">'
    if result is None:
        return f"{head}\n（認卡無法使用：{_text(error) or '服務沒有回應'}）\n</RESULT>"
    cards = result.get("cards") or []
    kind = result.get("kind", "")
    if not cards:
        return f"{head}\n（這張圖沒有認出卡片）\n</RESULT>"
    how = {
        "neuron": "Neuron 牌組截圖，依區段標題分組",
        "photo": "照片，依卡框找卡，依卡片種類分組（照片分不出副牌組）",
        "photo+boxes": "照片，依卡框與 Gemini 標出的位置找卡，依卡片種類分組（照片分不出副牌組）",
        "boxes": "照片，依 Gemini 標出的位置找卡，依卡片種類分組（照片分不出副牌組）",
        "whole": "整張圖當作一張卡比對",
    }.get(kind, _text(kind, 40))
    lines = [f"共 {len(cards)} 張；{how}。"]
    for section, entries in (result.get("groups") or {}).items():
        total = sum(_int(e.get("count")) for e in entries)
        lines.append(f"\n{SECTION_NAMES.get(section, _text(section, 40))}（{total} 張）")
        for e in entries:
            unsure = _int(e.get("unsure"))
            flag = f"（其中 {unsure} 張不確定）" if unsure else ""
            lines.append(f"- {_int(e.get('count'))}× {_name(e)}{flag}")
    unsure = [c for c in cards if not c.get("sure")]
    if unsure:
        lines.append("\n不確定的卡（第一名與第二名分數接近），依位置列出候選：")
        for c in unsure:
            options = "、".join(
                f"{_name(t)} {_score(t.get('score')):.3f}" for t in c.get("top") or []
            )
            lines.append(f"- 第 {_int(c.get('pos'))} 張：{options}")
    lines.append(
        "\n（依卡圖比對，不是讀卡名：異圖與 OCG／TCG 卡圖不同的卡可能認錯，不確定的請看圖確認；"
        "中文卡名與效果以巧巧屋用卡片密碼查）"
    )
    return f"{head}\n" + "\n".join(lines) + "\n</RESULT>"
