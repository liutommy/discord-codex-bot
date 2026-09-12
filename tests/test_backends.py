from pathlib import Path

from discord_codex_bot.agy import parse_stream
from discord_codex_bot.backends import (
    AGY,
    AGY_FAMILIES,
    CODEX,
    choices,
    parse_choice,
    resolve,
)
from discord_codex_bot.memory import MemoryLimits, MemoryStore
from discord_codex_bot.threads import ThreadStore

LIMITS = MemoryLimits(200, 25_000, 50_000_000, 200_000_000, 2000, 50_000, 50, 3)


def test_choices_cover_codex_and_every_agy_slug() -> None:
    all_choices = choices("gpt-5.6-luna")
    assert all_choices[0].value == "codex:gpt-5.6-luna" and all_choices[0].backend == CODEX
    assert {c.family for c in all_choices if c.backend == AGY} == set(AGY_FAMILIES)
    assert len(all_choices) <= 25  # Discord's per-option choice cap
    assert parse_choice("agy:claude-opus-4-6", "gpt-5.6-luna").backend == AGY
    assert parse_choice("agy:gemini-3.8-flash-high", "gpt-5.6-luna").family == "gemini-3.8-flash"
    assert parse_choice("agy:no-such-model", "gpt-5.6-luna").value == "codex:gpt-5.6-luna"
    assert parse_choice("", "gpt-5.6-luna").backend == CODEX


def test_effort_maps_onto_legal_agy_slugs() -> None:
    flash = parse_choice("agy:gemini-3.8-flash", "gpt-5.6-luna")
    assert resolve(flash, "medium").model == "gemini-3.8-flash-medium"
    assert resolve(flash, "max").model == "gemini-3.8-flash-high"  # capped at the family's top
    pro = parse_choice("agy:gemini-3.1-pro", "gpt-5.6-luna")
    assert resolve(pro, "medium").model == "gemini-3.1-pro-low"  # closest level at or below
    assert resolve(pro, "xhigh").model == "gemini-3.1-pro-high"
    claude = parse_choice("agy:claude-opus-4-6", "gpt-5.6-luna")
    assert resolve(claude, "high") == resolve(claude, "low")
    assert resolve(claude, "high").effort == "" and resolve(claude, "high").model.endswith("thinking")
    codex = parse_choice("codex:gpt-5.6-luna", "gpt-5.6-luna")
    assert resolve(codex, "xhigh").effort == "xhigh" and resolve(codex, "xhigh").backend == CODEX


def test_member_model_choice_is_stored_per_guild(tmp_path: Path) -> None:
    store = MemoryStore(tmp_path, LIMITS)
    assert store.get_model(1, 2) == ""
    store.set_model(1, 2, "agy:gemini-3.8-flash-high")
    assert store.get_model(1, 2) == "agy:gemini-3.8-flash-high" and store.get_model(2, 2) == ""
    assert store.clear_model(1, 2) and not store.clear_model(1, 2)


def test_threads_never_cross_backends(tmp_path: Path) -> None:
    store = ThreadStore(tmp_path / "t.json", ttl_seconds=600, version="v1")
    key = ThreadStore.key(1, 2, 3)
    store.remember(key, "codex-thread", message_id=5, plain=False, model="codex:gpt-5.6-luna")
    assert store.current(key, plain=False, model="codex:gpt-5.6-luna") == "codex-thread"
    assert store.current(key, plain=False, model="agy:claude-sonnet-4-6") == ""
    assert store.by_message(5, plain=False, model="agy:claude-sonnet-4-6") == ""


def test_parse_stream_reads_result_event() -> None:
    out = "\n".join(
        (
            '{"event":"init","conversation_id":"abc","cwd":"/w"}',
            '{"event":"step_update","step_update":{}}',
            '{"event":"result","result":{"conversation_id":"abc","status":"SUCCESS","response":"OK\\n"}}',
        )
    )
    assert parse_stream(out) == ("abc", "OK\n", "")
    failed = '{"event":"result","result":{"conversation_id":"abc","status":"ERROR","error":"boom"}}'
    assert parse_stream(failed)[2] == "ERROR: boom"
