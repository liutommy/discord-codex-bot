from discord_codex_bot.output import split_discord_message, truncate


def test_splits_every_message_below_limit() -> None:
    chunks = split_discord_message("alpha beta gamma delta epsilon", 10)
    assert len(chunks) > 1
    assert all(len(chunk) <= 10 for chunk in chunks)
    assert " ".join(chunks) == "alpha beta gamma delta epsilon"


def test_truncates_oversized_response() -> None:
    value = truncate("x" * 100, 50)
    assert len(value) == 50
    assert value.endswith("[輸出已截斷]")
