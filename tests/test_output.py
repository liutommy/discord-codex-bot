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


def test_split_prefers_newline_then_space_then_hard_cut() -> None:
    assert split_discord_message("") == ["Codex 沒有回傳文字。"]
    assert split_discord_message("  \n ") == ["Codex 沒有回傳文字。"]
    assert split_discord_message("a" * 8 + "\n" + "b" * 8, 10) == ["a" * 8, "b" * 8]
    assert split_discord_message("ab\ncdefg hijkl", 10) == ["ab\ncdefg", "hijkl"]  # early \n
    assert split_discord_message("x" * 25, 10) == ["x" * 10, "x" * 10, "x" * 5]
    assert split_discord_message("short", 10) == ["short"]
    assert split_discord_message("x" * 10, 10) == ["x" * 10]


def test_format_reply_lists_every_tag_and_handles_empty_prompt() -> None:
    reply = format_reply("q", "a", has_image=True, effort="Medium", resumed=True)
    assert reply == "**問**（Medium、附圖、續接）：\n> q\n\na"
    assert format_reply("", "a") == "**問**：\n> \n\na"
    assert format_reply("q", "a", effort="gemini-3.8-flash-high · High") == (
        "**問**（gemini-3.8-flash-high · High）：\n> q\n\na"
    )


def test_truncate_keeps_text_at_or_below_the_limit() -> None:
    assert truncate("abc", 3) == "abc"
    assert truncate("", 5) == ""
