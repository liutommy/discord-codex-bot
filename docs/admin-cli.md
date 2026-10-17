# 維運 CLI（`discord_codex_bot.admin`）操作手冊

這個 CLI 讓 operator 在**不重啟 Bot** 的情況下，做成員或伺服器管理員在 Discord 上能做的事：
幫成員設定或取消提醒、管理記憶與模型、新增或停掉社群追蹤、切換伺服器設定、更換人設，
以及以成員身分提問或摘要頻道，並查看 Bot 的系統狀態。

所有說明也都在 CLI 的 `--help` 裡（每個指令都有選項、限制、範例與結束碼），內容和程式碼出自
同一份 schema。本文件補充背後的運作方式、檢查規則與疑難排解。

文中的 `/remind` 等 slash 指令，實際名稱是 `/<COMMAND_PREFIX>-remind`（預設 `/codex-remind`）；
`/codex` 指 `/<COMMAND_PREFIX>` 本身。

## 目錄

1. [運作方式](#運作方式)
2. [怎麼執行](#怎麼執行)
3. [找到 id](#找到-id)
4. [共同選項與檢查](#共同選項與檢查)
5. [指令一覽](#指令一覽)
6. [結果、狀態與結束碼](#結果狀態與結束碼)
7. [逾時、Ctrl-C 與撤回](#逾時ctrl-c-與撤回)
8. [收件匣、權限與安全邊界](#收件匣權限與安全邊界)
9. [疑難排解](#疑難排解)
10. [其他維運指令](#其他維運指令)

## 運作方式

提醒、對話串、路由標記、額度與進行中的請求都存在 Bot 的記憶體裡，存檔時整份覆寫；別的行程
直接改檔案，會被 Bot 忽略再蓋掉。所以 CLI 自己**什麼都不做**：

1. CLI 檢查參數（型別、長度、範圍），把請求寫成 `CODEX_HOME/admin/inbox/<id>.json`。
2. Bot 每 2 秒看一次收件匣，把請求改名搬進 `processing/`（從這一刻起算「已接手」），
   重新檢查參數，再檢查伺服器、頻道與成員（見[共同選項與檢查](#共同選項與檢查)）。
3. Bot 在自己的行程裡，用和 slash 指令**相同的程式**執行，結果寫到 `done/<id>.json`；
   沒有執行的請求連同原因寫到 `rejected/<id>.json`。
4. CLI 一直在等這個結果，讀到後印出來並以對應的結束碼結束。

同時最多執行 4 個請求，所以一個跑十分鐘的 `ask` 不會卡住快速的查詢。

**CLI 本身不會在任何頻道發訊息。** `ask`、`summary`、`help` 的結果只回到 CLI。會在頻道裡出現的
只有功能本身之後的動作：用 `remind add` 設的提醒到時會照常發出並 @ 對象；`track add` 的追蹤有
新內容時會照常通知；`ask` 的回答裡如果有動作標籤（設提醒、追蹤、寫記憶），會像成員自己在
`--channel` 問的一樣生效。`summary` 和 `/summary` 一樣不帶頻道執行：回答裡的記憶標籤、取消提醒，
以及修改或取消既有追蹤的標籤會生效，但新的提醒與新的追蹤不會設下。

## 怎麼執行

CLI 要在 Bot 的 container 裡執行（它要讀同一個 `CODEX_HOME`，且必須是同一個 uid）。container
名稱是 compose 的 `BOT_CONTAINER_NAME`（預設 `tommy_test`）。基本形式是
`docker exec <container> python -m discord_codex_bot.admin <指令> [<動作>] [選項]`，例如：

```console
$ docker exec tommy_test python -m discord_codex_bot.admin --help
$ docker exec tommy_test python -m discord_codex_bot.admin remind --help
$ docker exec tommy_test python -m discord_codex_bot.admin remind add --help
$ docker exec tommy_test python -m discord_codex_bot.admin bot status
```

從標準輸入讀內容（`--text -`、`--prompt -`、`--interest -`）時，`docker exec` 要加 `-i`，
否則讀到的是空的：

```console
$ docker exec -i tommy_test python -m discord_codex_bot.admin persona set --kind persona --text - < persona.md
```

送出後 CLI 會在標準錯誤印出請求的 id，然後等結果：

```text
已送出 3f2a9c0d1e4b5a6978c0d1e2f3a4b5c6，等 Bot 處理…
```

這個 id 之後可以用 `result` 再查（見[結果、狀態與結束碼](#結果狀態與結束碼)）。

## 找到 id

所有 `--guild`、`--channel`、`--user`、`--who` 都是 Discord 的數字 id（最多 20 位數）。

1. 在 Discord 開啟「使用者設定 → 進階 → 開發者模式」。
2. 伺服器 id：在左側伺服器圖示上按右鍵 →「複製伺服器 ID」。
3. 頻道或討論串 id：在頻道名稱上按右鍵 →「複製頻道 ID」。討論串也有自己的 id，可以直接用。
4. 使用者 id：在成員名字或頭像上按右鍵 →「複製使用者 ID」。

另外兩種編號不是 Discord id，而是 Bot 自己的：提醒編號（`remind list` 的 `#12`）與追蹤編號
（`track list` 的 `#7`）。

本文件的範例一律用下面這些假 id：

| 代表 | 範例 id |
| --- | --- |
| 伺服器 | `123456789012345678` |
| 頻道 | `234567890123456789` |
| 成員 | `345678901234567890` |
| 另一位成員 | `456789012345678901` |

## 共同選項與檢查

參數在 CLI 先檢查一次，Bot 接手後**再檢查一次**（也檢查手寫進收件匣的請求），然後才檢查和
Discord 有關的部分。任何一項不通過，請求就被拒絕，什麼都不執行。

| 選項 | 規則 |
| --- | --- |
| `--guild` | 必須在 `ALLOWED_GUILD_IDS` 內。成員的設定、記憶、提醒與追蹤都依伺服器分開存，所以作用於成員的指令一律要給。 |
| `--channel` | Bot 要找得到它（快取或 API），它必須**屬於 `--guild`**，並通過和 slash 指令相同的頻道開放檢查（`ALLOWED_CHANNEL_IDS`，討論串看它的上層頻道）。 |
| `--user` | 必須是 `--guild` 的**現任成員**。同時給了 `--channel` 時，這位成員還必須**看得到那個頻道**（Discord 的 View Channel 權限；私人討論串則要在討論串裡，或有 Manage Threads）。成員在 Discord 上用不了的地方，CLI 也不能以他的身分去做。 |
| `--who` | 每一位都必須是 `--guild` 的成員。 |

**已離開伺服器的成員**：只讀取、取消或改時間的指令——`remind list`、`remind cancel`、
`track list`、`track cancel`、`track every`、`track at`、`track log`——不要求 `--user` 還是成員。
成員離開後，他設的提醒和追蹤仍會照常發出，所以必須還能列出來、停掉。這些指令只會碰到
**`--user` 自己的、在 `--guild` 的**項目：打錯編號不會動到同一個人在別的伺服器的東西。
其他指令（設定新提醒、改模型、提問等）仍要求現任成員。

每個指令都有：

- `--json`：印出完整的 JSON 結果（見下節），方便給腳本用。
- `--timeout 秒數`：最多等多久，最多 3600。預設是這個指令在 Bot 的時限再加 30 秒
  （大部分 60+30=90 秒，`export`、`status` 150 秒，`ask`、`summary`、`track add` 630 秒），
  每個指令的 `--help` 會顯示實際預設值。
- `-h`／`--help`：說明。

參數錯誤（缺必填、格式不對、超過長度）在 CLI 就擋下，不會送出，結束碼 1。

## 指令一覽

| 指令 | 做什麼 | 對應 |
| --- | --- | --- |
| `remind add／list／cancel` | 以成員身分設定、列出、取消提醒 | `/remind` |
| `memory add／forget／list` | 個人或伺服器記憶 | `/remember`、`/forget`、`/memory` |
| `model get／set／clear` | 成員用的模型與推理強度 | `/model` |
| `style get／set／clear／persona` | 個人回覆風格、是否套用伺服器人設 | `/style` |
| `reset` | 忘掉成員在一個頻道的對話脈絡 | `/reset` |
| `stop` | 取消成員進行中的請求 | `/stop` |
| `export` | 把成員的個人記憶打包成 zip | `/export` |
| `status` | 成員會在 `/status` 看到的內容 | `/status` |
| `track add／list／cancel／every／at／log` | 社群追蹤 | `/track` 與 `<track>` 標籤 |
| `tracking outbox` | 還沒送出的追蹤通知 | （只有 CLI） |
| `linkclean get／set` | 伺服器的連結洗參數模式 | `/linkclean` |
| `embedfix get／set` | 伺服器的預覽修正開關 | `/embedfix` |
| `persona get／set／clear` | 所有伺服器共用的人設與預設輸出風格 | `/persona` |
| `summary` | 摘要頻道最近的訊息，只回到 CLI | `/summary` |
| `ask` | 以成員身分提問，答案只回到 CLI | `/codex` |
| `help` | 成員看到的說明 | `/help` |
| `bot status` | Bot 的系統狀態 | （只有 CLI） |
| `alerts list` | 告警狀態 | （只有 CLI） |
| `result`、`pending` | 查結果、列出排隊中與執行中的請求 | （CLI 本身） |

下面的範例省略 `docker exec tommy_test`；在 host 上執行時記得加在前面。

### remind：提醒

`remind add` 以 `--user` 的身分設定提醒，和他自己用 `/remind` 一樣：時間要在 365 天內，每人最多
20 個。和他既有的提醒頻道、內容與要 @ 的人都相同，且到期時間相差不到 1 分鐘時不會重複設定，
而是以「已經有一樣的提醒 #N」失敗（結束碼 1）。到時 Bot 在 `--channel` 發提醒並 @ `--user`；
給了 `--who` 則改 @ 那些成員（要含 `--user` 就一併列出）。

`--when` 是台北時間，接受：`30分鐘後`、`2小時後`、`3天後`、`明天 9:30`、`後天下午3點`、
`21:00`（今天，已過則明天）、`9/15 14:30`、`2026-10-01 08:00`。含空白的要加引號。

```console
$ python -m discord_codex_bot.admin remind add --guild 123456789012345678 --channel 234567890123456789 --user 345678901234567890 --when "明天 9:30" --text "交週報"
已送出 3f2a9c0d1e4b5a6978c0d1e2f3a4b5c6，等 Bot 處理…
⏰ 已設定 #12 10/18 09:30 伺服器 123456789012345678 頻道 234567890123456789 — 交週報（台北時間）

$ python -m discord_codex_bot.admin remind add --guild 123456789012345678 --channel 234567890123456789 --user 345678901234567890 --when 30分鐘後 --text 開會 --who 345678901234567890 456789012345678901

$ python -m discord_codex_bot.admin remind list --guild 123456789012345678 --user 345678901234567890
#12 10/18 09:30 伺服器 123456789012345678 頻道 234567890123456789 — 交週報

$ python -m discord_codex_bot.admin remind cancel --guild 123456789012345678 --user 345678901234567890 --id 12
已取消提醒 #12。
```

### memory：記憶

`--scope user` 是 `--user` 的個人記憶；`--scope guild` 是整個伺服器共用的記憶，不需要 `--user`。
和 `/remember` 存在同一個地方，Bot 之後回答時會參考。`memory forget` 用完整名稱或 `memory list`
括號裡的檔名（`.md`）指定；有好幾條同名時什麼都不刪，請改用檔名。

```console
$ python -m discord_codex_bot.admin memory add --scope user --guild 123456789012345678 --user 345678901234567890 --name 生日 --text 3月14日
$ docker exec -i tommy_test python -m discord_codex_bot.admin memory add --scope guild --guild 123456789012345678 --name 群規 --text - < rules.md
$ python -m discord_codex_bot.admin memory list --guild 123456789012345678 --user 345678901234567890
$ python -m discord_codex_bot.admin memory list --guild 123456789012345678 --scope guild
$ python -m discord_codex_bot.admin memory forget --scope user --guild 123456789012345678 --user 345678901234567890 --name 生日
```

### model、style、reset、stop、export、status：成員的設定

`model set` 和 `/model` 的選單走同一個檢查：只能選它提供的模型。
`--value` 寫模型（例如 `agy:gemini-3.8-flash`、`openrouter:<id>`，或配合 `--provider` 只寫 id），
只給 `--provider` 則改用它的預設模型，`--effort` 設預設推理強度（`low`／`medium`／`high`／
`xhigh`／`max` 之一，和 `/model` 一樣不對照模型檢查）；三者至少給一個。`--value` 的模型不合時
會被拒絕，並列出可選的模型；只給 `--provider` 而它沒有預設模型時，會被拒絕並請你改用 `--value`
指定。

```console
$ python -m discord_codex_bot.admin model get --guild 123456789012345678 --user 345678901234567890
目前（預設）：…

$ python -m discord_codex_bot.admin model set --guild 123456789012345678 --user 345678901234567890 --value agy:gemini-3.8-flash
$ python -m discord_codex_bot.admin model set --guild 123456789012345678 --user 345678901234567890 --effort high
$ python -m discord_codex_bot.admin model clear --guild 123456789012345678 --user 345678901234567890

$ python -m discord_codex_bot.admin style set --guild 123456789012345678 --user 345678901234567890 --text "回答盡量簡短，用條列"
$ python -m discord_codex_bot.admin style persona --guild 123456789012345678 --user 345678901234567890 --mode off
$ python -m discord_codex_bot.admin style clear --guild 123456789012345678 --user 345678901234567890

$ python -m discord_codex_bot.admin reset --guild 123456789012345678 --channel 234567890123456789 --user 345678901234567890
已清除這位成員在此頻道的對話脈絡。

$ python -m discord_codex_bot.admin stop --guild 123456789012345678 --user 345678901234567890
已取消 1 個進行中的請求。

$ python -m discord_codex_bot.admin status --guild 123456789012345678 --channel 234567890123456789 --user 345678901234567890
```

- `reset` 只影響那一個頻道；他的記憶與其他頻道的對話不受影響。
- `stop` 給 `--channel` 只取消那個頻道的（同 `/stop`）；不給則取消他在整個伺服器的。
- `status` 的「續接」是每個頻道各自的，不給 `--channel` 時那一行會提醒你加上。
- `export` 在有設定 `BACKUP_DIR` 且該目錄存在時，把 zip 寫到 `BACKUP_DIR/exports`
  （`.env.example` 設為 `/backups`，compose 把 host 的 `./backups` 掛在那裡，可以直接在 host 上
  取）；否則寫到 `codex_home` volume 裡的 `CODEX_HOME/exports`，要用 `docker cp` 拿出來。
  權限 0600，不會傳給成員。印出的是 container 內的路徑：

```console
$ python -m discord_codex_bot.admin export --guild 123456789012345678 --user 345678901234567890
已匯出（12 KB）：/backups/exports/memory-123456789012345678-345678901234567890-20261017-153000.zip
```

### track、tracking：社群追蹤

`track add` 以 `--user` 的身分新增追蹤，和 `/track`、`<track>` 標籤走同一條路：每人上限
（`TRACKING_MAX_PER_USER`）、provider 設定與網址檢查都一樣。`--source` 可以是 YouTube／Twitch
頻道網址、X 帳號網址（`https://x.com/<帳號>`）、露天賣場網址（可帶 `?q=關鍵字`）或任何 http(s)
網頁。判斷時間：`--at` 是每天固定時間（台灣時間），最多 6 個（多的會被忽略）；否則每 `--every` 分鐘，不給則用
`TRACKING_CLASSIFY_INTERVAL_MINUTES`（預設 60）。解析 X 帳號可能要幾分鐘，所以時限是 600 秒。

```console
$ python -m discord_codex_bot.admin track add --guild 123456789012345678 --channel 234567890123456789 --user 345678901234567890 --source https://www.youtube.com/@example --at 12:01,20:01
$ python -m discord_codex_bot.admin track add --guild 123456789012345678 --channel 234567890123456789 --user 345678901234567890 --source https://x.com/example --interest "新品發表或價格調整" --who 456789012345678901 --every 120

$ python -m discord_codex_bot.admin track list --guild 123456789012345678
社群追蹤 1 個：
#7 youtube · https://www.youtube.com/@example（每天 12:01、20:01 判斷）
　成員 345678901234567890 · 伺服器 123456789012345678 · 頻道 234567890123456789

$ python -m discord_codex_bot.admin track every --guild 123456789012345678 --user 345678901234567890 --id 7 --minutes 30
追蹤 #7 改成每 30 分鐘判斷一次。

$ python -m discord_codex_bot.admin track at --guild 123456789012345678 --user 345678901234567890 --id 7 --times 09:00,21:00
$ python -m discord_codex_bot.admin track log --guild 123456789012345678 --user 345678901234567890 --id 7 --limit 5
$ python -m discord_codex_bot.admin track cancel --guild 123456789012345678 --user 345678901234567890 --id 7
已取消追蹤 #7。
```

`tracking outbox` 列出已經判斷要通知、但還沒成功發到頻道的通知；送出失敗的會留著，每輪追蹤都
重試。預設只列 `ALLOWED_GUILD_IDS` 內伺服器的；`--guild` 只列一個，`--all-guilds` 連已經移出
允許清單的伺服器也列（兩者擇一）。

```console
$ python -m discord_codex_bot.admin tracking outbox --failed
待送的追蹤通知 3 則，其中 1 則送出失敗過（每輪追蹤都會重試）。
#41 送出失敗 2 次 · 追蹤 #7 · 成員 345678901234567890 · 伺服器 123456789012345678 · 頻道 234567890123456789
　新影片標題
　https://www.youtube.com/watch?v=…

$ python -m discord_codex_bot.admin tracking outbox --all-guilds --limit 100
```

### linkclean、embedfix、persona：伺服器與人設

`linkclean set` 和 `embedfix set` 只影響之後成員在那個伺服器貼的連結；預覽修正隨連結洗參數
一起投遞，連結洗參數為 `off` 時不會有作用。

```console
$ python -m discord_codex_bot.admin linkclean get --guild 123456789012345678
連結洗參數：全部清洗

$ python -m discord_codex_bot.admin linkclean set --guild 123456789012345678 --mode links
$ python -m discord_codex_bot.admin embedfix set --guild 123456789012345678 --state on
```

人設（`persona`）與預設輸出風格（`style`）是**所有伺服器共用的**。`persona set`／`persona clear`
和 `/persona` 一樣：有設定 `BACKUP_DIR` 時，先把被取代的版本備份到 `BACKUP_DIR/instructions/`
（沒設定就不備份）；接著重建 Codex 的工作目錄，並讓所有進行中的對話串重新開始。`persona clear`
只有真的還原了東西才會這樣做：那一份本來就是 image 預設（沒有上傳過）時，什麼都不變。
內容的檢查和 `/persona` 上傳 `.md` 檔相同（最多 20000 字）。

```console
$ python -m discord_codex_bot.admin persona get
$ docker exec tommy_test python -m discord_codex_bot.admin persona get --kind persona > persona.md
$ docker exec -i tommy_test python -m discord_codex_bot.admin persona set --kind persona --text - < persona.md
$ python -m discord_codex_bot.admin persona clear --kind style
```

### ask、summary：以成員身分回答

兩者都以 `--user` 的身分執行：用他的模型、風格與記憶，算是他進行中的請求（他的 `/stop` 或 ❌
可以取消它，CLI 會收到「已取消」的失敗）。`--user` 必須是現任成員，且看得到 `--channel`。

> **注意（`ask`）**：回答裡的動作標籤照樣生效，就像成員自己問的一樣——會寫進他的記憶，設下的
> 提醒與追蹤之後會在頻道裡通知他。要只看答案而不留下任何東西，問題裡就不要叫 Bot 做這些事。

`ask` 接續他在 `--channel` 的對話（`--new` 則從頭開始），之後他在頻道裡也會接著這段對話。
答案不貼到頻道；回答產生的檔案不會附上：文字輸出只說有幾個，檔名在 `--json` 的 `files` 裡。

```console
$ python -m discord_codex_bot.admin ask --guild 123456789012345678 --channel 234567890123456789 --user 345678901234567890 --prompt "幫我整理今天討論的結論"
已送出 9a0b1c2d3e4f5a6b7c8d9e0f1a2b3c4d，等 Bot 處理…
今天的結論是……

（回答模型：…；接續先前的對話）

$ docker exec -i tommy_test python -m discord_codex_bot.admin ask --guild 123456789012345678 --channel 234567890123456789 --user 345678901234567890 --new --effort high --prompt - < question.txt
```

`summary` 和 `/summary` 相同，但不貼到頻道。預設最近 100 則；`--count`（5–500）與 `--hours`
（1–168）擇一，`--focus` 寫特別想知道什麼。摘要是一次獨立的回答，不會變成成員的對話脈絡。

> **注意（`summary`）**：和 `/summary` 一樣，摘要不帶頻道執行。回答裡的記憶標籤、取消提醒，以及
> 修改或取消既有追蹤的標籤會生效；新的提醒與新的追蹤不會設下。

```console
$ python -m discord_codex_bot.admin summary --guild 123456789012345678 --channel 234567890123456789 --user 345678901234567890
$ python -m discord_codex_bot.admin summary --guild 123456789012345678 --channel 234567890123456789 --user 345678901234567890 --hours 24 --focus "有哪些待辦"
```

### help、bot status、alerts list

```console
$ python -m discord_codex_bot.admin help
$ python -m discord_codex_bot.admin help --sheet
$ python -m discord_codex_bot.admin bot status
Bot：已連線 · 延遲 52 ms · 啟動於 2026-10-17 08:00
進行中：成員請求 0 · Codex 佇列 0 · 維運指令 1（含這一個）
Codex：… · 額度 5h 12% / 7d 40%
…
背景工作：
　附件清理：執行中
　…

$ python -m discord_codex_bot.admin bot status --json
$ python -m discord_codex_bot.admin alerts list
```

`help --sheet` 是注入給模型的精簡版（`<HELP>` 區塊）。`bot status` 只有執行中的 Bot 知道：額度與
暫停標記、最近一次備援、自動挑選的路由表、每個背景工作是否還活著（⚠️ 表示該看一下）。
`alerts list` 列出告警私訊給誰、各後端連續失敗幾次、哪些在等恢復通知，以及這次啟動後送出過的告警。

## 結果、狀態與結束碼

| 結束碼 | 意思 |
| --- | --- |
| 0 | 完成（done，ok） |
| 1 | 執行失敗（done，不 ok）、被拒絕（rejected）、參數錯誤，或 `result` 找不到這個 id |
| 2 | 時間內沒有結果：Bot 還沒接手的已撤回（withdrawn）；已在執行的（running）之後用 `result` 查 |

完成時結果印到**標準輸出**；失敗、被拒絕、撤回或還在執行的說明印到**標準錯誤**。
`--json` 時，Bot 寫下的結果（以及 withdrawn、running）印到標準輸出，`status` 是下列之一。例外：
送出前的錯誤（參數不合、目錄權限不對）一律是印到標準錯誤的純文字；`result <id> --json` 遇到還在
排隊的 id（結束碼 2）或找不到的 id（結束碼 1），也是印到標準錯誤的純文字。

- **`done`、`ok: true`**：執行完成，`result` 是指令的完整結果（文字在 `result.text`）。
- **`done`、`ok: false`**：執行了但失敗，原因在 `error`，例如「找不到這位成員在這個伺服器的提醒 #12。」、
  「超過 600 秒，已中止；可能只做了一部分」、「已取消：成員用 /stop 或 ❌ 取消了這個請求」。
  兩種特別的情況要**去確認實際結果**，因為可能已經做了一部分：
  - 「Bot 在執行途中重啟；不會再執行，但可能已經做了一部分，請確認結果（interrupted while running）」
  - 「Bot 無法記錄這個請求的結果；不會再執行，但它可能已經執行，請確認結果（the result could not be recorded; it may have run）」
- **`rejected`**：**什麼都沒執行**，原因在 `reason`。常見的有：伺服器不在允許清單、頻道不屬於伺服器、
  頻道未開放、使用者不是成員或看不到頻道、參數不合、
  「請求的建立時間離現在超過兩小時，已經沒人在等，不執行」、
  「這個 id 已經有結果；同一個請求不會執行兩次」、
  「Bot 在開始執行前重啟；沒有執行，也不會再執行（interrupted before it ran）」、
  「無法向 Discord 確認伺服器、頻道或成員（…），沒有執行」。
- **`withdrawn`**：Bot 沒在時限內接手，CLI 已經把請求從收件匣拿掉，什麼都沒執行。
- **`running`**：Bot 已經接手、還在執行；它會跑完，結果之後用 `result` 查。

```console
$ python -m discord_codex_bot.admin result 3f2a9c0d1e4b5a6978c0d1e2f3a4b5c6
$ python -m discord_codex_bot.admin result 3f2a9c0d1e4b5a6978c0d1e2f3a4b5c6 --json
{
  "status": "done",
  "id": "3f2a9c0d1e4b5a6978c0d1e2f3a4b5c6",
  "command": "remind.cancel",
  "ok": true,
  "result": {
    "text": "已取消提醒 #12。",
    "id": 12
  },
  "error": null,
  "finished": 1792222200.0
}

$ python -m discord_codex_bot.admin pending
3f2a9c0d1e4b5a6978c0d1e2f3a4b5c6  執行中  ask  2026-10-17 15:30  1000
```

`result` 的結束碼：0 完成；1 失敗、被拒絕或找不到；2 還在排隊或執行中。結果保留 7 天。
`pending` 列出 `inbox/`（排隊中）與 `processing/`（執行中）的請求：id、狀態、指令、送出時間
（台北時間）與送出的系統使用者（請求裡的 `by`：使用者名稱，container 裡沒有這個 uid 的名稱時
是 uid，例如 Bot 的 container 裡是 `1000`）。

**每個請求最多執行一次**：同一個 id 已經有結果就不再執行；Bot 重啟時留在 `processing/` 的請求
不會重跑（還沒開始的記為 rejected，已經開始的記為 done／失敗「可能已經做了一部分」）；
超過兩小時的請求不執行。所以「沒有結果」時不要假設它沒做——先用 `result`／`pending` 查，
再決定要不要重送。

## 逾時、Ctrl-C 與撤回

- 時限到，或 container 裡的 CLI 收到 SIGINT、SIGTERM、SIGHUP（例如在 container 裡對它送訊號、
  加了 `-t` 時在終端機按 Ctrl-C 或關掉終端機）時，CLI 會試著把請求從 `inbox/` 拿掉。成功就是
  `withdrawn`：**什麼都沒執行，之後也不會執行**。
- **`docker exec` 不會轉送訊號。** 沒加 `-t` 時（本文件的範例都沒加；從檔案用 `<` 餵內容時也
  不能加），按 Ctrl-C 或砍掉 `docker exec` 只會停掉 host 上的 client，container 裡的 CLI 會繼續
  等到 `--timeout` 為止。而 Bot 通常 2 秒內就接手，接手後就撤不回來了；想確定請求沒有被執行，
  只能在 Bot 接手前撤回，事後用 `pending`／`result` 確認實際狀況。
- 如果 Bot 已經接手（請求已在 `processing/`），就撤不回來了：它會照常跑完，結果寫到 `done/`，
  CLI 印出「仍在執行中」並以 2 結束，之後用 `result <id>` 查。
- 在送出之前就收到中斷，CLI 不寫任何請求，結束碼 1。
- Bot 給每個指令有自己的時限（預設 60 秒；`export`、`status` 120 秒；`ask`、`summary`、
  `track add` 600 秒）；超過就中止，記為失敗「可能只做了一部分」。

## 收件匣、權限與安全邊界

收件匣在 `CODEX_HOME/admin`（compose 裡是 `/var/lib/codex/admin`，位在 `codex_home` volume），
裡面有 `inbox/`、`processing/`、`done/`、`rejected/`。

- Bot 啟動時建立這些目錄並設為 0700（已存在也 chmod 回 0700）；請求與結果檔是 0600，
  以暫存檔寫完、fsync 後再改名，讀的一方不會看到寫一半的檔案。
- 只有擁有者（container 使用者 uid 1000）和 root 寫得進去。模型寫不進去：Codex 以唯讀
  sandbox 執行，agy 禁止寫入。
- CLI 在目錄不屬於自己的 uid，或群組／其他人可寫時**拒絕執行**；Bot 在這種狀態下暫停讀收件匣
  （log 記一次 `Admin inbox refused`）。Bot 也不讀符號連結、不是一般檔案、不屬於 Bot 的 uid，
  或超過 256 KB 的請求。
- 這個目錄**故意不進每晚的備份**：還原的收件匣不能讓舊請求再跑一次。
- 請求只帶參數，不能帶程式碼；每個指令都走和 slash 指令相同的檢查（允許清單、頻道、成員與
  看得到頻道），不會以成員身分做他在 Discord 上做不到的事。拒絕原因與失敗訊息會寫進 Bot 的
  log，所以不會引用成員的文字。
- 請求裡的 `by` 是送出者的系統使用者名稱；container 裡沒有這個 uid 的名稱時是 uid
  （Bot 的 container 沒有 uid 1000 的 passwd 項目，所以是 `1000`）。只用於 `pending` 與紀錄。

## 疑難排解

**一直等到逾時，結果是 withdrawn（Bot 沒有接手）**

- Bot 有在跑嗎？`docker ps`、`docker logs <container>`；`bot status` 本身也要 Bot 在跑才會有結果。
- Bot 還沒就緒（剛啟動，還沒連上 Discord）時不讀收件匣，等它連上後再試。
- 在 Bot 的 log 找 `Admin inbox refused`（目錄權限不對）或
  `Admin inbox … unusable; operator commands are off`（啟動時無法建立目錄，整個功能關閉，要重啟 Bot）。
- 同時已有 4 個請求在跑（例如幾個長的 `ask`）時，新的會排隊；用 `pending` 看。
- 是不是在另一個 container 裡執行？CLI 要在 Bot 自己的 container 裡跑，才是同一個 `CODEX_HOME`。

**「拒絕執行：… 不存在；Bot 啟動時會建立它（Bot 有在跑嗎？）」**

Bot 從來沒有在這個 `CODEX_HOME` 啟動過，或 CLI 不是在 Bot 的 container 裡執行。

**「拒絕執行：… 屬於 uid …，不是目前的 uid …」**

用了不同的使用者執行（例如 `docker exec -u root`）。用 container 的預設使用者（uid 1000）執行，
不要加 `-u`。

**「拒絕執行：… 的權限是 drwxrwxr-x，群組或其他人可寫；請 chmod 700 後重啟 Bot」**

有人改了目錄權限。在 container 裡 `chmod 700` 那個目錄後重啟 Bot（Bot 啟動時也會自己改回 0700）。

**被拒絕：「此頻道目前未開放 Codex。」／「頻道 … 不屬於伺服器 …」／「使用者 … 看不到頻道 …」**

頻道不在 `ALLOWED_CHANNEL_IDS`（或它的上層頻道不在）、id 貼錯伺服器，或那位成員在 Discord 上
本來就看不到那個頻道。換成他看得到、而且 Bot 有開放的頻道。

**被拒絕：「使用者 … 不是伺服器 … 的成員。」**

成員已經離開。設定新東西不行；要列出或停掉他留下的提醒與追蹤，用 `remind list／cancel`、
`track list／cancel` 等不要求現任成員的指令。

**`--text -` 讀到空的：「沒有送出：--text 不可空白」（結束碼 1）**

`docker exec` 沒加 `-i`，標準輸入沒有接上。CLI 在送出前就擋下，什麼都沒寫進收件匣，`rejected/`
裡也不會有紀錄。

**結果是「仍在執行中」**

Bot 已經接手，請求會跑完。稍後用 `result <id>` 查；不要重送，否則會做兩次。

## 其他維運指令

這些各自獨立，不經過這個收件匣：`python -m discord_codex_bot.announce`（公告：send、queue、
list）、`discord_codex_bot.emoji`（表情符號描述）、`discord_codex_bot.backup`（立即備份）、
`discord_codex_bot.harvest`、`discord_codex_bot.digest`、`discord_codex_bot.consolidate`
（記憶整理，可加 `--force`）。
