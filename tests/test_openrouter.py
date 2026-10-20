from __future__ import annotations

import json
from dataclasses import replace
from functools import partial
from pathlib import Path

import aiohttp
import pytest

from discord_codex_bot import openrouter
from discord_codex_bot.backends import OPENROUTER, parse_choice, resolve
from discord_codex_bot.codex import BackendError
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
    assert body["messages"][0]["role"] == "system"  # a personal style keeps the persona
    assert [m["role"] for m in body["messages"]] == ["system", "user", "assistant", "user"]
    assert body["messages"][2]["content"] == "一"  # system, q1, answer, q2
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
    with pytest.raises(RuntimeError, match="有回應，但沒有答案文字"):
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


def test_router_descriptors_and_thread_prefixes(config: Config) -> None:
    assert isinstance(ORCA, Router) and ORCA.api(config) == "https://api.orcarouter.ai/v1"
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


# ---------- Local server: a third router on the same core ----------
from discord_codex_bot.backends import LOCAL  # noqa: E402
from discord_codex_bot.openrouter import strip_think  # noqa: E402

LOCAL_ROUTER = ROUTERS[LOCAL]
# What llama-server lists (no price, no architecture); mlx_lm.server lists the same shape.
LOCAL_CATALOG = {
    "object": "list",
    "data": [
        {"id": "qwen3-8b-q4", "object": "model", "owned_by": "llamacpp", "meta": {"n_ctx": 8192}},
        {"id": "mlx-community/Qwen3-4B-4bit", "object": "model", "created": 1760000000},
    ],
}


@pytest.fixture
def local_cfg(cfg: Config) -> Config:
    return replace(cfg, local_llm_url="http://host.orb.internal:8080/v1/")


async def test_local_catalog_takes_every_entry_from_the_configured_url(
    monkeypatch, local_cfg: Config
) -> None:
    session = Session([Response(LOCAL_CATALOG)])
    monkeypatch.setattr(openrouter, "_session", lambda config, timeout, router=None: session)
    models = await Catalog(local_cfg, LOCAL_ROUTER).free_models()
    assert [m.id for m in models] == ["mlx-community/Qwen3-4B-4bit", "qwen3-8b-q4"]
    assert all(not m.image and not m.reasoning for m in models)  # plain text models
    # the configured URL, trailing slash dropped
    assert session.calls[0][1] == "http://host.orb.internal:8080/v1/models"
    assert LOCAL_ROUTER.thread_prefix == "lo-" and is_router_thread("lo-abc")
    assert not LOCAL_ROUTER.enabled(replace(local_cfg, local_llm_url=""))
    assert LOCAL_ROUTER.enabled(local_cfg)


async def test_local_path_ids_are_shown_by_name_and_sent_in_full(
    monkeypatch, local_cfg: Config
) -> None:
    # mlx_lm.server lists the model it was started with by its absolute path on the host.
    catalog_payload = {
        "data": [
            {"id": "/Users/someone/models/mimo-q4"},
            {"id": "mlx-community/Qwen3-4B-4bit"},
            {"id": "/srv/a/same.gguf"},
            {"id": "/srv/b/same.gguf"},
            {"id": "/opt/m/qwen3"},
            {"id": "qwen3"},
        ]
    }
    session = Session(
        [Response(catalog_payload), Response({"choices": [{"message": {"content": "好"}}]})]
    )
    monkeypatch.setattr(openrouter, "_session", lambda config, timeout, router=None: session)
    catalog = Catalog(local_cfg, LOCAL_ROUTER)
    models = await catalog.free_models()
    # A name another model shows gets a tag fixed by the full path (sha256, 4 hex digits).
    assert sorted((m.id, m.name) for m in models) == [
        ("mimo-q4", "mimo-q4"),
        ("mlx-community/Qwen3-4B-4bit", "mlx-community/Qwen3-4B-4bit"),
        ("qwen3", "qwen3"),  # a server-side id keeps its name; the path beside it is tagged
        ("qwen3~e736", "qwen3~e736"),
        ("same.gguf~c5aa", "same.gguf~c5aa"),
        ("same.gguf~fa0c", "same.gguf~fa0c"),
    ]
    assert not any(m.id.startswith("/") or m.name.startswith("/") for m in models)
    await run_router(LOCAL_ROUTER, "q", local_cfg, "mimo-q4", catalog=catalog)
    assert session.calls[-1][2]["model"] == "/Users/someone/models/mimo-q4"
    session.responses.append(Response({"choices": [{"message": {"content": "好"}}]}))
    await run_router(LOCAL_ROUTER, "q", local_cfg, "same.gguf~c5aa", catalog=catalog)
    assert session.calls[-1][2]["model"] == "/srv/b/same.gguf"
    # Its twin gone, /srv/b is shown as plain same.gguf; the stored alias still finds it.
    catalog.models = parse_catalog({"data": [{"id": "/srv/b/same.gguf"}]}, LOCAL_ROUTER)
    assert [m.id for m in catalog.models] == ["same.gguf"]
    assert catalog.get("same.gguf~c5aa").api_id == "/srv/b/same.gguf"
    assert catalog.get("same.gguf~fa0c") is None, "an alias never lands on another model"
    # A path-shaped name is cut like the id, even when it differs from it.
    named = parse_catalog(
        {"data": [{"id": "/m/a/x.gguf", "name": "/m/a/x.gguf.q4"}, {"id": "r", "name": "/m/r"}]},
        LOCAL_ROUTER,
    )
    assert sorted(m.name for m in named) == ["r", "x.gguf.q4"]


async def test_a_local_error_naming_a_host_path_shows_only_its_last_part(
    monkeypatch, local_cfg: Config
) -> None:
    session = Session(
        [Response({"error": {"message": "failed to load /Users/op/models/foo: no such file"}})]
    )
    monkeypatch.setattr(openrouter, "_session", lambda config, timeout, router=None: session)
    with pytest.raises(BackendError) as raised:
        await run_router(LOCAL_ROUTER, "q", local_cfg, "foo")
    assert "上游錯誤：failed to load foo: no such file" in str(raised.value)
    assert "/Users" not in str(raised.value)
    assert openrouter.short_id("C:\\models\\qwen.gguf") == "qwen.gguf"


async def test_authorization_header_only_with_a_key(local_cfg: Config) -> None:
    async with openrouter._session(local_cfg, 5, LOCAL_ROUTER) as session:
        assert "Authorization" not in session.headers
    keyed = replace(local_cfg, local_llm_api_key="sk-local")
    async with openrouter._session(keyed, 5, LOCAL_ROUTER) as session:
        assert session.headers["Authorization"] == "Bearer sk-local"
    # the routers keep sending their key
    async with openrouter._session(local_cfg, 5, ORCA) as session:
        assert session.headers["Authorization"] == "Bearer test-orca"


async def test_run_router_on_local_uses_the_url_prefix_and_its_own_hint(
    monkeypatch, local_cfg: Config
) -> None:
    session = Session([Response({"choices": [{"message": {"content": "哈囉"}}]})])
    monkeypatch.setattr(openrouter, "_session", lambda config, timeout, router=None: session)
    result = await run_router(LOCAL_ROUTER, "q", local_cfg, "qwen3-8b-q4")
    assert result.text == "哈囉" and result.thread_id.startswith("lo-")
    assert session.calls[0][1] == "http://host.orb.internal:8080/v1/chat/completions"
    assert session.calls[0][2]["max_tokens"] == local_cfg.local_llm_max_tokens == 8192
    # Sampling from LOCAL_LLM_PARAMS rides along; it cannot replace the Bot's own fields.
    tuned = replace(local_cfg, local_llm_params='{"temperature": 1.0, "presence_penalty": 1.5}')
    session = Session([Response({"choices": [{"message": {"content": "好"}}]})])
    monkeypatch.setattr(openrouter, "_session", lambda config, timeout, router=None: session)
    await run_router(LOCAL_ROUTER, "q", tuned, "qwen3-8b-q4")
    sent = session.calls[0][2]
    assert (sent["temperature"], sent["presence_penalty"], sent["model"]) == (
        1.0,
        1.5,
        "qwen3-8b-q4",
    )
    session = Session([Response({"error": {"message": "model not loaded"}}, 500)])
    monkeypatch.setattr(openrouter, "_session", lambda config, timeout, router=None: session)
    hint = "（地端模型可能沒有在跑，換一個試試）"
    with pytest.raises(RuntimeError, match=f"^地端模型 HTTP 500：model not loaded{hint}$"):
        await run_router(LOCAL_ROUTER, "q", local_cfg, "qwen3-8b-q4")


class _Refused(aiohttp.ClientConnectorError):
    os_error = ConnectionRefusedError()

    def __init__(self) -> None:  # no real connection key: nothing was connected
        pass


class _NoSuchHost(_Refused):
    os_error = OSError("nodename nor servname provided")

    def __str__(self) -> str:
        return "refused"


async def test_local_retries_a_refused_connection_but_the_routers_do_not(
    monkeypatch, local_cfg: Config
) -> None:
    monkeypatch.setattr(openrouter, "CONNECT_RETRY_SECONDS", 0)
    calls = []

    async def post(router, config, body, on_delta, *, refusals):
        calls.append(router.key)
        if len(calls) <= refusals:
            raise _Refused()
        return 200, {"choices": [{"message": {"content": "好"}}]}

    monkeypatch.setattr(openrouter, "_post", partial(post, refusals=3))
    result = await run_router(LOCAL_ROUTER, "q", local_cfg, "m")
    assert result.text == "好" and calls == ["local"] * 4  # three refusals, then the answer

    calls.clear()
    monkeypatch.setattr(openrouter, "_post", partial(post, refusals=4))
    with pytest.raises(RuntimeError, match="^地端模型 連線失敗：_Refused"):
        await run_router(LOCAL_ROUTER, "q", local_cfg, "m")
    assert calls == ["local"] * 4

    # only a refusal: a name or certificate failure will not change in a second
    calls.clear()

    async def unresolved(router, config, body, on_delta):
        calls.append(router.key)
        raise _NoSuchHost()

    monkeypatch.setattr(openrouter, "_post", unresolved)
    with pytest.raises(RuntimeError, match="^地端模型 連線失敗"):
        await run_router(LOCAL_ROUTER, "q", local_cfg, "m")
    assert calls == ["local"]

    calls.clear()
    monkeypatch.setattr(openrouter, "_post", partial(post, refusals=1))
    with pytest.raises(RuntimeError, match="^OrcaRouter 連線失敗"):
        await run_router(ORCA, "q", local_cfg, "m")
    assert calls == ["orcarouter"]


async def test_an_unconfigured_local_router_says_so(monkeypatch, cfg: Config) -> None:
    async def post(*args):
        raise AssertionError("nothing is sent without LOCAL_LLM_URL")

    monkeypatch.setattr(openrouter, "_post", post)
    with pytest.raises(RuntimeError, match="^地端模型 未設定$"):
        await run_router(LOCAL_ROUTER, "q", replace(cfg, local_llm_url=""), "m")


def test_strip_think_drops_only_a_leading_block() -> None:
    assert strip_think("<think>\n想一下\n</think>\n\n答案") == "答案"
    assert strip_think("  <think>x</think>答案") == "答案"
    assert strip_think("<think>還在想") == ""  # unclosed: no answer yet
    assert strip_think("答案 <think>x</think>") == "答案 <think>x</think>"  # not leading
    assert strip_think("<thi") == "<thi"  # a final answer is what it is
    assert strip_think("<thi", streaming=True) == ""  # a stream may still grow into <think>
    assert strip_think("<b>粗體</b>", streaming=True) == "<b>粗體</b>"


async def test_think_block_is_stripped_from_the_answer_and_the_transcript(
    monkeypatch, local_cfg: Config
) -> None:
    cfg = local_cfg
    content = "<think>\n使用者打招呼。\n</think>\n\n哈囉"
    session = Session([Response({"choices": [{"message": {"content": content}}]})])
    monkeypatch.setattr(openrouter, "_session", lambda config, timeout, router=None: session)
    result = await run_router(LOCAL_ROUTER, "q", cfg, "m")
    assert result.text == "哈囉"
    assert load_transcript(cfg, result.thread_id)[1]["content"] == "哈囉"
    # a hosted router answers as it was written: "<think>" may be the topic
    literal = {"choices": [{"message": {"content": "<think> 是推理標籤"}}]}
    monkeypatch.setattr(
        openrouter, "_session", lambda config, timeout, router=None: Session([Response(literal)])
    )
    assert (await run_router(ORCA, "q", cfg, "tencent/hy3-free")).text == "<think> 是推理標籤"
    # reasoning only: the usual empty-answer error
    only = {"choices": [{"message": {"content": "<think>想不完"}}]}
    monkeypatch.setattr(
        openrouter, "_session", lambda config, timeout, router=None: Session([Response(only)])
    )
    with pytest.raises(RuntimeError, match="有回應，但沒有答案文字"):
        await run_router(LOCAL_ROUTER, "q", cfg, "m")


async def test_streamed_display_hides_a_leading_think_block(monkeypatch, local_cfg: Config) -> None:
    pieces = ["<thi", "nk>想", "一下</thi", "nk>\n\n", "答", "案"]
    lines = [
        ("data: " + json.dumps({"choices": [{"delta": {"content": p}}]}) + "\n").encode()
        for p in pieces
    ] + [b"data: [DONE]\n"]

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

    result = await run_router(LOCAL_ROUTER, "q", local_cfg, "qwen3-8b-q4", on_delta=on_delta)
    assert seen == ["答", "答案"] and result.text == "答案"
    assert load_transcript(local_cfg, result.thread_id)[1]["content"] == "答案"


def _sse(chunks: list[dict]) -> Response:
    lines = [("data: " + json.dumps(c) + "\n").encode() for c in chunks] + [b"data: [DONE]\n"]

    class Body:
        def __aiter__(self):
            return self._gen()

        async def _gen(self):
            for line in lines:
                yield line

    class Streaming(Response):
        content = Body()

    return Streaming({})


async def test_hosted_routers_send_no_output_cap(monkeypatch, cfg: Config) -> None:
    session = Session([Response({"choices": [{"message": {"content": "好"}}]})])
    monkeypatch.setattr(openrouter, "_session", lambda config, timeout, router=None: session)
    await run_openrouter("q", cfg, "a/text:free")
    assert "max_tokens" not in session.calls[0][2]


async def test_thinking_that_uses_up_the_cap_is_reported_as_such(
    monkeypatch, local_cfg: Config
) -> None:
    # mlx_lm.server streams the thinking as `reasoning` deltas and ends on finish_reason length.
    session = Session(
        [
            _sse(
                [{"choices": [{"delta": {"reasoning": "想"}, "finish_reason": None}]}] * 3
                + [{"choices": [{"delta": {}, "finish_reason": "length"}]}]
            )
        ]
    )
    monkeypatch.setattr(openrouter, "_session", lambda config, timeout, router=None: session)

    async def on_delta(text):
        raise AssertionError("no answer text to show")

    with pytest.raises(BackendError) as caught:
        await run_router(LOCAL_ROUTER, "q", local_cfg, "m", on_delta=on_delta)
    assert str(caught.value) == (
        "地端模型 思考過程就用完了輸出長度上限（8192 token），還沒寫出答案。"
        "把問題問得更具體，或換一個模型試試"
    )
    # An unclosed <think> block in the content is the same case on servers without the field.
    session = Session(
        [Response({"choices": [{"message": {"content": "<think>想"}, "finish_reason": "length"}]})]
    )
    monkeypatch.setattr(openrouter, "_session", lambda config, timeout, router=None: session)
    with pytest.raises(BackendError, match="^地端模型 思考過程就用完了輸出長度上限"):
        await run_router(LOCAL_ROUTER, "q", local_cfg, "m")


async def test_an_empty_answer_does_not_claim_the_server_is_down(
    monkeypatch, local_cfg: Config
) -> None:
    session = Session(
        [Response({"choices": [{"message": {"content": ""}, "finish_reason": "stop"}]})]
    )
    monkeypatch.setattr(openrouter, "_session", lambda config, timeout, router=None: session)
    with pytest.raises(BackendError, match="^地端模型 有回應，但沒有答案文字，換一個模型試試$"):
        await run_router(LOCAL_ROUTER, "q", local_cfg, "m")
    session = Session(
        [Response({"choices": [{"message": {"content": ""}, "finish_reason": "length"}]})]
    )
    monkeypatch.setattr(openrouter, "_session", lambda config, timeout, router=None: session)
    with pytest.raises(
        BackendError, match="^地端模型 輸出在寫出答案前就達到長度上限（8192 token）"
    ):
        await run_router(LOCAL_ROUTER, "q", local_cfg, "m")


async def test_a_cut_off_answer_says_so_but_the_transcript_keeps_the_answer(
    monkeypatch, local_cfg: Config
) -> None:
    session = Session(
        [
            _sse(
                [
                    {"choices": [{"delta": {"content": "前半"}, "finish_reason": None}]},
                    {"choices": [{"delta": {}, "finish_reason": "length"}]},
                ]
            )
        ]
    )
    monkeypatch.setattr(openrouter, "_session", lambda config, timeout, router=None: session)

    async def on_delta(text):
        pass

    result = await run_router(LOCAL_ROUTER, "q", local_cfg, "m", on_delta=on_delta)
    assert result.text == "前半\n\n（回答達到長度上限，後面被截斷。）"
    assert load_transcript(local_cfg, result.thread_id)[1]["content"] == "前半"


def test_backends_accept_local_ids() -> None:
    choice = parse_choice("local:mlx-community/Qwen3-4B-4bit|low", "gpt-5.6-luna")
    assert choice.backend == LOCAL and choice.family == "mlx-community/Qwen3-4B-4bit"
    assert choice.label == "地端模型 · mlx-community/Qwen3-4B-4bit"
    target = resolve(choice, "low")
    assert (target.backend, target.model) == (LOCAL, "mlx-community/Qwen3-4B-4bit")
