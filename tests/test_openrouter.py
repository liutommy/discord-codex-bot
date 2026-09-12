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

CATALOG = {"data": [
    {"id": "b/vision:free", "name": "Vision", "pricing": {"prompt": "0", "completion": "0"},
     "architecture": {"input_modalities": ["text", "image"], "output_modalities": ["text"]},
     "supported_parameters": ["reasoning", "tools"], "context_length": 4096},
    {"id": "a/text:free", "name": "Alpha", "pricing": {"prompt": "0", "completion": "0"},
     "architecture": {"input_modalities": ["text"], "output_modalities": ["text"]},
     "supported_parameters": ["tools"], "context_length": 8192},
    {"id": "paid/model", "name": "Paid", "pricing": {"prompt": "0.001", "completion": "0"},
     "architecture": {"input_modalities": ["text"], "output_modalities": ["text"]}},
    {"id": "music/free", "name": "Music", "pricing": {"prompt": "0", "completion": "0"},
     "architecture": {"input_modalities": ["text"], "output_modalities": ["text", "audio"]}},
    {"id": "x/content-safety:free", "name": "Guard", "pricing": {"prompt": "0", "completion": "0"},
     "architecture": {"input_modalities": ["text"], "output_modalities": ["text"]}},
    {"id": "thinkingmachines/inkling:free", "name": "Ink",
     "pricing": {"prompt": "0", "completion": "0"},
     "architecture": {"input_modalities": ["text"], "output_modalities": ["text"]}},
]}


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
        config, openrouter_dir=tmp_path / "or", codex_workspace=tmp_path / "ws",
        codex_workspace_plain=tmp_path / "plain", output_style_path=tmp_path / "none.md",
    )


def test_parse_catalog_keeps_free_text_chat_models_image_first() -> None:
    models = parse_catalog(CATALOG)
    assert [m.id for m in models] == ["b/vision:free", "a/text:free"]
    assert models[0].image and models[0].reasoning and models[0].context == 4096
    assert not models[1].image and not models[1].reasoning


async def test_catalog_caches_and_keeps_the_last_good_list(monkeypatch, cfg: Config) -> None:
    sessions = [Session([Response(CATALOG)]), Session([Response({}, status=503)])]
    monkeypatch.setattr(openrouter, "_session", lambda config, timeout: sessions.pop(0))
    catalog = Catalog(cfg)
    assert [m.id for m in await catalog.free_models()] == ["b/vision:free", "a/text:free"]
    assert await catalog.free_models() and not sessions[0].calls  # fresh: no second request
    catalog._at = float("-inf")  # expire
    assert [m.id for m in await catalog.free_models()] == ["b/vision:free", "a/text:free"]
    assert catalog.get("a/text:free") is not None and catalog.get("nope") is None


def test_trim_history_keeps_newest_whole_messages_within_budget() -> None:
    messages = [{"role": "user", "content": "a" * 50}, {"role": "assistant", "content": "b" * 50},
                {"role": "user", "content": [{"type": "text", "text": "c" * 30}]}]
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
    monkeypatch.setattr(openrouter, "_session", lambda config, timeout: session)
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
    monkeypatch.setattr(openrouter, "_session", lambda config, timeout: first)
    one = await run_openrouter("q1", cfg, "a/text:free", effort="high", catalog=catalog)
    assert "reasoning" not in first.calls[0][2]  # the catalog says this model takes no effort
    content = [{"type": "text", "text": "二"}]
    second = Session([Response({"choices": [{"message": {"content": content}}]})])
    monkeypatch.setattr(openrouter, "_session", lambda config, timeout: second)
    two = await run_openrouter(
        "q2", cfg, "a/text:free", images=[image], resume=one.thread_id, personal_style="短",
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
        "user", "assistant", "user", "assistant"
    ]
    # an unknown thread id starts fresh instead of failing
    third = Session([Response({"choices": [{"message": {"content": "三"}}]})])
    monkeypatch.setattr(openrouter, "_session", lambda config, timeout: third)
    fresh = await run_openrouter("q3", cfg, "a/text:free", resume="or-missing", catalog=catalog)
    assert not fresh.resumed and fresh.thread_id != "or-missing"


async def test_run_openrouter_reports_api_errors_with_the_free_model_hint(
    monkeypatch, cfg: Config
) -> None:
    session = Session([Response({"error": {"message": "Rate limit exceeded"}}, status=429)])
    monkeypatch.setattr(openrouter, "_session", lambda config, timeout: session)
    with pytest.raises(RuntimeError, match="HTTP 429：Rate limit exceeded（免費模型可能暫時不可用"):
        await run_openrouter("q", cfg, "a/text:free")
    session = Session([Response({"error": {"message": "Upstream overloaded"}}, status=200)])
    monkeypatch.setattr(openrouter, "_session", lambda config, timeout: session)
    with pytest.raises(RuntimeError, match="OpenRouter 上游錯誤：Upstream overloaded"):
        await run_openrouter("q", cfg, "a/text:free")
    session = Session([Response({"choices": [{"message": {"content": ""}}]})])
    monkeypatch.setattr(openrouter, "_session", lambda config, timeout: session)
    with pytest.raises(RuntimeError, match="沒有回傳文字"):
        await run_openrouter("q", cfg, "a/text:free")
    assert load_transcript(cfg, "or-nothing") == []


async def test_harvest_reads_the_bot_kept_transcript(monkeypatch, cfg: Config) -> None:
    session = Session([Response({"choices": [{"message": {"content": "答"}}]})])
    monkeypatch.setattr(openrouter, "_session", lambda config, timeout: session)
    result = await run_openrouter("我叫小明", cfg, "a/text:free", memory="- x")
    assert transcript(cfg, result.thread_id) == "後輩：我叫小明\n\n前輩：答"
    assert transcript(cfg, "or-unknown") == ""


def test_backends_accept_any_openrouter_model_id() -> None:
    choice = parse_choice("openrouter:google/gemma-4-31b-it:free|high", "gpt-5.6-luna")
    assert choice.backend == OPENROUTER and choice.family == "google/gemma-4-31b-it:free"
    assert choice.value == "openrouter:google/gemma-4-31b-it:free"
    target = resolve(choice, "high")
    assert (target.backend, target.model, target.effort) == (
        OPENROUTER, "google/gemma-4-31b-it:free", "high"
    )
    assert parse_choice("openrouter:", "gpt-5.6-luna").backend == "codex"  # empty id ⇒ default
