from __future__ import annotations

from dataclasses import dataclass

CODEX = "codex"
AGY = "agy"
DEFAULT_CHOICE = "codex:gpt-5.6-luna"

# Verified 2026-09-12 against `agy models` and a full model × --effort matrix: the reasoning
# effort is part of the slug, `--effort` is only accepted when it repeats the suffix, and the
# Claude models accept no effort at all. So each slug is one complete choice.
AGY_MODELS: dict[str, str] = {
    "gemini-3.8-flash-high": "Gemini 3.8 Flash · High",
    "gemini-3.8-flash-medium": "Gemini 3.8 Flash · Medium",
    "gemini-3.8-flash-low": "Gemini 3.8 Flash · Low",
    "gemini-3.7-flash-high": "Gemini 3.7 Flash · High",
    "gemini-3.7-flash-medium": "Gemini 3.7 Flash · Medium",
    "gemini-3.7-flash-low": "Gemini 3.7 Flash · Low",
    "gemini-3.6-flash-high": "Gemini 3.6 Flash · High",
    "gemini-3.6-flash-medium": "Gemini 3.6 Flash · Medium",
    "gemini-3.6-flash-low": "Gemini 3.6 Flash · Low",
    "gemini-3.1-pro-high": "Gemini 3.1 Pro · High",
    "gemini-3.1-pro-low": "Gemini 3.1 Pro · Low",
    "claude-sonnet-4-6": "Claude Sonnet 4.6 (Thinking)",
    "claude-opus-4-6-thinking": "Claude Opus 4.6 (Thinking)",
    "gpt-oss-120b-medium": "GPT-OSS 120B · Medium",
}


@dataclass(frozen=True, slots=True)
class ModelChoice:
    value: str  # "<backend>:<model>", what the slash command stores
    label: str
    backend: str
    model: str


def choices(codex_model: str) -> list[ModelChoice]:
    """Every selectable model: the Codex default (effort picked per request) plus all agy slugs."""
    out = [
        ModelChoice(
            f"{CODEX}:{codex_model}",
            f"Codex · {codex_model}（強度用 effort 選）",
            CODEX,
            codex_model,
        )
    ]
    out += [ModelChoice(f"{AGY}:{slug}", label, AGY, slug) for slug, label in AGY_MODELS.items()]
    return out


def parse_choice(value: str, codex_model: str) -> ModelChoice:
    """Resolve a stored value; unknown or stale values fall back to the Codex default."""
    for choice in choices(codex_model):
        if choice.value == value:
            return choice
    return choices(codex_model)[0]
