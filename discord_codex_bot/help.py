"""The single source for what this Bot can do, in two renderings: the detailed member guide
behind /<prefix>-help (explanations and worked examples) and the compact sheet injected into the
model's prompt so it can describe itself truthfully. Command names and parameters come from the
registered command tree; the prose here is keyed by command suffix, and a test insists the two
sets match exactly — add a command without a guide entry (or the reverse) and CI fails, which is
how this file is kept honest as features land."""

from __future__ import annotations

from collections.abc import Iterable

# suffix -> (what it does, worked examples). "{p}" is the command prefix.
COMMAND_GUIDE: dict[str, tuple[str, list[str]]] = {
    "": (
        "問問題。可附一張圖、選推理強度、或用 new 忽略之前的對話從頭開始。"
        "回覆會引用你的問題；同一頻道的下一題預設會接續上一段對話。",
        [
            "/{p} prompt:幫我解釋量子糾纏，三句話",
            "/{p} prompt:這張圖裡是什麼遊戲 image:（附上圖片）",
            "/{p} prompt:再來一題 effort:High",
            "/{p} prompt:換個話題 new:True",
        ],
    ),
    "-help": ("列出所有指令的說明與範例（就是這一頁）。", ["/{p}-help"]),
    "-status": (
        "看你目前的設定（會走哪個模型與強度、個人風格、下一句會不會接續上一段對話、"
        "記憶用量）和系統狀態（各後端與功能是否可用）。只有你看得到。",
        ["/{p}-status"],
    ),
    "-model": (
        "選你要用的模型：provider（Codex／Antigravity／OpenRouter／OrcaRouter）→ model（打字會自動"
        "篩選；OpenRouter、OrcaRouter 只列免費模型）→ effort（這個模型的預設強度）。留空＝查看目前"
        "設定；clear 回到預設 Codex。換模型後下一題會新開對話。",
        [
            "/{p}-model provider:Antigravity model:gemini-3.8-flash effort:Medium",
            "/{p}-model provider:OpenRouter model:gemma（打幾個字就會出現候選）",
            "/{p}-model（留空：看目前用什麼）",
            "/{p}-model clear:True",
        ],
    ),
    "-style": (
        "設定你個人的回覆風格，會覆蓋伺服器預設（人設也會換成不帶角色的版本）。"
        "留空＝查看；clear 清除回到預設。",
        [
            "/{p}-style text:條列、少於 100 字、用英文",
            "/{p}-style（留空：看目前風格）",
            "/{p}-style clear:True",
        ],
    ),
    "-remember": (
        "叫 Bot 記住一件事。scope 選「個人」只對你有效、「伺服器」這裡所有人都適用；"
        "name 是短標題（之後用它刪除），text 是內容。Bot 在對話中也會自己記下你說的長期偏好。",
        [
            "/{p}-remember scope:個人 name:拉麵 text:我最愛豚骨拉麵，不吃香菜",
            "/{p}-remember scope:伺服器 name:開團時間 text:每週五晚上九點開團",
        ],
    ),
    "-forget": (
        "刪除一則記憶。用 /{p}-memory 查名稱後，指定 scope 與 name。",
        ["/{p}-forget scope:個人 name:拉麵"],
    ),
    "-memory": (
        "列出 Bot 記得的事的索引（個人＋伺服器）；scope 可只看其中一層。只有你看得到。",
        ["/{p}-memory", "/{p}-memory scope:伺服器"],
    ),
    "-reset": (
        "忘掉你在這個頻道的對話脈絡，下一題從頭開始（記憶不會被刪）。",
        ["/{p}-reset"],
    ),
}

# Abilities that are not commands; shown to members and told to the model alike.
FEATURES: list[str] = [
    "**@提及** Bot 也能問，不必打指令；訊息裡的圖片會一起看。",
    "**回覆某則訊息**再 @Bot：接續那段對話，或讓 Bot 看那則訊息的文字與圖。",
    "**貼連結**會自動讀網頁（含 X／fixvx 貼文的全文與圖片；網站擋 Bot 時退回 Discord 預覽）。",
    "**貼影片連結**會看影片再回答：YouTube 直接看（長片也行），X、TikTok、Instagram、Bilibili、"
    "Reddit、Streamable 等會抓下來看；長片會先回「🎬 處理中」再改成正式答案。",
    "**模型來源**：Codex（預設）、Antigravity（Gemini／Claude）、"
    "OpenRouter 與 OrcaRouter 的免費模型；免費模型可能不穩或下架，回錯就換一個。",
    "**記憶**分個人與伺服器兩層，另有管理者維護的永久記憶；Bot 會在需要時自己查閱。",
]


def render_guide(prefix: str, registered: Iterable[str]) -> str:
    """The member-facing guide for /<prefix>-help, in registered-command order."""
    p = prefix
    blocks = [f"**/{p} 指令說明**"]
    for name in registered:
        suffix = name.removeprefix(prefix)
        summary, examples = COMMAND_GUIDE[suffix]
        lines = [f"**/{name}**", summary.replace("{p}", p)]
        lines += [f"　`{example.replace('{p}', p)}`" for example in examples]
        blocks.append("\n".join(lines))
    blocks.append("**不用指令也能做的事**\n" + "\n".join(f"・{f}" for f in FEATURES))
    return "\n\n".join(blocks)


def render_sheet(prefix: str, commands: Iterable[tuple[str, str, list[str]]]) -> str:
    """The compact sheet injected into the model's prompt: one line per command plus the
    feature list, Markdown stripped so it reads as facts rather than formatting."""
    lines = [f"這個 Bot 的斜線指令（前綴 /{prefix}）："]
    for name, description, params in commands:
        suffix = f"（參數：{'、'.join(params)}）" if params else ""
        lines.append(f"/{name} — {description}{suffix}")
    lines.append("其他用法：" + " ".join(f.replace("**", "") for f in FEATURES))
    return "\n".join(lines)
