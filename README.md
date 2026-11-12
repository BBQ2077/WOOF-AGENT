# WOOF

個人 AI agent 桌面程式。核心很小，模型、工具、記憶都是 `plugins/` 底下的插件。

## 開啟

雙擊 `WOOF.vbs`。關掉視窗會縮到系統匣；要完全結束，在系統匣圖示按右鍵 →「結束 WOOF」，後端和它開的所有程序會一起關掉。

## 第一次安裝

```bash
# Python 後端
uv sync                                  # 依 pyproject.toml 建 .venv 並安裝相依套件

# Electron 外殼
cd desktop
npm install
node node_modules/electron/install.js   # 新版 npm 不會自動跑這步，Electron 本體要手動下載
```

## 設定檔

| 檔案 | 內容 |
|---|---|
| `config.toml` | 模型商、模型、工作目錄、允許預覽的資料夾、權限 |
| `keys.toml` | API key；同一個金鑰池可放多把，額度用完或被擋會自動換下一把 |
| `plugins/<名稱>/plugin.toml` | 插件說明和設定欄位，介面的設定表單由這裡自動產生 |

平常直接在「設定」頁改，按「儲存變更」生效。手動改檔後到「插件」頁按「重新載入」。外觀（顏色、縮放、字級）存在介面本機，不寫進設定檔。

### 模型商與模型

模型商放連線方式和共用欄位，模型只放自己的名稱與能力；模型自己寫的同名欄位會蓋過模型商的。

```toml
[[vendors]]
id = "openrouter"                  # 代號，模型用 vendor 對應
label = "OpenRouter"               # 介面顯示名稱
provider = "openai_compat"         # 用哪個插件：openai_compat / anthropic / claude_cli
base_url = "https://openrouter.ai/api/v1"
key_pool = "openrouter"            # 對應 keys.toml 的 [[openrouter]]
effort_style = "openrouter"        # 思考強度參數格式：none / openai / deepseek / openrouter
rate_limit_wait = 120              # 被 429 限流時最多等幾秒重試

[[models]]
id = "gemma4-31b-free"             # 內部代號
vendor = "openrouter"
label = "Gemma 4 31B（免費）"
model = "google/gemma-4-31b-it:free"
efforts = []                       # 介面上可選的思考強度
vision = true
context_window = 262144
price = [0.5, 1.5, 0.05]           # 選填：美元／每百萬 token，輸入 / 輸出 / 快取讀取；用量面板據此算金額
```

右側面板「額度 → 用量」可設每日、每月預算，超過時可選只提醒、暫停送出，或改用「模型用途 → 備用模型」。沒填單價的模型不算錢。

主要模型出錯（限流、額度用完、連不上，服務商自己的重試都用完）時，如果設了備用模型，這一回合改用它接著做，做到一半也會接手，前面的工具結果都保留。

## 連接 Claude

WOOF 不提供 Claude 帳號登入，有兩種用法：

| 方式 | 設定 | 計費 |
|---|---|---|
| Anthropic API | `provider = "anthropic"`，`keys.toml` 放 Claude Console 的 API key | 依 API 用量計費 |
| 本機 Claude Code | `provider = "claude_cli"`，WOOF 呼叫你電腦上原版的 `claude` 指令 | 依你在 Claude Code 裡登入的方式；WOOF 不讀、不存任何 Claude 憑證 |

用本機 Claude Code 前，先自己安裝並執行 `claude auth login`。

## 介面語言

「設定 → 外觀 → 介面語言」可切換繁體中文與 English。切換後，模型的基礎提示詞與回覆語言會一起改；進行中的對話會在下一則訊息附上語言變更，不重組系統提示詞。

## 安全預設值

WOOF 讓模型在你的電腦上讀寫檔案、執行指令。下面是沒改設定時的行為：

| 項目 | 預設 |
|---|---|
| 後端連線 | 只聽 `127.0.0.1`，埠號隨機，每次啟動產生新的存取 token，沒有 token 的請求一律拒絕 |
| API key | `keys.toml` 用 Windows DPAPI 加密，只有同一台電腦、同一個 Windows 帳號解得開 |
| 工具輸出 | 送進模型、存進對話、顯示在畫面前，會遮掉 `keys.toml` 裡的 key 和常見格式的金鑰（`redact_secrets = false` 可關） |
| 新對話的權限 | 「先問我」：讀檔、搜尋、連網直接做；寫檔、執行指令、子代理、MCP 工具先問你 |
| 危險指令 | 在會自動執行的模式裡仍先問（`rm -rf`、`git push --force`、格式化等）；刪整顆磁碟、fork bomb 等任何模式都擋 |
| 排程的對話 | 沒人能按允許，要先問的工具直接擋下 |
| 自動審核 | 預設關閉。在「設定 → 模型用途 → 自動審核」指定模型後，要先問你的動作會先交給它判斷，判定安全就放行並在對話裡留一行紀錄；送過去的只有工具名稱、參數和工作目錄。危險指令一律問你 |
| 寫入型子代理 | 只能改主代理指定範圍內的檔案；可以跑 terminal 驗證，但工作目錄限在範圍內、不能開背景指令 |
| 檔案還原 | 模型用 `write_file`／`patch` 改檔前會先備份，可還原到任一則訊息之前；終端機指令改的檔追不到 |

使用前要知道的事：

- 讀檔沒有限制資料夾，模型讀得到你這個 Windows 帳號讀得到的所有檔案。
- 對話紀錄存在 `data/woof.db`，沒有加密。
- 網頁搜尋與擷取透過 Firecrawl、Tavily 的雲端服務，查詢字串和網址會送給它們；擷取在它們的伺服器上進行，連不到你的區域網路。
- 你傳的訊息、圖片和工具結果會送給你選的模型商。
- 權限可在「設定 → 權限」或 `config.toml` 的 `[permissions]` 調整（範例見 `config.example.toml`）。改成「全自動」後，模型不經確認就能寫檔和執行指令。

## 新增插件

複製 `plugins/_template`，改資料夾名稱和 `plugin.toml`，寫 `__init__.py` 的 `register(woof)`，到介面「插件」頁按「重新載入」。

## 結構

```
woof/          核心：設定、資料庫、程序管理、插件載入、工具迴圈、伺服器
plugins/       所有功能
desktop/       Electron 外殼（main.js、preload.js）
web/dist/      介面（index.html、app.js、app.css）
data/          對話資料庫、日誌、上傳的圖片
tests/         測試腳本（`.venv/Scripts/python tests/unit_test.py` 跑單元測試，用暫存資料夾，不碰你的對話）
```


## 內建插件

| 插件 | 做什麼 | 設定 |
|---|---|---|
| `web_search` | 網路搜尋與網頁擷取，Firecrawl 與 Tavily 多把 key 輪換 | `keys.toml` 的 `[[firecrawl]]`、`[[tavily]]`；用完的 key 標到下個月 1 日自動恢復 |
| `context` | 上下文用量超過「壓縮門檻」（預設 70%）時，先把較早、用過的工具輸出換成一行佔位；這樣還不夠才把較早的訊息寫成摘要，之後只送摘要加最近幾輪 | 插件設定可改壓縮門檻與保留輪數 |
| `vision` | 讓 agent 看本機圖片檔（`vision_analyze`） | 空白則用目前對話的模型，不支援看圖時改用清單裡第一個支援的 |
| `mcp_client` | 連接 MCP 伺服器，對方的工具以 `mcp_<名稱>_<工具>` 提供 | 設定每行一個：`名稱 \| command 參數` 或 `名稱 \| http://位址` |

MCP 連線在第一次對話時建立，插件重載或後端結束時關閉。stdio 子程序由 SDK 放進 Job Object，後端被殺會一起結束。

## 授權

MIT，見 `LICENSE`。`web/dist/vendor/` 裡的第三方套件各自的授權見 `web/dist/vendor/THIRD_PARTY.md`。
