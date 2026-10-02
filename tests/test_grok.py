"""Grok as a model provider: choices and efforts, the fallback chain, the sidecar client, and
how a member turn moves along grok → codex → agy."""

from __future__ import annotations

import time
from dataclasses import replace
from pathlib import Path

import pytest
from aiohttp import web
from aiohttp.test_utils import TestServer
from test_bot import GUILD, USER, FakeBackend, client  # noqa: F401  (client is a fixture)

from discord_codex_bot import bot as bot_module
from discord_codex_bot import grok
from discord_codex_bot.backends import (
    AGY,
    CODEX,
    GROK,
    BackendUnavailable,
    fallback_chain,
    grok_choice,
    parse_choice,
    resolve,
    router_choice,
)
from discord_codex_bot.codex import CodexResult, CodexUsageLimit
from discord_codex_bot.config import Config

CHAIN = ("grok:grok-4.7|medium", "codex", "agy:gemini-3.8-flash|medium")
CATALOG = [
    grok.GrokModel("grok-4.7", "Grok 4.7", ("xhigh", "high", "medium", "low"), "high"),
    grok.GrokModel("grok-4.5", "Grok 4.5", ("high", "medium", "low"), "high"),
]


@pytest.fixture(autouse=True)
def catalog(monkeypatch):
    monkeypatch.setattr(grok, "_catalog", (time.monotonic(), list(CATALOG)))
    monkeypatch.setattr(grok, "_usage", (0.0, None))


# ----------------------------------------------------------------------------- choices


def test_grok_values_parse_and_unknown_values_fall_to_the_default() -> None:
    assert parse_choice("grok:grok-4.7|medium", "gpt-x").backend == GROK
    assert parse_choice("grok:not-yet-in-catalog", "gpt-x").family == "not-yet-in-catalog"
    default = "grok:grok-4.7|medium"
    assert parse_choice("", "gpt-x", default).value == "grok:grok-4.7"
    assert parse_choice("agy:gone-model", "gpt-x", default).value == "grok:grok-4.7"
    assert parse_choice("grok:bad id!", "gpt-x").backend == CODEX  # malformed: not Grok
    assert parse_choice("", "gpt-x").backend == CODEX  # no DEFAULT_MODEL: Codex, as before


@pytest.mark.parametrize(
    ("model", "asked", "applied"),
    [
        ("grok-4.7", "medium", "medium"),
        ("grok-4.7", "max", "xhigh"),  # closest offered at or below
        ("grok-4.5", "xhigh", "high"),  # 4.5 has no xhigh
        ("grok-4.5", "low", "low"),
        ("grok-9", "medium", "medium"),  # not in the catalog: the sidecar decides
    ],
)
def test_effort_follows_each_models_own_menu(model, asked, applied) -> None:
    target = resolve(grok_choice(model), asked)
    assert (target.backend, target.model, target.effort) == (GROK, model, applied)


# --------------------------------------------------------------------------- the chain


def test_fallback_goes_down_the_chain_never_up() -> None:
    def chain_of(choice):
        return [(t.backend, t.model) for t in fallback_chain(choice, CHAIN, "gpt-x", "high")]

    assert chain_of(grok_choice("grok-4.5")) == [
        (CODEX, "gpt-x"),
        (AGY, "gemini-3.8-flash-medium"),
    ]
    assert chain_of(parse_choice("codex:gpt-x", "gpt-x")) == [(AGY, "gemini-3.8-flash-medium")]
    assert chain_of(parse_choice("agy:gemini-3.8-flash", "gpt-x")) == []
    assert chain_of(router_choice("openrouter", "some/free")) == []  # not in the chain


def test_without_a_chain_only_codex_keeps_its_single_spare() -> None:
    codex = parse_choice("codex:gpt-x", "gpt-x")
    spare = fallback_chain(codex, (), "gpt-x", "high", "agy:gemini-3.8-flash|medium")
    assert [t.model for t in spare] == ["gemini-3.8-flash-medium"]
    assert fallback_chain(grok_choice("grok-4.7"), (), "gpt-x", "high", "agy:x") == []


# ------------------------------------------------------------------------- the client


@pytest.fixture
async def sidecar(tmp_path):
    state = {"status": 200, "calls": []}

    async def models(request):
        return web.json_response({"models": [
            {"id": "grok-4.7", "name": "Grok 4.7", "efforts": ["high", "medium"],
             "default_effort": "high"}]})  # fmt: skip

    async def usage(request):
        return web.json_response({"weekly_percent": 12.0, "reset_at": "x", "live": True})

    async def chat(request):
        state["calls"].append(await request.json())
        if state["status"] != 200:
            return web.json_response({"error": "x"}, status=state["status"])
        return web.json_response({"text": " 前輩的回答 ", "model": "grok-4.7", "effort": "medium"})

    app = web.Application()
    app.add_routes([web.get("/models", models), web.get("/usage", usage), web.post("/chat", chat)])
    async with TestServer(app) as server:
        server.state = state
        yield server


def _cfg(config: Config, server, tmp_path: Path) -> Config:
    workspace = tmp_path / "ws"
    workspace.mkdir(exist_ok=True)
    (workspace / "AGENTS.md").write_text("你是前輩。")
    return replace(
        config,
        xsearch_url=f"http://127.0.0.1:{server.port}",
        grok_dir=tmp_path / "grok",
        codex_workspace=workspace,
    )


async def test_client_reads_models_and_usage(sidecar, config, tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(grok, "_catalog", (0.0, []))
    cfg = _cfg(config, sidecar, tmp_path)
    (model,) = await grok.models(cfg)
    assert (model.id, model.efforts, model.default_effort) == (
        "grok-4.7",
        ("high", "medium"),
        "high",
    )
    assert grok.cached_model("grok-4.7") == model
    assert (await grok.usage(cfg))["weekly_percent"] == 12.0


@pytest.mark.parametrize(
    ("status", "error"),
    [
        (402, grok.GrokQuota),
        (401, grok.GrokLogin),
        (429, grok.GrokBusy),
        (503, grok.GrokRefused),
        (502, grok.GrokUnavailable),
        (400, grok.GrokBadRequest),
    ],
)
async def test_sidecar_statuses_become_errors_the_chain_acts_on(
    sidecar, config, tmp_path, status, error
) -> None:
    sidecar.state["status"] = status
    with pytest.raises(error) as raised:
        await grok.run_grok("hi", _cfg(config, sidecar, tmp_path), "grok-4.7", "medium")
    # A bad request is the Bot's own mistake: it must not look like "try the next backend".
    assert isinstance(raised.value, BackendUnavailable) is (status != 400)


async def test_unreachable_sidecar_is_unavailable(config) -> None:
    cfg = replace(config, xsearch_url="http://127.0.0.1:9")
    with pytest.raises(grok.GrokUnavailable):
        await grok.run_grok("hi", cfg, "grok-4.7")
    with pytest.raises(grok.GrokUnavailable):
        await grok.run_grok("hi", replace(config, xsearch_url=""), "grok-4.7")


async def test_turns_carry_the_persona_and_replay_the_transcript(sidecar, config, tmp_path) -> None:
    cfg = _cfg(config, sidecar, tmp_path)
    first = await grok.run_grok("第一個問題", cfg, "grok-4.7", "medium", raw=True)
    assert first.text == "前輩的回答" and first.thread_id.startswith("gk-") and not first.resumed
    sent = sidecar.state["calls"][0]
    assert sent == {"prompt": "第一個問題", "system": "你是前輩。", "model": "grok-4.7",
                    "effort": "medium"}  # fmt: skip
    second = await grok.run_grok(
        "第二個", cfg, "grok-4.7", "medium", resume=first.thread_id, raw=True
    )
    assert second.resumed and second.thread_id == first.thread_id
    replayed = sidecar.state["calls"][1]["prompt"]
    assert "第一個問題" in replayed and "前輩的回答" in replayed and replayed.endswith("第二個")


PNG = b"\x89PNG\r\n\x1a\n" + b"\0" * 16
JPEG = b"\xff\xd8\xff\xe0" + b"\0" * 16


async def test_png_and_jpeg_images_go_along_to_the_sidecar(sidecar, config, tmp_path) -> None:
    import base64

    cfg = _cfg(config, sidecar, tmp_path)
    (tmp_path / "a.png").write_bytes(PNG)
    (tmp_path / "b.jpg").write_bytes(JPEG)
    result = await grok.run_grok(
        "這兩張是什麼", cfg, "grok-4.7", images=[tmp_path / "a.png", tmp_path / "b.jpg"], raw=True
    )
    sent = sidecar.state["calls"][0]
    assert sent["images"] == [base64.b64encode(PNG).decode(), base64.b64encode(JPEG).decode()]
    stored = grok.load_transcript(cfg, result.thread_id)
    assert stored[0]["content"] == "這兩張是什麼\n\n[附圖 2 張]"  # the bytes are not kept

    await grok.run_grok("沒有圖", cfg, "grok-4.7", raw=True)
    assert "images" not in sidecar.state["calls"][1]


@pytest.mark.parametrize(
    "files",
    [
        {"a.gif": b"GIF89a" + b"\0" * 16},
        {"a.webp": b"RIFF\0\0\0\0WEBPVP8 "},
        {"a.png": PNG, "b.webp": b"RIFF\0\0\0\0WEBPVP8 "},  # one unreadable image: none sent
        {f"{i}.png": PNG for i in range(grok.MAX_IMAGES + 1)},
        {"big.png": PNG + b"\0" * 64},
    ],
)
async def test_images_grok_cannot_take_send_the_turn_down_the_chain(
    sidecar, config, tmp_path, monkeypatch, files
) -> None:
    monkeypatch.setattr(grok, "MAX_IMAGE_BYTES", 60)
    paths = []
    for name, data in files.items():
        (tmp_path / name).write_bytes(data)
        paths.append(tmp_path / name)
    with pytest.raises(grok.GrokImages) as raised:
        await grok.run_grok("hi", _cfg(config, sidecar, tmp_path), "grok-4.7", images=paths)
    assert isinstance(raised.value, BackendUnavailable)  # the chain answers it elsewhere
    assert sidecar.state["calls"] == []


# ------------------------------------------------------------------- a member's turn


@pytest.fixture
def grok_client(client, monkeypatch):  # noqa: F811
    client.config = replace(
        client.config,
        xsearch_url="http://xsearch:8090",
        default_model="grok:grok-4.7|medium",
        model_chain=CHAIN,
    )
    calls = {"grok": [], "usage": 12.0}
    replies = {"grok": "Grok 的答案"}

    async def fake_grok(text, config, model, effort="", **kw):
        calls["grok"].append((model, effort, kw))
        outcome = replies["grok"]
        if isinstance(outcome, Exception):
            raise outcome
        return CodexResult(outcome, thread_id="gk-1")

    async def fake_usage(config):
        return {"weekly_percent": calls["usage"], "live": True}

    monkeypatch.setattr(grok, "run_grok", fake_grok)
    monkeypatch.setattr(grok, "usage", fake_usage)
    codex, agy = FakeBackend("Codex 的答案"), FakeBackend("agy 的答案")
    monkeypatch.setattr(bot_module, "run_codex", codex)
    monkeypatch.setattr(bot_module, "run_agy", agy)
    return client, calls, replies, codex, agy


async def test_a_member_without_a_choice_gets_grok_47_medium(grok_client) -> None:
    bot, calls, _replies, codex, _agy = grok_client
    result = await bot._answer("q", [], GUILD, USER)
    assert result.text == "Grok 的答案" and calls["grok"][0][:2] == ("grok-4.7", "medium")
    assert codex.calls == []
    assert bot._member_effort(GUILD, USER) == "medium"  # what /inmu-king uses when unset


async def test_grok_quota_falls_to_codex_then_agy(grok_client, monkeypatch) -> None:
    bot, _calls, replies, _codex, agy = grok_client
    replies["grok"] = grok.GrokQuota("402")
    result = await bot._answer("q", [], GUILD, USER)
    assert result.text == "Codex 的答案"
    assert isinstance(bot._last_fallback[1], grok.GrokQuota)

    async def spent(*_a, **_kw):
        raise CodexUsageLimit("spent")

    monkeypatch.setattr(bot_module, "run_codex", spent)
    result = await bot._answer("q", [], GUILD, USER)
    assert result.text == "agy 的答案" and agy.calls[-1][1] == ("gemini-3.8-flash-medium",)


async def test_turns_past_the_reserve_skip_grok_but_turns_with_images_do_not(
    grok_client, monkeypatch
) -> None:
    bot, calls, replies, codex, _agy = grok_client
    picture = type("A", (), {"content_type": "image/png", "filename": "a.png", "size": 10})()
    monkeypatch.setattr(bot_module, "validate_attachment", lambda *a: ("skip", ""))
    await bot._answer("q", [picture], GUILD, USER)
    assert len(calls["grok"]) == 1 and codex.calls == []  # Grok sees PNG and JPEG now
    replies["grok"] = grok.GrokImages("a GIF")  # one it cannot take: the chain answers it
    result = await bot._answer("q", [picture], GUILD, USER)
    assert result.text == "Codex 的答案" and isinstance(bot._last_fallback[1], grok.GrokImages)
    replies["grok"] = "Grok 的答案"
    calls["usage"] = 85.0  # past GROK_CHAT_MAX_WEEKLY_PERCENT (80): the rest is for X lookups
    calls["grok"].clear(), codex.calls.clear()
    await bot._answer("q", [], GUILD, USER)
    assert calls["grok"] == [] and codex.calls


async def test_refused_sessions_fall_back_and_trip_the_breaker(grok_client, monkeypatch) -> None:
    bot, calls, replies, codex, _agy = grok_client
    alerts = []

    async def record(backend, detail):
        alerts.append(backend)

    monkeypatch.setattr(bot.alerts, "record_failure", record)
    replies["grok"] = grok.GrokRefused("503")
    for _ in range(3):
        result = await bot._answer("q", [], GUILD, USER)
        assert result.text == "Codex 的答案"  # the refused answer is never used
    assert alerts == [GROK, GROK]  # the first refusal, then the breaker — not one per message
    tried = len(calls["grok"])
    await bot._answer("q", [], GUILD, USER)
    assert len(calls["grok"]) == tried  # breaker open: Grok is not even asked


async def test_a_bad_request_is_reported_not_hidden_by_a_fallback(grok_client) -> None:
    bot, _calls, replies, codex, _agy = grok_client
    replies["grok"] = grok.GrokBadRequest("unknown model")
    result = await bot._answer("q", [], GUILD, USER)
    assert result.text == bot_module.FAILURE_MESSAGE.format(prefix=bot.config.command_prefix)
    assert codex.calls == []


async def test_a_codex_member_never_falls_up_into_grok(grok_client, monkeypatch) -> None:
    bot, calls, _replies, _codex, agy = grok_client
    bot.memory.set_model(GUILD, USER, "codex:" + bot.config.codex_model)

    async def spent(*_a, **_kw):
        raise CodexUsageLimit("spent")

    monkeypatch.setattr(bot_module, "run_codex", spent)
    result = await bot._answer("q", [], GUILD, USER)
    assert result.text == "agy 的答案" and calls["grok"] == []


async def test_status_shows_grok_weekly_usage_and_the_reserve(grok_client) -> None:
    bot, calls, _replies, _codex, _agy = grok_client
    calls["usage"] = 85.0

    async def no_probe(config):
        return None

    async def login(config):
        return "ChatGPT 訂閱登入有效"

    bot_module.probe_rate_limits, saved = no_probe, bot_module.probe_rate_limits
    bot_module.codex_login_status, saved_login = login, bot_module.codex_login_status
    try:
        text = await bot._status_text(GUILD, 1, USER)
    finally:
        bot_module.probe_rate_limits, bot_module.codex_login_status = saved, saved_login
    assert "Grok：可選 · 額度 7d 85%" in text and "留給 X 查詢" in text
    assert "模型：Grok · Grok 4.7 · 強度 Medium（預設）" in text


async def test_requests_go_out_as_utf8_not_escaped_ascii(sidecar, config, tmp_path) -> None:
    sizes = []
    original = sidecar.app.router  # noqa: F841 (kept for clarity)

    cfg = _cfg(config, sidecar, tmp_path)
    prompt = "前" * 30_000
    await grok.run_grok(prompt, cfg, "grok-4.7", raw=True)
    sent = sidecar.state["calls"][0]["prompt"]
    assert sent == prompt
    sizes.append(len(prompt.encode("utf-8")))
    assert sizes[0] < 100_000  # 90 KB as UTF-8; `json=` would have made it 180 KB


def test_transcript_ids_are_only_the_bots_own(config, tmp_path) -> None:
    cfg = replace(config, grok_dir=tmp_path)
    assert grok.load_transcript(cfg, "gk-../../etc/passwd") == []
    with pytest.raises(ValueError):
        grok.transcript_path(cfg, "gk-../x")
    assert grok.transcript_path(cfg, "gk-0123456789abcdef").name == "gk-0123456789abcdef.json"


async def test_a_retired_grok_model_falls_to_the_default_grok_model(grok_client) -> None:
    bot, calls, _replies, _codex, _agy = grok_client
    bot.memory.set_model(GUILD, USER, "grok:grok-3-retired|high")
    await bot._answer("q", [], GUILD, USER)
    assert calls["grok"][0][0] == "grok-4.7"


async def test_a_chain_listing_grok_after_codex_still_never_sends_codex_users_to_grok(
    grok_client, monkeypatch
) -> None:
    bot, calls, _replies, _codex, agy = grok_client
    bot.config = replace(bot.config, model_chain=("codex", CHAIN[0], CHAIN[2]))
    bot.memory.set_model(GUILD, USER, "codex:" + bot.config.codex_model)

    async def spent(*_a, **_kw):
        raise CodexUsageLimit("spent")

    monkeypatch.setattr(bot_module, "run_codex", spent)
    result = await bot._answer("q", [], GUILD, USER)
    assert result.text == "agy 的答案" and calls["grok"] == []
