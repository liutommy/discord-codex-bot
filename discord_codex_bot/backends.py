from __future__ import annotations

import logging
from dataclasses import dataclass

LOGGER = logging.getLogger(__name__)
CODEX = "codex"
AGY = "agy"
OPENROUTER = "openrouter"
ORCAROUTER = "orcarouter"
# OpenAI-compatible routers: any model id is a valid choice value; the live catalog decides
# what the autocomplete offers. Members store "<backend>:<model id>".
ROUTER_BACKENDS = (OPENROUTER, ORCAROUTER)
ROUTER_LABELS = {OPENROUTER: "OpenRouter", ORCAROUTER: "OrcaRouter"}

# Verified 2026-09-12 against `agy models` and a full model × --effort matrix: agy bakes the
# reasoning effort into the model slug (`-high/-medium/-low`), `--effort` is only accepted when
# it repeats that suffix, and the Claude / gpt-oss models take no effort at all. Members therefore
# pick a *family* here and the shared `effort` option is mapped onto a legal slug per family.
# family value -> (label, {effort: slug})  — an effort missing from the map falls back to the
# family's strongest listed level (xhigh/max on Gemini become high; Claude ignores effort).
AGY_FAMILIES: dict[str, tuple[str, dict[str, str]]] = {
    "gemini-3.8-flash": (
        "Gemini 3.8 Flash",
        {lvl: f"gemini-3.8-flash-{lvl}" for lvl in ("low", "medium", "high")},
    ),
    "gemini-3.7-flash": (
        "Gemini 3.7 Flash",
        {lvl: f"gemini-3.7-flash-{lvl}" for lvl in ("low", "medium", "high")},
    ),
    "gemini-3.6-flash": (
        "Gemini 3.6 Flash",
        {lvl: f"gemini-3.6-flash-{lvl}" for lvl in ("low", "medium", "high")},
    ),
    "gemini-3.1-pro": (
        "Gemini 3.1 Pro",
        {"low": "gemini-3.1-pro-low", "high": "gemini-3.1-pro-high"},
    ),
    "claude-sonnet-4-6": ("Claude Sonnet 4.6（固定 thinking）", {"": "claude-sonnet-4-6"}),
    "claude-opus-4-6": ("Claude Opus 4.6（固定 thinking）", {"": "claude-opus-4-6-thinking"}),
    "gpt-oss-120b": ("GPT-OSS 120B（固定 medium）", {"": "gpt-oss-120b-medium"}),
}
_ORDER = ["low", "medium", "high", "xhigh", "max"]


@dataclass(frozen=True, slots=True)
class ModelChoice:
    value: str  # "<backend>:<family>", what the slash command stores
    label: str
    backend: str
    family: str


@dataclass(frozen=True, slots=True)
class Resolved:
    backend: str
    model: str  # exact slug / model name to run
    effort: str  # effort actually applied ("" = the model has none)


def choices(codex_model: str) -> list[ModelChoice]:
    out = [ModelChoice(f"{CODEX}:{codex_model}", f"Codex · {codex_model}", CODEX, codex_model)]
    out += [
        ModelChoice(f"{AGY}:{fam}", label, AGY, fam) for fam, (label, _) in AGY_FAMILIES.items()
    ]
    return out


def router_choice(backend: str, model_id: str, name: str = "") -> ModelChoice:
    """A router model as a choice: the free tier changes weekly, so any id is a valid value."""
    label = f"{ROUTER_LABELS[backend]} · {name or model_id}"
    return ModelChoice(f"{backend}:{model_id}", label, backend, model_id)


def openrouter_choice(model_id: str, name: str = "") -> ModelChoice:
    return router_choice(OPENROUTER, model_id, name)


def split_stored(value: str) -> tuple[str, str]:
    """Stored per-member value "<backend>:<family>|<effort>" -> (choice value, effort or "")."""
    choice, _, effort = value.partition("|")
    return choice, effort.strip()


def parse_choice(value: str, codex_model: str) -> ModelChoice:
    """Resolve a stored value; unknown or stale values fall back to the Codex default.
    Values stored by the earlier slug-based command ("agy:gemini-3.8-flash-high") still map."""
    value = split_stored(value)[0]
    for choice in choices(codex_model):
        if choice.value == value:
            return choice
    for backend in ROUTER_BACKENDS:
        if value.startswith(f"{backend}:") and len(value) > len(backend) + 1:
            return router_choice(backend, value.split(":", 1)[1])
    if value.startswith(f"{AGY}:"):
        slug = value.split(":", 1)[1]
        for fam, (_, by_effort) in AGY_FAMILIES.items():
            if slug in by_effort.values():
                return parse_choice(f"{AGY}:{fam}", codex_model)
    return choices(codex_model)[0]


async def run_batch(
    prompt: str,
    config,
    *,
    schema=None,
    effort: str = "",
    isolated: bool = False,
) -> str:
    """One background turn (memory consolidation, social classification) as text.

    Batch jobs have nobody watching to retry them, so a spent subscription or temporarily full
    model must not simply fail: the same CODEX_FALLBACK_MODEL that answers members takes over.
    agy keeps its own deny-list
    (commands, writes, URL reads and MCP are all refused) and is given a fresh conversation each
    time, which is what `isolated` buys on the Codex side.
    """
    from .agy import run_agy
    from .codex import CodexFallbackError, run_codex

    try:
        result = await run_codex(
            prompt, config, effort=effort, raw=True, schema=schema, isolated=isolated
        )
        return result.text
    except CodexFallbackError as unavailable:
        spare = fallback_target(
            config.codex_fallback_model, config.codex_model, config.codex_reasoning_effort
        )
        if spare is None or spare.backend != AGY:
            raise
        LOGGER.warning(
            "Codex unavailable (%s: %s); running this batch on %s",
            type(unavailable).__name__,
            unavailable,
            spare.model,
        )
        result = await run_agy(prompt, config, spare.model, raw=True, schema=schema, plain=True)
        return result.text


def fallback_target(stored: str, codex_model: str, default_effort: str) -> Resolved | None:
    """The spare backend to answer on while Codex quota/capacity is unavailable, written like a
    member's stored model ("<backend>:<family>|<effort>"). Empty means no fallback; so does Codex
    itself, which cannot stand in for its own outage."""
    if not stored:
        return None
    choice = parse_choice(stored, codex_model)
    if choice.backend == CODEX:
        return None
    return resolve(choice, split_stored(stored)[1] or default_effort)


def resolve(choice: ModelChoice, effort: str) -> Resolved:
    """Map the shared effort option onto what this backend/family can actually run."""
    if choice.backend == CODEX:
        return Resolved(CODEX, choice.family, effort)
    if choice.backend in ROUTER_BACKENDS:
        return Resolved(choice.backend, choice.family, effort)  # applied only if the model takes it
    _label, by_effort = AGY_FAMILIES[choice.family]
    if "" in by_effort:
        return Resolved(AGY, by_effort[""], "")
    if effort in by_effort:
        return Resolved(AGY, by_effort[effort], effort)
    # requested level not offered: take the closest level at or below it, else the lowest
    wanted = _ORDER.index(effort) if effort in _ORDER else len(_ORDER)
    for level in reversed(_ORDER[: wanted + 1]):
        if level in by_effort:
            return Resolved(AGY, by_effort[level], level)
    lowest = next(level for level in _ORDER if level in by_effort)
    return Resolved(AGY, by_effort[lowest], lowest)
