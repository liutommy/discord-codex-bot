"""Model routing for members who never chose a model: Jev (TypeSafe's hosted decision model)
judges each message's task type and difficulty, and config/routing.json maps that to a backend
and effort. The questions below are the setup adopted after evaluation (~/jev-eval a2fcf4d):
the state is {message, recent_context}, types are offered as objects (what / not_for /
examples), difficulty is the 10-level degree scale, and no yes/no gate questions are asked.

A conversation never drops a band: it moves to the cell of its latest message of substance at the
higher of that message's band and its own (routing.decide).

With JEV_PICK (shadow by default) the same call also asks Jev which of routing.json's
`candidates` should answer. Shadow logs that pick beside the table's cell; on routes by it
(routing.pick_route): never to a target weaker than the conversation's current one, only on
the backend a type needs (live-x: Grok), and Argon or Claude only for a type with their stand-in."""

from __future__ import annotations

import json
import logging
import time
from dataclasses import dataclass, field
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
    current,
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
MODEL_INSTRUCTIONS = (
    "Which of these AI models should answer `message`? Each option describes one model the bot"
    " can use. Pick the cheapest model that will still answer it well: stronger models are"
    " slower and spend scarcer quota."
)
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
class Candidate:
    """A model Jev may pick: its routing entry, its strength rank (higher answers harder
    questions; a conversation never moves to a lower one) and the option Jev reads."""

    target: str
    strength: int
    option: dict[str, str]


@dataclass(frozen=True, slots=True)
class Table:
    types: dict[str, tuple[str, ...]]  # type -> one stored-model entry per band
    needs: dict[str, str]  # type -> the only backend that can answer it
    image: tuple[str, ...]  # where a message with an image goes, in order
    grok_spent: str
    live_x_without_grok: str
    claude_failed: dict[str, str]
    argon_failed: dict[str, str]  # type -> the cell Argon replaced, for when its pool is spent
    codex_five_hour: float
    codex_seven_day: float
    candidates: dict[str, Candidate] = field(default_factory=dict)  # option key -> candidate

    def strength(self, entry: str) -> int | None:
        """The strength of the candidate routing to `entry`; None when no candidate does."""
        for candidate in self.candidates.values():
            if candidate.target == current(entry):
                return candidate.strength
        return None


def questions(table: Table, pick: bool) -> dict:
    """The Jev questions for one call: type and difficulty, plus `model` over the table's
    candidates when `pick` (JEV_PICK shadow or on) and the table has any."""
    if not pick or not table.candidates:
        return QUESTIONS
    model = {
        "type": "choice",
        "instructions": MODEL_INSTRUCTIONS,
        "criteria": {key: dict(c.option) for key, c in table.candidates.items()},
    }
    return {**QUESTIONS, "model": model}


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
            image=tuple(str(entry) for entry in data["image"]),
            grok_spent=str(degrade["grok_spent"]),
            live_x_without_grok=str(degrade["live_x_without_grok"]),
            claude_failed={str(k): str(v) for k, v in degrade["claude_failed"].items()},
            argon_failed={str(k): str(v) for k, v in degrade["argon_failed"].items()},
            codex_five_hour=float(degrade["codex_quota"]["five_hour_percent"]),
            codex_seven_day=float(degrade["codex_quota"]["seven_day_percent"]),
            candidates=_candidates(data.get("candidates", {})),
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
    if not table.image:
        raise ValueError("routing: image needs at least one target")
    if backend_of(table.image[0]) != CODEX:
        # An image goes Codex first (then Grok, agy). Codex cells have no
        # type stand-ins, so keeping a conversation's Codex cell for its image stays simple.
        raise ValueError("routing: image must start with codex (the order is codex → grok → agy)")
    for name, stand_ins in (
        ("claude_failed", table.claude_failed),
        ("argon_failed", table.argon_failed),
    ):
        for kind in stand_ins:
            if kind not in TYPES:
                raise ValueError(f"routing: {name} names an unknown type {kind!r}")
    for kind, row in table.types.items():
        if any(is_argon(cell) for cell in row) and kind not in table.argon_failed:
            raise ValueError(
                f"routing: argon_failed has no stand-in for {kind!r}, which uses Argon"
            )
    cells = [cell for row in table.types.values() for cell in row]
    if table.candidates:
        targets = {c.target for c in table.candidates.values()}
        missing = sorted({cell for cell in cells if current(cell) not in targets})
        if missing:
            # A conversation's current target needs a strength for "never weaker" to hold.
            raise ValueError(f"routing: cells without a candidate: {', '.join(missing)}")
    for entry in [
        *cells,
        *(c.target for c in table.candidates.values()),
        table.grok_spent,
        table.live_x_without_grok,
        *table.claude_failed.values(),
        *table.argon_failed.values(),
        *table.image,
    ]:
        check_entry(entry)
    return table


MAX_CANDIDATES = 255  # Jev's limit on a choice question's options (a 256th answers 422)


def _candidates(data: object) -> dict[str, Candidate]:
    """routing.json's `candidates`: key -> {target, strength, what, not_for, ...}; every other
    string field goes to Jev as part of the option."""
    if not isinstance(data, dict) or len(data) > MAX_CANDIDATES:
        raise ValueError(f"candidates must be an object of at most {MAX_CANDIDATES} entries")
    out = {}
    for key, spec in data.items():
        if key.startswith("_"):
            continue  # a comment
        target, strength = spec["target"], spec["strength"]
        integer = isinstance(strength, int) and not isinstance(strength, bool)
        if not isinstance(target, str) or not integer:
            raise ValueError(f"candidate {key!r} needs a target string and an integer strength")
        option = {k: v for k, v in spec.items() if k not in ("target", "strength")}
        if not option.get("what") or not all(isinstance(v, str) for v in option.values()):
            raise ValueError(f"candidate {key!r} needs a `what` and only string fields")
        out[str(key)] = Candidate(target, strength, option)
    return out


def retired(entry: str) -> bool:
    """An agy target whose family agy no longer offers. Only that: a stored route may also be
    DEFAULT_MODEL, written like a member's stored model rather than a table cell."""
    value = split_stored(entry)[0]
    backend, _, family = value.partition(":")
    return backend == AGY and family not in AGY_FAMILIES and not _known_slug(family)


def _known_slug(slug: str) -> bool:
    return any(slug in slugs.values() for _, slugs in AGY_FAMILIES.values())


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


def is_argon(entry: str) -> bool:
    return split_stored(entry)[0] == f"{AGY}:gemini-4-argon"


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
        # A route saved on a replaced family compares equal to the table cell that replaced it.
        return cls(current(entry), str(data["band"]), kind, threads[-MAX_EARLIER:])


def decide(table: Table, kind: str, level: str, current: Route | None) -> tuple[Route, str]:
    """(where this message goes, why). A new conversation takes its cell. A running one never
    drops a band and follows the latest message of substance: chat (any backend can chat) leaves
    it as it is, type and all, so a thank-you inside an X conversation keeps it an X one; any
    other type goes to its own cell at the higher of the two bands. That also covers the types
    only one backend can answer (`needs`, checked at load), and keeps a hard question from staying
    on the weak cell an easier type left the conversation on."""
    if current is None:
        return Route(table.types[kind][BANDS.index(level)], level, kind), "new"
    if kind == "chat":
        return current, "kept"
    top = max(level, current.band, key=BANDS.index)
    cell = table.types[kind][BANDS.index(top)]
    moved = Route(cell, top, kind, current.earlier)
    if cell == current.entry:
        return moved, "kept"
    return moved, "harder" if BANDS.index(level) > BANDS.index(current.band) else "type"


def has_stand_in(table: Table, kind: str, target: str) -> bool:
    """Whether a target of this type has its quota stand-in: an Argon target needs
    argon_failed[kind], a Claude target claude_failed[kind]; any other target needs none."""
    if is_argon(target):
        return kind in table.argon_failed
    if is_claude(target):
        return kind in table.claude_failed
    return True


def pick_route(
    table: Table, kind: str, level: str, current_route: Route | None, pick: str
) -> tuple[Route, str]:
    """(where this message goes, why) with JEV_PICK=on; `pick` is "" when Jev's pick is missing
    or unsure. A pick is used unless the type needs another backend (live-x: Grok) or the pick
    is Argon or Claude without that type's stand-in; then the table's cell stands in for it.
    Either way the conversation never moves to a weaker target than its current one, unless
    its current one is not on the backend the type needs. chat keeps a running conversation
    where it is, as in decide; the band recorded is decide's."""
    tabled, why = decide(table, kind, level, current_route)
    candidate = table.candidates.get(pick)
    needed = table.needs.get(kind)
    if candidate is not None and (
        (needed and backend_of(candidate.target) != needed)
        or not has_stand_in(table, kind, candidate.target)
    ):
        candidate = None
    if current_route is None:
        if candidate is None:
            return tabled, why
        return Route(candidate.target, level, kind), "picked"
    if kind == "chat":
        return current_route, "kept"
    chosen, reason = (candidate.target, "picked") if candidate else (tabled.entry, why)
    floor, strength = table.strength(current_route.entry), table.strength(chosen)
    on_needed = not needed or backend_of(current_route.entry) == needed
    if on_needed and floor is not None and strength is not None and strength < floor:
        stay = Route(current_route.entry, tabled.band, kind, current_route.earlier)
        return stay, "pick-weaker"
    moved = Route(chosen, tabled.band, kind, current_route.earlier)
    return moved, "kept" if chosen == current_route.entry else reason


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
    pick: str = ""  # Jev's model pick (a candidate key), "" when not asked or unusable
    pick_probability: float = 0.0


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
    except (KeyError, TypeError, ValueError, AttributeError) as error:
        # Any shape Jev's schema did not promise is a bad verdict, never an exception that
        # leaves the member's request unanswered.
        raise ValueError(f"malformed answer: {type(error).__name__}") from None
    if kind not in TYPES or not 0.0 <= score <= len(LEVELS) - 1 or not 0.0 <= confidence <= 1.0:
        raise ValueError("answer out of range")
    return kind, score, confidence


def parse_pick(data: object, keys: frozenset[str]) -> tuple[str, float]:
    """(candidate key, its probability) from the `model` answer; ("", 0.0) when it is missing
    or not one of `keys`. A bad pick never spoils the type and difficulty answers."""
    try:
        answer = data["answers"]["model"]  # type: ignore[index]
        pick = answer["choice"]
        probability = float((answer.get("probabilities") or {}).get(pick, 0.0))
    except (KeyError, TypeError, ValueError, AttributeError):
        return "", 0.0
    if pick not in keys or not 0.0 <= probability <= 1.0:
        return "", 0.0
    return pick, probability


async def judge(
    message: str,
    recent_context: list[str],
    config: Config,
    asked: dict | None = None,
) -> Verdict:
    """One Jev call, never retried: on a timeout, an error status (429 and 5xx included), a
    malformed answer or low confidence the caller answers with DEFAULT_MODEL instead. Nothing
    here logs or raises with the request: the key is in its headers."""
    body = {
        "model": config.jev_model,
        "state": {"message": message, "recent_context": recent_context},
        "questions": asked or QUESTIONS,
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
    keys = frozenset((asked or {}).get("model", {}).get("criteria", {}))
    pick, probability = parse_pick(payload, keys) if keys else ("", 0.0)
    return Verdict(
        status,
        kind,
        score,
        confidence,
        elapsed(),
        tokens if isinstance(tokens, int) else None,
        pick,
        probability,
    )
