"""Jev's model pick: routing.json candidates, the `model` question, pick_route, and the bot in
shadow mode (the table routes, the pick is logged) and on mode (the pick routes)."""

import json
from dataclasses import replace
from pathlib import Path

import pytest

from discord_codex_bot import routing
from discord_codex_bot.backends import Resolved
from discord_codex_bot.routing import Route, Verdict
from tests.test_routing import (  # noqa: F401  (backends, client and jev are fixtures)
    TABLE_PATH,
    answer,
    ask,
    backends,
    client,
    jev,
    verdicts,
)

# ruff: noqa: F811  (pytest passes the imported fixtures back in as arguments)


def table() -> routing.Table:
    return routing.load_table(TABLE_PATH)


def picked(kind: str, degree: int, pick: str, probability: float = 0.9) -> Verdict:
    return Verdict("ok", kind, degree - 1.0, 0.9, 300.0, 1600, pick, probability)


def write_table(tmp_path: Path, edit) -> Path:
    data = json.loads(TABLE_PATH.read_text("utf-8"))
    edit(data)
    path = tmp_path / "routing.json"
    path.write_text(json.dumps(data), "utf-8")
    return path


# --------------------------------------------------------------------------- the table


def test_every_shipped_cell_is_a_candidate_with_a_strength() -> None:
    t = table()
    assert t.candidates and "_comment" not in t.candidates
    for row in t.types.values():
        for cell in row:
            assert t.strength(cell) is not None, cell
    assert t.strength("agy:gemini-4-hydrogen|medium") == 4
    assert t.strength("agy:gemini-3.8-flash|medium") == 4  # a renamed family keeps its rank
    assert t.strength("codex|xhigh") is None  # a stand-in only, not a candidate


def test_a_cell_without_a_candidate_stops_start_up(tmp_path: Path) -> None:
    path = write_table(tmp_path, lambda d: d["candidates"].pop("codex-low"))
    with pytest.raises(ValueError, match="cells without a candidate: codex\\|low"):
        routing.load_table(path)


@pytest.mark.parametrize(
    "spec",
    [
        {"target": "agy:gemini-4-hydrogen|low", "strength": "3", "what": "x"},
        {"target": "agy:gemini-4-hydrogen|low", "strength": True, "what": "x"},
        {"target": "agy:gemini-4-hydrogen|low", "strength": 3},
        {"target": "agy:gemini-4-hydrogen|low", "strength": 3, "what": "x", "cost": 1},
        {"target": "agy:no-such-model|low", "strength": 3, "what": "x"},
        {"strength": 3, "what": "x"},
    ],
)
def test_a_bad_candidate_stops_start_up(tmp_path: Path, spec) -> None:
    path = write_table(tmp_path, lambda d: d["candidates"].update(bad=spec))
    with pytest.raises(ValueError):
        routing.load_table(path)


def test_more_candidates_than_jev_takes_stop_start_up(tmp_path: Path) -> None:
    def many(data):
        spec = {"target": "codex|low", "strength": 2, "what": "x"}
        data["candidates"].update({f"c{i}": dict(spec) for i in range(256)})

    with pytest.raises(ValueError, match="at most 255"):
        routing.load_table(write_table(tmp_path, many))


def test_a_table_without_candidates_asks_no_model_question(tmp_path: Path) -> None:
    t = routing.load_table(write_table(tmp_path, lambda d: d.pop("candidates")))
    assert t.candidates == {} and t.strength("codex|low") is None
    assert routing.questions(t, pick=True) is routing.QUESTIONS


def test_the_model_question_offers_every_candidate_without_its_target() -> None:
    t = table()
    assert routing.questions(t, pick=False) is routing.QUESTIONS
    asked = routing.questions(t, pick=True)
    assert set(asked) == {"type", "difficulty", "model"}
    criteria = asked["model"]["criteria"]
    assert set(criteria) == set(t.candidates)
    for option in criteria.values():
        assert option["what"] and "target" not in option and "strength" not in option


# --------------------------------------------------------------------------- the answer

KEYS = frozenset({"hydrogen-low", "codex-high"})


def with_model(choice: object, probabilities: object) -> dict:
    body = answer()
    body["answers"]["model"] = {"choice": choice, "probabilities": probabilities}
    return body


@pytest.mark.parametrize(
    ("body", "expected"),
    [
        (with_model("codex-high", {"codex-high": 0.8, "hydrogen-low": 0.2}), ("codex-high", 0.8)),
        (with_model("opus-high", {"opus-high": 1.0}), ("", 0.0)),  # not offered
        (with_model("codex-high", [0.8]), ("", 0.0)),
        (with_model("codex-high", {"codex-high": 1.5}), ("", 0.0)),
        (answer(), ("", 0.0)),  # no model answer
        (["not", "an", "object"], ("", 0.0)),
    ],
)
def test_parse_pick_takes_only_an_offered_key(body, expected) -> None:
    assert routing.parse_pick(body, KEYS) == expected


async def test_judge_asks_the_model_question_and_reads_the_pick(jev, config) -> None:
    t = table()
    asked = routing.questions(t, pick=True)
    jev["body"] = with_model("codex-high", {"codex-high": 0.7, "hydrogen-low": 0.3})
    verdict = await routing.judge("修 bug", [], replace(config, jev_url=jev["url"]), asked)
    assert (verdict.status, verdict.kind, verdict.pick, verdict.pick_probability) == (
        "ok", "code", "codex-high", 0.7,
    )  # fmt: skip
    _headers, body = jev["requests"][0]
    assert set(body["questions"]) == {"type", "difficulty", "model"}


async def test_a_bad_pick_leaves_type_and_difficulty_usable(jev, config) -> None:
    jev["body"] = with_model("nope", {"nope": 1.0})
    asked = routing.questions(table(), pick=True)
    verdict = await routing.judge("問題", [], replace(config, jev_url=jev["url"]), asked)
    assert verdict.status == "ok" and verdict.kind == "code" and verdict.pick == ""


# --------------------------------------------------------------------------- pick_route


def test_a_new_conversation_takes_the_pick() -> None:
    route, why = routing.pick_route(table(), "text", "L", None, "sonnet-high")
    assert route == Route("agy:claude-sonnet-5-5|high", "L", "text") and why == "picked"


def test_a_type_that_needs_grok_ignores_a_pick_elsewhere() -> None:
    t = table()
    route, why = routing.pick_route(t, "live-x", "M", None, "codex-high")
    assert route == routing.decide(t, "live-x", "M", None)[0] and why == "new"
    route, _ = routing.pick_route(t, "live-x", "M", None, "grok-high")
    assert route.entry == "grok:grok-4.7|high"


def test_a_conversation_never_moves_to_a_weaker_pick() -> None:
    running = Route("codex|high", "H", "code", ("t1",))
    route, why = routing.pick_route(table(), "text", "L", running, "hydrogen-low")
    assert route.entry == "codex|high" and route.earlier == ("t1",) and why == "pick-weaker"
    assert route.band == "H"  # decide's band: never down


def test_a_stronger_pick_moves_the_conversation() -> None:
    running = Route("agy:gemini-4-hydrogen|low", "L", "chat", ("t1",))
    route, why = routing.pick_route(table(), "reason", "H", running, "argon-medium")
    assert route == Route("agy:gemini-4-argon|medium", "H", "reason", ("t1",))
    assert why == "picked"


def test_chat_keeps_a_running_conversation_and_an_unknown_pick_is_the_table() -> None:
    t = table()
    running = Route("codex|high", "H", "code")
    assert routing.pick_route(t, "chat", "L", running, "opus-high") == (running, "kept")
    assert routing.pick_route(t, "text", "M", None, "nope") == routing.decide(t, "text", "M", None)


def test_a_route_off_the_candidates_takes_any_pick() -> None:
    # DEFAULT_MODEL (or an older cell) has no strength: the pick is not held back.
    running = Route("grok:grok-4.5|medium", "M", "")
    route, why = routing.pick_route(table(), "text", "M", running, "hydrogen-low")
    assert route.entry == "agy:gemini-4-hydrogen|low" and why == "picked"


# --------------------------------------------------------------------------- the bot


async def test_shadow_routes_by_the_table_and_logs_the_pick(client, backends, monkeypatch) -> None:
    verdicts(monkeypatch, picked("text", 2, "sonnet-high"))
    plan, result = await ask(client)
    assert result.via == Resolved("agy", "gemini-4-hydrogen-low", "low")  # the table's cell
    assert plan.log["cell"] == "agy:gemini-4-hydrogen|low"
    assert plan.log["pick"] == "sonnet-high" and plan.log["pick_p"] == 0.9
    assert plan.log["pick_cell"] == "agy:claude-sonnet-5-5|high"
    assert plan.log["jev_pick"] == "shadow"


async def test_on_routes_by_the_pick(client, backends, monkeypatch) -> None:
    client.config = replace(client.config, jev_pick="on")
    verdicts(monkeypatch, picked("text", 2, "sonnet-high"))
    plan, result = await ask(client)
    assert result.via == Resolved("agy", "claude-sonnet-5-5-high", "high")
    assert plan.log["why"] == "picked" and plan.route.entry == "agy:claude-sonnet-5-5|high"


async def test_on_keeps_the_table_for_an_unsure_pick(client, backends, monkeypatch) -> None:
    client.config = replace(client.config, jev_pick="on")
    verdicts(monkeypatch, picked("text", 2, "sonnet-high", probability=0.2))
    plan, result = await ask(client)
    assert result.via == Resolved("agy", "gemini-4-hydrogen-low", "low")
    assert plan.log["pick"] == "sonnet-high" and plan.log["pick_cell"] is None


async def test_off_asks_no_model_question(client, backends, monkeypatch) -> None:
    client.config = replace(client.config, jev_pick="off")
    seen = []

    async def judge(message, context, config, questions=None):
        seen.append(questions)
        return Verdict("ok", "text", 1.0, 0.9, 300.0, 1600)

    monkeypatch.setattr(routing, "judge", judge)
    plan, _ = await ask(client)
    assert seen == [routing.QUESTIONS] and plan.log["pick"] is None


def test_an_x_question_leaves_a_stronger_model_for_grok() -> None:
    # Codex on the pre-PR review: the strength floor kept an X question on Argon, which has no
    # X search. The backend a type needs comes before the floor.
    running = Route("agy:gemini-4-argon|medium", "X", "reason", ("t1",))
    route, why = routing.pick_route(table(), "live-x", "M", running, "grok-high")
    assert route.entry == "grok:grok-4.7|high" and why == "picked"
    route, _ = routing.pick_route(table(), "live-x", "M", running, "codex-high")
    assert routing.backend_of(route.entry) == "grok"  # the table's live-x cell
    route, _ = routing.pick_route(table(), "live-x", "M", running, "")
    assert routing.backend_of(route.entry) == "grok"


@pytest.mark.parametrize(
    ("kind", "pick", "used"),
    [
        ("text", "sonnet-high", True),  # claude_failed has text
        ("code", "sonnet-high", False),  # ...but not code: no stand-in when 3p is short
        ("create", "opus-high", True),
        ("advice", "opus-high", False),
        ("reason", "argon-medium", True),  # argon_failed has reason
        ("quick", "argon-medium", False),  # ...but not quick
    ],
)
def test_argon_and_claude_are_picked_only_with_their_stand_in(kind, pick, used) -> None:
    t = table()
    route, why = routing.pick_route(t, kind, "M", None, pick)
    if used:
        assert route.entry == t.candidates[pick].target and why == "picked"
    else:
        assert route == routing.decide(t, kind, "M", None)[0]


def test_an_unsure_pick_never_drops_a_conversation_a_pick_moved() -> None:
    # Codex on the pre-PR review: with no usable pick the table's L cell (strength 3) replaced
    # the Sonnet target (strength 5) an earlier pick had chosen.
    running = Route("agy:claude-sonnet-5-5|high", "L", "text", ("t1",))
    route, why = routing.pick_route(table(), "text", "L", running, "")
    assert route.entry == "agy:claude-sonnet-5-5|high" and why == "pick-weaker"


async def test_on_keeps_a_picked_conversation_when_the_next_pick_is_unsure(
    client, backends, monkeypatch
) -> None:
    client.config = replace(client.config, jev_pick="on")
    verdicts(monkeypatch, picked("text", 2, "sonnet-high"), picked("text", 2, "hydrogen-low", 0.2))
    first, _ = await ask(client)
    assert first.route.entry == "agy:claude-sonnet-5-5|high"
    second, result = await ask(client)
    assert second.route.entry == "agy:claude-sonnet-5-5|high"
    assert result.via == Resolved("agy", "claude-sonnet-5-5-high", "high")
