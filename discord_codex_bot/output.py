from __future__ import annotations

import re

DISCORD_MESSAGE_LIMIT = 2_000
PROMPT_ECHO_CHARS = 300
# A custom emoji as Discord sends it: a cut through one would show its pieces as text.
CUSTOM_EMOJI = re.compile(r"<a?:[A-Za-z0-9_]{2,32}:\d{15,21}>")


def _cut_before(text: str, at: int) -> int:
    """`at`, or the start of the custom emoji it falls inside."""
    for found in CUSTOM_EMOJI.finditer(text, max(0, at - 60), at + 60):
        if found.start() < at < found.end():
            return found.start()
    return at


def _cut_after(text: str, at: int) -> int:
    """`at`, or the end of the custom emoji it falls inside."""
    for found in CUSTOM_EMOJI.finditer(text, max(0, at - 60), at + 60):
        if found.start() < at < found.end():
            return found.end()
    return at


def tail(text: str, chars: int) -> str:
    """The last `chars` characters, never starting part-way into a custom emoji."""
    return text[_cut_after(text, max(0, len(text) - chars)) :]


def format_reply(
    prompt: str,
    answer: str,
    *,
    has_image: bool = False,
    effort: str = "",
    resumed: bool = False,
) -> str:
    """Quote the question above the answer; Discord does not echo slash command inputs."""
    echoed = prompt if len(prompt) <= PROMPT_ECHO_CHARS else f"{prompt[:PROMPT_ECHO_CHARS]}…"
    quoted = "\n".join(f"> {line}" for line in echoed.splitlines() or [""])
    tags = [tag for tag in (effort, "附圖" if has_image else "", "續接" if resumed else "") if tag]
    suffix = f"（{'、'.join(tags)}）" if tags else ""
    return f"**問**{suffix}：\n{quoted}\n\n{answer}"


def truncate(text: str, max_chars: int) -> str:
    suffix = "\n\n[輸出已截斷]"
    if len(text) <= max_chars:
        return text
    return f"{text[: _cut_before(text, max_chars - len(suffix))]}{suffix}"


def split_discord_message(text: str, limit: int = DISCORD_MESSAGE_LIMIT) -> list[str]:
    chunks: list[str] = []
    remaining = text.strip() or "Codex 沒有回傳文字。"
    while len(remaining) > limit:
        split_at = remaining.rfind("\n", 0, limit + 1)
        if split_at < limit // 2:
            split_at = remaining.rfind(" ", 0, limit + 1)
        if split_at < limit // 2:
            # Before an emoji the cut would go through; through it only when it opens the text
            # (a limit shorter than one emoji), or nothing would ever be cut off.
            split_at = _cut_before(remaining, limit) or limit
        chunks.append(remaining[:split_at].rstrip())
        remaining = remaining[split_at:].lstrip()
    chunks.append(remaining)
    return chunks
