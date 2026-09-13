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
        "問問題。可附一張圖或一份檔案（PDF／文字／程式碼，Bot 會讀內容）、選推理強度、"
        "或用 new 忽略之前的對話從頭開始。"
        "回覆會引用你的問題；同一頻道的下一題預設會接續上一段對話。",
        [
            "/{p} prompt:幫我解釋量子糾纏，三句話",
            "/{p} prompt:這張圖裡是什麼遊戲 image:（附上圖片）",
            "/{p} prompt:幫我看這份合約有沒有坑 image:（附上 PDF）",
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
    "-summary": (
        "摘要這個頻道最近的對話：重點、結論、待辦、誰說了什麼。預設最近 100 則，可改則數或改成"
        "最近幾小時；focus 可指定特別想知道的事。結果公開貼在頻道，不會混進你自己的對話。",
        [
            "/{p}-summary",
            "/{p}-summary count:300",
            "/{p}-summary hours:12 focus:有沒有人約時間",
        ],
    ),
    "-remind": (
        "設定提醒：到時 Bot 會在這個頻道 @你。時間可以寫相對（30分鐘後、2小時後、3天後）、"
        "今天／明天／後天加時間（明天 9:30、後天下午3點、21:00）、或日期（9/15 14:30）。"
        "who 可以指定要 @ 的人（留空＝提醒自己）。留空列出你的提醒，cancel 加編號取消。"
        "直接用講的也行：跟前輩說「明天九點提醒我倒垃圾」「把那個提醒取消」，它會自己設／取消並回報編號。",
        [
            "/{p}-remind when:30分鐘後 text:去收衣服",
            "/{p}-remind when:明天 20:00 text:開團囉 who:@小明",
            "/{p}-remind when:明天 9:30 text:開會前先看簡報",
            "/{p}-remind（留空：列出你的提醒）",
            "/{p}-remind cancel:3",
        ],
    ),
    "-export": (
        "把你在這個伺服器的個人記憶（索引、archive、每一則內容）打包成 zip 私下給你，"
        "當作自己的備份或搬家用。伺服器記憶不在裡面。",
        ["/{p}-export"],
    ),
    "-stop": (
        "取消你在這個頻道進行中的請求（例如影片太長不想等）。"
        "回答生成中的「🤔 思考中」訊息上也有 ❌ 按鈕。",
        ["/{p}-stop"],
    ),
}

# Abilities that are not commands; shown to members and told to the model alike.
FEATURES: list[str] = [
    "**@提及** Bot 也能問，不必打指令；訊息裡的圖片會一起看，"
    "附的檔案（PDF／文字／程式碼）會讀內容。",
    "**回覆某則訊息**再 @Bot：接續那段對話，或讓 Bot 看那則訊息的文字與圖。",
    "**貼連結**會自動讀網頁（含 X／fixvx 貼文的全文與圖片；網站擋 Bot 時退回 Discord 預覽）。",
    "**貼影片連結**會看影片再回答：YouTube 直接看（長片也行），X、TikTok、Instagram、Bilibili、"
    "Reddit、Streamable 等會抓下來看；長片會先回「🎬 處理中」再改成正式答案。",
    "**模型來源**：Codex（預設）、Antigravity（Gemini／Claude）、"
    "OpenRouter 與 OrcaRouter 的免費模型；免費模型可能不穩或下架，回錯就換一個。",
    "**記憶**分個人與伺服器兩層，另有管理者維護的永久記憶；Bot 會在需要時自己查閱。",
    "**會上網搜尋**：問到需要最新或可查證的事，Bot 會自己搜尋再讀網頁，不用你貼連結。",
    "**會寫程式算東西**：算數、資料整理、格式轉換、影片抽幀，Bot 會在隔離沙盒裡跑 Python／shell，"
    "產生的檔案直接附給你。",
    "**回答上的按鈕**：生成中 ❌ 取消；答完後 🔁 用同一題重答（新開對話）、"
    "👍 把這段問答記進個人記憶。只有發問的人能按。",
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
