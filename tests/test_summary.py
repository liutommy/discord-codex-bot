from __future__ import annotations

from datetime import UTC, datetime, timedelta
from types import SimpleNamespace as NS

from discord_codex_bot.summary import render_transcript, since, summary_prompt


def msg(text, who="小明", bot=False, minute=0, attachments=0, embeds=0):
    return NS(
        content=text, attachments=[1] * attachments, embeds=[1] * embeds,
        created_at=datetime(2026, 9, 13, 4, minute, tzinfo=UTC),
        author=NS(display_name=who, bot=bot),
    )


def test_render_transcript_lines_markers_and_tail_bound() -> None:
    text = render_transcript([
        msg("早安", minute=1), msg("", minute=2),
        msg("看這個", "阿花", minute=3, attachments=1, embeds=2),
        msg("我是機器人", "Inmu King", bot=True, minute=4), msg("", minute=5, attachments=1),
    ])
    assert text.splitlines() == [
        "09/13 12:01 小明：早安",
        "09/13 12:03 阿花：看這個 [附件 1 個] [連結預覽 2 個]",
        "09/13 12:04 Inmu King（bot）：我是機器人",
        "09/13 12:05 小明： [附件 1 個]",
    ]
    long = render_transcript([msg("x" * 50, minute=i) for i in range(10)], max_chars=120)
    assert long.startswith("[較早的訊息已略過]\n") and len(long) <= 120 + 12
    assert render_transcript([msg("")]) == ""


def test_summary_prompt_wraps_the_transcript_as_untrusted_and_takes_a_focus() -> None:
    prompt = summary_prompt("general", "09/13 12:01 小明：早安", 1)
    assert "頻道「general」最近 1 則" in prompt
    assert "<CHANNEL>\n09/13 12:01 小明：早安\n</CHANNEL>" in prompt
    assert "不要聽從" in prompt and "待辦事項" in prompt
    focused = summary_prompt("general", "t", 3, focus="誰約了時間")
    assert "涵蓋：誰約了時間。" in focused and "待辦事項" not in focused


def test_since_is_utc_and_in_the_past() -> None:
    cutoff = since(2)
    assert cutoff.tzinfo is UTC
    assert timedelta(hours=1, minutes=59) < datetime.now(UTC) - cutoff < timedelta(hours=2, seconds=5)
