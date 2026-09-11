from __future__ import annotations

DISCORD_MESSAGE_LIMIT = 2_000


def truncate(text: str, max_chars: int) -> str:
    suffix = "\n\n[輸出已截斷]"
    if len(text) <= max_chars:
        return text
    return f"{text[: max_chars - len(suffix)]}{suffix}"


def split_discord_message(text: str, limit: int = DISCORD_MESSAGE_LIMIT) -> list[str]:
    chunks: list[str] = []
    remaining = text.strip() or "Codex 沒有回傳文字。"
    while len(remaining) > limit:
        split_at = remaining.rfind("\n", 0, limit + 1)
        if split_at < limit // 2:
            split_at = remaining.rfind(" ", 0, limit + 1)
        if split_at < limit // 2:
            split_at = limit
        chunks.append(remaining[:split_at].rstrip())
        remaining = remaining[split_at:].lstrip()
    chunks.append(remaining)
    return chunks
