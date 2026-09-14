from __future__ import annotations

import json
from dataclasses import replace
from pathlib import Path

import pytest

from discord_codex_bot import openrouter
from discord_codex_bot.backends import OPENROUTER, parse_choice, resolve
from discord_codex_bot.config import Config
from discord_codex_bot.harvest import transcript
from discord_codex_bot.openrouter import (
    Catalog,
    load_transcript,
    parse_catalog,
    run_openrouter,
    trim_history,
)

CATALOG = {
    "data": [
        {
            "id": "b/vision:free",
            "name": "Vision",
            "pricing": {"prompt": "0", "completion": "0"},
            "architecture": {"input_modalities": ["text", "image"], "output_modalities": ["text"]},
            "supported_parameters": ["reasoning", "tools"],
            "context_length": 4096,
        },
        {
            "id": "a/text:free",
            "name": "Alpha",
            "pricing": {"prompt": "0", "completion": "0"},
            "architecture": {"input_modalities": ["text"], "output_modalities": ["text"]},
            "supported_parameters": ["tools"],
            "context_length": 8192,
        },
        {
            "id": "paid/model",
            "name": "Paid",
            "pricing": {"prompt": "0.001", "completion": "0"},
            "architecture": {"input_modalities": ["text"], "output_modalities": ["text"]},
        },
        {
            "id": "music/free",
            "name": "Music",
            "pricing": {"prompt": "0", "completion": "0"},
            "architecture": {"input_modalities": ["text"], "output_modalities": ["text", "audio"]},
        },
        {
            "id": "x/content-safety:free",
            "name": "Guard",
            "pricing": {"prompt": "0", "completion": "0"},
            "architecture": {"input_modalities": ["text"], "output_modalities": ["text"]},
        },
        {
            "id": "thinkingmachines/inkling:free",
            "name": "Ink",
            "pricing": {"prompt": "0", "completion": "0"},
            "architecture": {"input_modalities": ["text"], "output_modalities": ["text"]},
        },
    ]
}


class Response:
    def __init__(self, payload, status: int = 200) -> None:
        self.payload, self.status = payload, status

    def raise_for_status(self) -> None:
        if self.status >= 400:
            raise openrouter.aiohttp.ClientResponseError(None, (), status=self.status)

    async def json(self, content_type=None):
        return self.payload

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        return False


class Session:
    """Records requests; `responses` is consumed in order."""

    def __init__(self, responses: list[Response]) -> None:
        self.responses = responses
        self.calls: list[tuple[str, str, dict | None]] = []

    def get(self, url: str, **kwargs):
        self.calls.append(("GET", url, None))
        return self.responses.pop(0)

    def post(self, url: str, json=None, **kwargs):
        self.calls.append(("POST", url, json))
        return self.responses.pop(0)

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        return False


@pytest.fixture
def cfg(config: Config, tmp_path: Path) -> Config:
    (tmp_path / "ws").mkdir()
    (tmp_path / "ws" / "AGENTS.md").write_text("你是前輩", "utf-8")
    (tmp_path / "plain").mkdir()
    return replace(
        config,
        openrouter_dir=tmp_path / "or",
        codex_workspace=tmp_path / "ws",
        codex_workspace_plain=tmp_path / "plain",
        output_style_path=tmp_path / "none.md",
    )


def test_parse_catalog_keeps_free_text_chat_models_image_first() -> None:
    models = parse_catalog(CATALOG)
    assert [m.id for m in models] == ["b/vision:free", "a/text:free"]
    assert models[0].image and models[0].reasoning and models[0].context == 4096
    assert not models[1].image and not models[1].reasoning


async def test_catalog_caches_and_keeps_the_last_good_list(monkeypatch, cfg: Config) -> None:
    sessions = [Session([Response(CATALOG)]), Session([Response({}, status=503)])]
    monkeypatch.setattr(
        openrouter, "_session", lambda config, timeout, router=None: sessions.pop(0)
    )
    catalog = Catalog(cfg)
    assert [m.id for m in await catalog.free_models()] == ["b/vision:free", "a/text:free"]
    assert await catalog.free_models() and not sessions[0].calls  # fresh: no second request
    catalog._at = float("-inf")  # expire
    assert [m.id for m in await catalog.free_models()] == ["b/vision:free", "a/text:free"]
    assert catalog.get("a/text:free") is not None and catalog.get("nope") is None


def test_trim_history_keeps_newest_whole_messages_within_budget() -> None:
    messages = [
        {"role": "user", "content": "a" * 50},
        {"role": "assistant", "content": "b" * 50},
        {"role": "user", "content": [{"type": "text", "text": "c" * 30}]},
    ]
    assert trim_history(messages, 90) == messages[1:]
    assert trim_history(messages, 10) == messages[2:]  # the newest always survives
    assert trim_history([], 10) == []


async def test_run_openrouter_builds_persona_images_effort_and_keeps_the_transcript(
    monkeypatch, cfg: Config, tmp_path: Path
) -> None:
    image = tmp_path / "pic.png"
    image.write_bytes(b"png")
    catalog = Catalog(cfg)
    catalog.models = parse_catalog(CATALOG)
    session = Session([Response({"choices": [{"message": {"content": " 你好 "}}]})])
    monkeypatch.setattr(openrouter, "_session", lambda config, timeout, router=None: session)
    result = await run_openrouter(
        "問題", cfg, "b/vision:free", images=[image], memory="- note", effort="max", catalog=catalog
    )
    assert result.text == "你好" and result.thread_id.startswith("or-") and not result.resumed
    _, url, body = session.calls[0]
    assert url.endswith("/chat/completions") and body["model"] == "b/vision:free"
    assert body["messages"][0] == {"role": "system", "content": "你是前輩"}
    user = body["messages"][-1]["content"]
    assert user[0]["type"] == "text" and "<USER_MESSAGE>\n問題\n</USER_MESSAGE>" in user[0]["text"]
    assert "- note" in user[0]["text"]
    assert user[1]["image_url"]["url"].startswith("data:image/png;base64,")
    assert body["reasoning"] == {"effort": "xhigh"}
    stored = load_transcript(cfg, result.thread_id)
    assert [m["role"] for m in stored] == ["user", "assistant"]
    assert "[附圖 1 張]" in stored[0]["content"] and "base64" not in json.dumps(stored)
    assert stored[1]["content"] == "你好"


async def test_run_openrouter_resumes_and_respects_model_capabilities(
    monkeypatch, cfg: Config, tmp_path: Path
) -> None:
    catalog = Catalog(cfg)
    catalog.models = parse_catalog(CATALOG)
    image = tmp_path / "pic.jpg"
    image.write_bytes(b"jpg")
    first = Session([Response({"choices": [{"message": {"content": "一"}}]})])
    monkeypatch.setattr(openrouter, "_session", lambda config, timeout, router=None: first)
    one = await run_openrouter("q1", cfg, "a/text:free", effort="high", catalog=catalog)
    assert "reasoning" not in first.calls[0][2]  # the catalog says this model takes no effort
    content = [{"type": "text", "text": "二"}]
    second = Session([Response({"choices": [{"message": {"content": content}}]})])
    monkeypatch.setattr(openrouter, "_session", lambda config, timeout, router=None: second)
    two = await run_openrouter(
        "q2",
        cfg,
        "a/text:free",
        images=[image],
        resume=one.thread_id,
        personal_style="短",
        catalog=catalog,
    )
    assert two.text == "二" and two.resumed and two.thread_id == one.thread_id
    body = second.calls[0][2]
    assert body["messages"][0]["role"] != "system"  # personal style ⇒ persona-free workspace
    assert [m["role"] for m in body["messages"]] == ["user", "assistant", "user"]
    assert body["messages"][1]["content"] == "一"
    assert isinstance(body["messages"][-1]["content"], str)  # no image parts for a text model
    assert "這個模型看不到圖片" in body["messages"][-1]["content"]
    assert [m["role"] for m in load_transcript(cfg, one.thread_id)] == [
        "user",
        "assistant",
        "user",
        "assistant",
    ]
    # an unknown thread id starts fresh instead of failing
    third = Session([Response({"choices": [{"message": {"content": "三"}}]})])
    monkeypatch.setattr(openrouter, "_session", lambda config, timeout, router=None: third)
    fresh = await run_openrouter("q3", cfg, "a/text:free", resume="or-missing", catalog=catalog)
    assert not fresh.resumed and fresh.thread_id != "or-missing"


async def test_run_openrouter_reports_api_errors_with_the_free_model_hint(
    monkeypatch, cfg: Config
) -> None:
    session = Session([Response({"error": {"message": "Rate limit exceeded"}}, status=429)])
    monkeypatch.setattr(openrouter, "_session", lambda config, timeout, router=None: session)
    with pytest.raises(RuntimeError, match="HTTP 429：Rate limit exceeded（免費模型可能暫時不可用"):
        await run_openrouter("q", cfg, "a/text:free")
    session = Session([Response({"error": {"message": "Upstream overloaded"}}, status=200)])
    monkeypatch.setattr(openrouter, "_session", lambda config, timeout, router=None: session)
    with pytest.raises(RuntimeError, match="OpenRouter 上游錯誤：Upstream overloaded"):
        await run_openrouter("q", cfg, "a/text:free")
    session = Session([Response({"choices": [{"message": {"content": ""}}]})])
    monkeypatch.setattr(openrouter, "_session", lambda config, timeout, router=None: session)
    with pytest.raises(RuntimeError, match="沒有回傳文字"):
        await run_openrouter("q", cfg, "a/text:free")
    assert load_transcript(cfg, "or-nothing") == []


async def test_harvest_reads_the_bot_kept_transcript(monkeypatch, cfg: Config) -> None:
    session = Session([Response({"choices": [{"message": {"content": "答"}}]})])
    monkeypatch.setattr(openrouter, "_session", lambda config, timeout, router=None: session)
    result = await run_openrouter("我叫小明", cfg, "a/text:free", memory="- x")
    assert transcript(cfg, result.thread_id) == "後輩：我叫小明\n\n前輩：答"
    assert transcript(cfg, "or-unknown") == ""


def test_backends_accept_any_openrouter_model_id() -> None:
    choice = parse_choice("openrouter:google/gemma-4-31b-it:free|high", "gpt-5.6-luna")
    assert choice.backend == OPENROUTER and choice.family == "google/gemma-4-31b-it:free"
    assert choice.value == "openrouter:google/gemma-4-31b-it:free"
    target = resolve(choice, "high")
    assert (target.backend, target.model, target.effort) == (
        OPENROUTER,
        "google/gemma-4-31b-it:free",
        "high",
    )
    assert parse_choice("openrouter:", "gpt-5.6-luna").backend == "codex"  # empty id ⇒ default


# ---------- OrcaRouter: the second router on the same core ----------
from discord_codex_bot.openrouter import ROUTERS, Router, is_router_thread, run_router  # noqa: E402

ORCA = ROUTERS["orcarouter"]
ORCA_CATALOG = {
    "data": [
        {"id": "orcarouter/free", "object": "model", "owned_by": "orcarouter"},
        {"id": "deepseek/deepseek-v4-flash-free", "object": "model", "pricing": {"prompt": None}},
        {"id": "tencent/hy3-free", "object": "model"},
        {"id": "z-ai/glm-5.3-flash-free", "object": "model"},
        {
            "id": "deepseek/deepseek-v4-flash",
            "object": "model",
            "pricing": {"prompt": "0.0000001", "completion": "0.0000005"},
        },
        {"id": "anthropic/claude-opus-5", "object": "model", "pricing": {"prompt": "0.00001"}},
    ]
}


def test_orcarouter_free_filter_is_the_dash_free_suffix_plus_its_free_router() -> None:
    models = parse_catalog(ORCA_CATALOG, ORCA)
    assert sorted(m.id for m in models) == [
        "deepseek/deepseek-v4-flash-free",
        "orcarouter/free",
        "tencent/hy3-free",
        "z-ai/glm-5.3-flash-free",
    ]
    # no architecture listed → taken as a plain text model that takes no effort
    assert all(not m.image and not m.reasoning for m in models)
    # the OpenRouter rule would keep none of these (no "0" prices)
    assert parse_catalog(ORCA_CATALOG) == []


def test_router_descriptors_and_thread_prefixes() -> None:
    assert isinstance(ORCA, Router) and ORCA.api == "https://api.orcarouter.ai/v1"
    assert ORCA.thread_prefix == "oc-" and ROUTERS["openrouter"].thread_prefix == "or-"
    assert is_router_thread("oc-abc") and is_router_thread("or-abc")
    assert not is_router_thread("thread-1") and not is_router_thread("")


async def test_run_router_on_orcarouter_uses_its_host_key_prefix_and_label(
    monkeypatch, cfg: Config
) -> None:
    session = Session([Response({"choices": [{"message": {"content": "哈囉"}}]})])
    seen = {}

    def fake_session(config, timeout, router=None):
        seen["router"] = router
        return session

    monkeypatch.setattr(openrouter, "_session", fake_session)
    result = await run_router(ORCA, "q", cfg, "tencent/hy3-free")
    assert result.text == "哈囉" and result.thread_id.startswith("oc-")
    assert seen["router"] is ORCA
    assert session.calls[0][1] == "https://api.orcarouter.ai/v1/chat/completions"
    assert load_transcript(cfg, result.thread_id)[1]["content"] == "哈囉"
    # the free tier's 429 is reported under the router's own name
    session = Session(
        [
            Response(
                {
                    "error": {
                        "code": "free_rate_limited",
                        "message": "Free models are not available",
                    }
                },
                429,
            )
        ]
    )
    monkeypatch.setattr(openrouter, "_session", lambda config, timeout, router=None: session)
    with pytest.raises(RuntimeError, match="OrcaRouter HTTP 429：Free models are not available"):
        await run_router(ORCA, "q", cfg, "tencent/hy3-free")


def test_backends_accept_orcarouter_ids_too() -> None:
    from discord_codex_bot.backends import ORCAROUTER, router_choice

    choice = parse_choice("orcarouter:tencent/hy3-free|low", "gpt-5.6-luna")
    assert choice.backend == ORCAROUTER and choice.family == "tencent/hy3-free"
    assert choice.label == "OrcaRouter · tencent/hy3-free"
    target = resolve(choice, "low")
    assert (target.backend, target.model, target.effort) == (ORCAROUTER, "tencent/hy3-free", "low")
    assert router_choice(ORCAROUTER, "x/y-free", "Y").label == "OrcaRouter · Y"


async def test_run_router_streams_content_deltas_and_ignores_reasoning(
    monkeypatch, cfg: Config
) -> None:
    lines = [
        b'data: {"choices":[{"delta":{"role":"assistant"}}]}\n',
        b'data: {"choices":[{"delta":{"reasoning_content":"thinking"}}]}\n',
        b"\n",
        b'data: {"choices":[{"delta":{"content":"\u4f60"}}]}\n',
        b'data: {"choices":[{"delta":{"content":"\u597d"}}]}\n',
        b"data: [DONE]\n",
        b'data: {"choices":[{"delta":{"content":"IGNORED"}}]}\n',
    ]

    class Body:
        def __aiter__(self):
            return self._gen()

        async def _gen(self):
            for line in lines:
                yield line

    class Streaming(Response):
        content = Body()

    session = Session([Streaming({})])
    monkeypatch.setattr(openrouter, "_session", lambda config, timeout, router=None: session)
    seen = []

    async def on_delta(text):
        seen.append(text)

    result = await run_router(ORCA, "q", cfg, "tencent/hy3-free", on_delta=on_delta)
    assert seen == ["你", "你好"] and result.text == "你好"
    assert session.calls[0][2]["stream"] is True
    # an error chunk inside the stream surfaces like a normal error
    lines[:] = [b'data: {"error":{"message":"quota"}}\n']
    session = Session([Streaming({})])
    monkeypatch.setattr(openrouter, "_session", lambda config, timeout, router=None: session)
    with pytest.raises(RuntimeError, match="上游錯誤：quota"):
        await run_router(ORCA, "q", cfg, "tencent/hy3-free", on_delta=on_delta)
