from discord_codex_bot.output import (
    PROMPT_ECHO_CHARS,
    format_reply,
    split_discord_message,
    truncate,
)


def test_splits_every_message_below_limit() -> None:
    chunks = split_discord_message("alpha beta gamma delta epsilon", 10)
    assert len(chunks) > 1
    assert all(len(chunk) <= 10 for chunk in chunks)
    assert " ".join(chunks) == "alpha beta gamma delta epsilon"


def test_truncates_oversized_response() -> None:
    value = truncate("x" * 100, 50)
    assert len(value) == 50
    assert value.endswith("[輸出已截斷]")


def test_format_reply_quotes_prompt_and_marks_image() -> None:
    reply = format_reply("第一行\n第二行", "答案", has_image=True)
    assert reply == "**問**（附圖）：\n> 第一行\n> 第二行\n\n答案"
    long_prompt = "x" * (PROMPT_ECHO_CHARS + 50)
    assert f"> {'x' * PROMPT_ECHO_CHARS}…\n" in format_reply(long_prompt, "答案")
