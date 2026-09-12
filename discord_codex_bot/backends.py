from __future__ import annotations

from dataclasses import dataclass

CODEX = "codex"
AGY = "agy"

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


def parse_choice(value: str, codex_model: str) -> ModelChoice:
    """Resolve a stored value; unknown or stale values fall back to the Codex default.
    Values stored by the earlier slug-based command ("agy:gemini-3.8-flash-high") still map."""
    for choice in choices(codex_model):
        if choice.value == value:
            return choice
    if value.startswith(f"{AGY}:"):
        slug = value.split(":", 1)[1]
        for fam, (_, by_effort) in AGY_FAMILIES.items():
            if slug in by_effort.values():
                return parse_choice(f"{AGY}:{fam}", codex_model)
    return choices(codex_model)[0]


def resolve(choice: ModelChoice, effort: str) -> Resolved:
    """Map the shared effort option onto what this backend/family can actually run."""
    if choice.backend == CODEX:
        return Resolved(CODEX, choice.family, effort)
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
