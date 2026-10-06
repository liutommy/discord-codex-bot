"""Model routing for members who never chose a model: Jev (TypeSafe's hosted decision model)
judges each message's task type and difficulty, and config/routing.json maps that to a backend
and effort. The questions below are the setup adopted after evaluation (~/jev-eval a2fcf4d):
the state is {message, recent_context}, types are offered as objects (what / not_for /
examples), difficulty is the 10-level degree scale, and no yes/no gate questions are asked.

A conversation only ever moves up: a message switches backend when it is harder than the band
the conversation is on, or needs an ability its backend lacks (X posts, live web, code)."""

from __future__ import annotations

import json
import logging
import time
from dataclasses import dataclass
from pathlib import Path

import aiohttp

from .backends import (
    _GROK_ID,
    AGY,
    AGY_FAMILIES,
    CODEX,
    GROK,
    ModelChoice,
    Resolved,
    parse_choice,
    resolve,
    split_stored,
)
from .config import REASONING_EFFORTS, Config

LOGGER = logging.getLogger(__name__)
TYPES = ("chat", "quick", "live-x", "live-web", "text", "code", "reason", "create", "advice")
BANDS = "LMHX"
CONTEXT_TURNS = 3  # earlier messages of the member's in recent_context, as in the evaluation
MAX_EARLIER = 4  # earlier threads of one conversation kept as its replay source

TYPE_CRITERIA = {
    "chat": {
        "what": "greetings, small talk, feelings, insults or complaints aimed at the bot, or a"
        " bare command to the bot such as setting or cancelling a reminder or 'remember that I"
        " ...'",
        "not_for": "any question that asks for information, an explanation or an opinion",
        "examples": ["早安各位", "你是不是又壞掉了", "提醒我明天下午三點開會", "記住我不吃香菜"],
    },
    "quick": {
        "what": "a factual question that general knowledge answers in one or two sentences",
        "not_for": "questions about recent events or anything that must be looked up, and"
        " questions that need a long explanation",
        "examples": ["富士山多高", "海綿寶寶的作者是誰", "Python 的 len 是做什麼的"],
    },
    "live-x": {
        "what": "needs posts or accounts on X (Twitter): what someone tweeted, an account's"
        " latest posts, an x.com link",
        "not_for": "news or information from ordinary websites",
        "examples": [
            "馬斯克今天又發了什麼推",
            "幫我看這篇推文在講什麼 https://x.com/user/status/1234567890123456789",
        ],
    },
    "live-web": {
        "what": "needs current information from the web: today's news, latest scores or prices,"
        " newest versions, or the member explicitly asks to search online",
        "not_for": "facts that do not change over time",
        "examples": ["今天台股收盤多少", "LoL 最新版本改了什麼", "上網查一下颱風會不會放假"],
    },
    "text": {
        "what": "work on a text the member pasted or quoted: summarize, translate, rewrite,"
        " proofread, or judge what it means or whether it is a scam",
        "not_for": "writing something new from scratch",
        "examples": ["幫我把這段翻成英文：…", "這封信是詐騙嗎？寄件者：…", "幫我潤一下這段自介：…"],
    },
    "code": {
        "what": "write, modify, debug or explain program code, scripts, config or error messages",
        "not_for": "math or logic problems with no code",
        "examples": [
            "幫我寫一個爬蟲抓 PTT 標題",
            "這個 TypeError 是什麼意思",
            "```js\nconsole.log(a)\n``` 為什麼印出 undefined",
        ],
    },
    "reason": {
        "what": "math, logic puzzles, calculations or problems that need several reasoning steps",
        "not_for": "questions that only need a fact",
        "examples": [
            "一個水池兩根水管 3 小時和 5 小時注滿，同時開要多久",
            "證明根號 2 是無理數",
            "1 到 100 的質數和是多少",
        ],
    },
    "create": {
        "what": "creative writing: stories, poems, copywriting, slogans, role-play, fan fiction",
        "not_for": "rewriting a text the member provided",
        "examples": [
            "寫一首關於颱風天的短詩",
            "幫我的咖啡店想三句廣告標語",
            "你現在扮演一隻傲嬌的貓",
        ],
    },
    "advice": {
        "what": "planning, decisions, comparisons and recommendations: what to buy, choose, do"
        " or plan",
        "not_for": "a single fact or a calculation",
        "examples": [
            "預算三萬要買什麼筆電",
            "要先學 Python 還是 JavaScript",
            "幫我排三天兩夜的花蓮行程",
        ],
    },
}
LEVELS = [
    "trivial, any tiny model answers it",
    "very easy",
    "easy, a small model is fine",
    "moderate, needs a decent general model",
    "moderate, needs some knowledge or judgment",
    "somewhat hard, needs a careful answer",
    "hard, needs a strong model or expert knowledge",
    "very hard, needs multi-step reasoning",
    "extremely hard, long multi-step work",
    "hardest, needs the strongest model available",
]
QUESTIONS = {
    "type": {
        "type": "choice",
        "instructions": "What kind of task is `message`, the newest message a Discord member"
        " sent to an AI assistant bot (Traditional Chinese)? `recent_context` holds the same"
        " member's earlier messages, for reference only.",
        "criteria": TYPE_CRITERIA,
    },
    "difficulty": {
        "type": "score",
        "instructions": "How strong a model is needed to answer `message` well?",
        "criteria": LEVELS,
    },
}


# --------------------------------------------------------------------------- the table


@dataclass(frozen=True, slots=True)
class Table:
    types: dict[str, tuple[str, ...]]  # type -> one stored-model entry per band
    needs: dict[str, str]  # type -> the only backend that can answer it
    grok_spent: str
    live_x_without_grok: str
    claude_failed: dict[str, str]
    codex_five_hour: float
    codex_seven_day: float


def check_entry(entry: str) -> None:
    """A routing cell must name a real target and an effort it takes, exactly: parse_choice
    quietly falls back to DEFAULT_MODEL on anything it does not know, and a table that silently
    meant DEFAULT would look like routing while doing none."""
    value, effort = split_stored(entry)
    if value == CODEX:
        if effort not in REASONING_EFFORTS:
            raise ValueError(f"routing: {entry!r} needs a Codex effort")
        return
    backend, _, model = value.partition(":")
    if backend == GROK:
        if not _GROK_ID.fullmatch(model) or effort not in REASONING_EFFORTS:
            raise ValueError(f"routing: {entry!r} is not a Grok model with an effort")
        return
    if backend == AGY and model in AGY_FAMILIES:
        offered = AGY_FAMILIES[model][1]
        if ("" in offered and effort) or ("" not in offered and effort not in offered):
            raise ValueError(f"routing: {entry!r} asks for an effort {model} does not take")
        return
    raise ValueError(f"routing: {entry!r} is not a codex, grok or agy target")


def load_table(path: Path) -> Table:
    """config/routing.json, every cell checked; any problem is a ValueError (start-up fails)."""
    try:
        data = json.loads(path.read_text("utf-8"))
        types = {kind: tuple(data["types"][kind]) for kind in TYPES}
        degrade = data["degrade"]
        table = Table(
            types=types,
            needs={str(k): str(v) for k, v in data["needs"].items()},
            grok_spent=str(degrade["grok_spent"]),
            live_x_without_grok=str(degrade["live_x_without_grok"]),
            claude_failed={str(k): str(v) for k, v in degrade["claude_failed"].items()},
            codex_five_hour=float(degrade["codex_quota"]["five_hour_percent"]),
            codex_seven_day=float(degrade["codex_quota"]["seven_day_percent"]),
        )
    except (OSError, ValueError, KeyError, TypeError, AttributeError) as error:
        raise ValueError(f"routing: cannot load {path}: {error}") from None
    for kind, cells in table.types.items():
        if len(cells) != len(BANDS):
            raise ValueError(f"routing: {kind} needs {len(BANDS)} cells, one per band")
    for kind, backend in table.needs.items():
        if kind not in TYPES or backend not in (CODEX, GROK, AGY):
            raise ValueError(f"routing: needs {kind!r} -> {backend!r} is not a type and backend")
        if any(backend_of(cell) != backend for cell in table.types[kind]):
            raise ValueError(f"routing: every {kind} cell must be on {backend}")
    for kind in table.claude_failed:
        if kind not in TYPES:
            raise ValueError(f"routing: claude_failed names an unknown type {kind!r}")
    cells = [cell for row in table.types.values() for cell in row]
    for entry in [
        *cells,
        table.grok_spent,
        table.live_x_without_grok,
        *table.claude_failed.values(),
    ]:
        check_entry(entry)
    return table


def backend_of(entry: str) -> str:
    return split_stored(entry)[0].split(":", 1)[0]


def choice_of(entry: str, codex_model: str) -> ModelChoice:
    value = split_stored(entry)[0]
    return parse_choice(f"{CODEX}:{codex_model}" if value == CODEX else value, codex_model)


def resolved(entry: str, codex_model: str) -> Resolved:
    return resolve(choice_of(entry, codex_model), split_stored(entry)[1])


def is_claude(entry: str) -> bool:
    value = split_stored(entry)[0]
    return value.startswith(f"{AGY}:claude-")


def lower_effort(entry: str) -> str:
    """The same target one effort level down; the lowest level stays where it is."""
    value, effort = split_stored(entry)
    levels = list(REASONING_EFFORTS)
    if effort not in levels or levels.index(effort) == 0:
        return entry
    return f"{value}|{levels[levels.index(effort) - 1]}"


def band(score: float) -> str:
    """Jev's score (0-9) is the 10-level degree scale minus one: 1-3 L, 4-6 M, 7-8 H, 9-10 X."""
    degree = round(score + 1)
    return "L" if degree <= 3 else "M" if degree <= 6 else "H" if degree <= 8 else "X"


@dataclass(frozen=True, slots=True)
class Route:
    """Where a routed conversation is: the table cell it was sent to (before any quota stand-in),
    that cell's band and the type that put it there. Kept in the ThreadStore with the thread,
    with the ids of the conversation's earlier threads on other backends (its replay source)."""

    entry: str
    band: str
    kind: str = ""
    earlier: tuple[str, ...] = ()

    def as_dict(self) -> dict:
        return {
            "entry": self.entry,
            "band": self.band,
            "kind": self.kind,
            "earlier": list(self.earlier),
        }

    @classmethod
    def from_dict(cls, data: object) -> Route | None:
        if not isinstance(data, dict) or data.get("band") not in tuple(BANDS):
            return None
        entry, kind, earlier = data.get("entry"), data.get("kind", ""), data.get("earlier", [])
        if not isinstance(entry, str) or not entry or not isinstance(kind, str):
            return None
        threads = tuple(str(t) for t in earlier) if isinstance(earlier, list) else ()
        return cls(entry, str(data["band"]), kind, threads[-MAX_EARLIER:])


def decide(table: Table, kind: str, level: str, current: Route | None) -> tuple[Route, str]:
    """(where this message goes, why). Only up: a new conversation takes its cell; a running one
    moves when this message is in a higher band, or to the same band on the one backend that can
    answer this type; otherwise it stays where it is."""
    cell = table.types[kind][BANDS.index(level)]
    if current is None:
        return Route(cell, level, kind), "new"
    if BANDS.index(level) > BANDS.index(current.band):
        return Route(cell, level, kind, current.earlier), "harder"
    need = table.needs.get(kind)
    if need and backend_of(current.entry) != need:
        moved = table.types[kind][BANDS.index(current.band)]
        return Route(moved, current.band, kind, current.earlier), f"needs-{need}"
    return current, "kept"


# --------------------------------------------------------------------------- the call


@dataclass(frozen=True, slots=True)
class Verdict:
    """What Jev said, or why there is no usable answer (`status` other than "ok")."""

    status: str  # ok | timeout | http-<code> | error-<kind> | bad-format | low-confidence
    kind: str = ""
    score: float = 0.0
    confidence: float = 0.0
    ms: float = 0.0
    input_tokens: int | None = None


def _confidence(answer: dict) -> float:
    if isinstance(answer.get("confidence"), (int, float)):
        return float(answer["confidence"])
    # TypeSafe's definition, for an answer without the field: (p_max - 1/n) / (1 - 1/n).
    probs = sorted((float(p) for p in (answer.get("probabilities") or {}).values()), reverse=True)
    if len(probs) < 2:
        raise ValueError("no probabilities")
    n = len(probs)
    return (probs[0] - 1 / n) / (1 - 1 / n)


def parse_answer(data: object) -> tuple[str, float, float]:
    """(type, score, confidence) from a /v1/systemone response; ValueError when malformed."""
    try:
        answers = data["answers"]  # type: ignore[index]
        kind = answers["type"]["choice"]
        score = float(answers["difficulty"]["score"])
        confidence = _confidence(answers["type"])
    except (KeyError, TypeError, ValueError) as error:
        raise ValueError(f"malformed answer: {type(error).__name__}") from None
    if kind not in TYPES or not 0.0 <= score <= len(LEVELS) - 1 or not 0.0 <= confidence <= 1.0:
        raise ValueError("answer out of range")
    return kind, score, confidence


async def judge(message: str, recent_context: list[str], config: Config) -> Verdict:
    """One Jev call, never retried: on a timeout, an error status (429 and 5xx included), a
    malformed answer or low confidence the caller answers with DEFAULT_MODEL instead. Nothing
    here logs or raises with the request: the key is in its headers."""
    body = {
        "model": config.jev_model,
        "state": {"message": message, "recent_context": recent_context},
        "questions": QUESTIONS,
    }
    headers = {
        "Authorization": f"Bearer {config.typesafe_api_key}",
        "Content-Type": "application/json",
    }
    started = time.perf_counter()

    def elapsed() -> float:
        return round((time.perf_counter() - started) * 1000, 1)

    try:
        timeout = aiohttp.ClientTimeout(total=config.jev_timeout_seconds)
        async with aiohttp.ClientSession(timeout=timeout) as session:
            data = json.dumps(body, ensure_ascii=False).encode()
            async with session.post(config.jev_url, data=data, headers=headers) as response:
                if response.status != 200:
                    return Verdict(f"http-{response.status}", ms=elapsed())
                payload = await response.json(content_type=None)
    except TimeoutError:
        return Verdict("timeout", ms=elapsed())
    except (aiohttp.ClientError, ValueError, OSError) as error:
        return Verdict(f"error-{type(error).__name__}", ms=elapsed())
    try:
        kind, score, confidence = parse_answer(payload)
    except ValueError:
        return Verdict("bad-format", ms=elapsed())
    usage = payload.get("usage") if isinstance(payload, dict) else None
    tokens = usage.get("input_tokens") if isinstance(usage, dict) else None
    status = "ok" if confidence >= config.jev_min_confidence else "low-confidence"
    return Verdict(
        status, kind, score, confidence, elapsed(), tokens if isinstance(tokens, int) else None
    )
