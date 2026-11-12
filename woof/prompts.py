"""WOOF 自己寫給模型的內部提示詞，全部集中在這裡。

每段有兩種寫法，由設定 [prompts] mode 決定：
  lean     精簡：只留必要指令
  normal   常規：判準說清楚，但不冗長（預設）
使用者可以在「設定 → 提示詞與記憶 → 內部提示詞」逐段檢視、覆寫，覆寫存在 data/prompt_overrides.json
（覆寫兩種模式都用同一段文字）。{名稱} 是送出時代入的值。
主模型的系統提示詞（基礎提示＋插件片段）不在這裡，看「上下文 → 這個對話的總提示詞」。
"""
from __future__ import annotations

import json
import string
from pathlib import Path

from .config import DATA

MODES = ("lean", "normal")
MODE_LABEL = {"lean": "精簡", "normal": "常規"}
GROUPS = ("流程", "接續", "壓縮", "其他")

CATALOG: dict[str, dict] = {}


def _e(key, group, title, when, lean, normal, vars=""):
    CATALOG[key] = {"key": key, "group": group, "title": title, "when": when,
                    "lean": lean, "normal": normal, "vars": [x.strip() for x in vars.split(",") if x.strip()]}


_e("subagent", "流程", "子代理的系統提示", "派子代理（delegate_task、審核員）時，接在子代理系統提示後面",
   "你是子代理，只做下面這一個任務。用工具查證，最後一則回覆寫完整結果：做了什麼、結果、檔案路徑、問題。主代理只看得到這則。",
   "你是被主代理派出去的子代理，只負責下面這一個任務。自己用工具查證、完成後，最後一則回覆要是完整的結果報告（做了什麼、結果、檔案路徑、遇到的問題），主代理只看得到這則。"),
_e("subagent_ctx", "流程", "子代理的背景資料標頭", "派子代理時有附背景資料，接在任務後面",
   "\n\n【背景資料】\n{context}",
   "\n\n【背景資料】\n{context}", vars="context"),
_e("plan_goal", "流程", "目標規劃員", "/goal 開始時，請規劃員把目標拆成步驟（系統提示）",
   "你是規劃員。把目標拆成 3 到 8 個可逐一檢查的步驟。只輸出 JSON 陣列，例如 [\"步驟一\", \"步驟二\"]。",
   "你是任務規劃員。把使用者的目標拆成 3 到 8 個具體、可以逐一檢查是否完成的步驟。只輸出 JSON 陣列，例如 [\"步驟一\", \"步驟二\"]，不要其他文字。"),
_e("plan_redo", "流程", "規劃被打回", "方案審核不通過時，附在目標後面請規劃員重寫",
   "\n\n【上一版方案被打回】{feedback}\n請修正後重新提出步驟，仍然只輸出 JSON 陣列。",
   "\n\n【上一版方案被打回】{feedback}\n請修正後重新提出步驟，仍然只輸出 JSON 陣列。", vars="feedback"),
_e("goal_step", "流程", "目標的執行步驟", "/goal 每一步交給主模型時（內部訊息）",
   "【目標】{objective}\n【步驟 {n}/{total}】{step}\n完成時最後一則回覆以「完成：」開頭，寫做了什麼與證據。",
   "【目標】{objective}\n【步驟 {n}/{total}】{step}\n請完成這一步。完成時最後一則回覆以「完成：」開頭，說明做了什麼與證據（檔案路徑、結果、數字）。", vars="objective, n, total, step"),
_e("review_fail", "流程", "步驟審核不通過", "/goal 某一步被審核員打回時（內部訊息）",
   "【審核未通過】{reason}\n補做這一步，完成後以「完成：」開頭宣告。",
   "【審核未通過】{reason}\n請補做這一步，完成後再以「完成：」開頭宣告。", vars="reason"),
_e("review", "流程", "步驟審核員（對抗審核）", "/goal 每一步完成後、/review、改動審核：交給審核子代理的開頭",
   "你是審核員，只判斷這一步是否實質完成。有證據（工具輸出、路徑、數值）就算完成；只有明顯沒做、做錯，或宣稱完成卻沒證據才判不通過。最後一行只能是 JSON：{\"pass\": true 或 false, \"reason\": \"一句話\"}",
   "你是任務審核員，只判斷「這一步是否實質完成」。\n判準：\n- 步驟要求的結果確實有了（檔案存在、內容符合、指令真的跑過且有結果等），有證據（工具輸出、路徑、具體數值）就算完成。\n- 允許措辭、格式、排版與小瑕疵不同；不要因為不完美、或你會寫得不一樣就不通過。\n- 只有明顯沒做、做錯，或宣稱完成卻拿不出證據時，才判不通過。\n必要時自己用工具查證（讀檔、看結果）。最後一行只能是 JSON：{\"pass\": true 或 false, \"reason\": \"一句話說明\"}"),
_e("review_body", "流程", "審核的資料格式", "接在審核員提示後面，放要審的步驟和執行者的宣告",
   "\n\n【要審核的步驟】\n{step}\n\n【執行者的完成宣告】\n{report}",
   "\n\n【要審核的步驟】\n{step}\n\n【執行者的完成宣告】\n{report}", vars="step, report"),
_e("plan_mode", "流程", "預案模式", "開著「預案模式」時，接在主模型系統提示後面",
   "【預案模式】只讀資料、分析，輸出一份預案：目標、步驟清單、會動到的檔案或指令、風險與預案。本回合不改檔案、不跑指令。",
   "【預案模式】先讀取需要的資料、分析，然後只輸出一份預案：目標、步驟清單（每步可檢查）、會動到的檔案或指令、風險與預案。本回合不要修改檔案、不要執行指令；使用者同意後才會執行。"),
_e("plan_review", "流程", "方案審核員（對抗審核）", "預案模式產生方案後，交給方案審核子代理",
   "你是方案審核員。只擋三種情況：漏了達成目標必要的步驟、步驟順序會出錯、會造成不可逆損害卻沒有防護。措辭、粒度、細節不要挑。最後一行只能是 JSON：{\"pass\": true 或 false, \"reason\": \"一句話\"}",
   "你是方案審核員，只判斷這份方案能不能達成目標。\n判準：\n- 只有以下情況才打回：漏了達成目標必要的步驟、步驟順序會出錯、步驟之間互相矛盾、會造成不可逆的損害卻沒有防護。\n- 步驟寫法、粒度、措辭、要不要多一兩步細節，都不算問題，不要吹毛求疵。\n- 打回時 reason 要具體指出缺什麼或哪裡錯，方便提案者修正。\n最後一行只能是 JSON：{\"pass\": true 或 false, \"reason\": \"一句話說明\"}"),
_e("plan_review_body", "流程", "方案審核的資料格式", "接在方案審核員提示後面",
   "\n\n【目標】\n{objective}\n\n【要審核的方案】\n{plan}",
   "\n\n【目標】\n{objective}\n\n【要審核的方案】\n{plan}", vars="objective, plan"),
_e("repeat_fail", "工具", "原地打轉擋下", "同一個工具、同樣參數在這一回合已經失敗 2 次，第 3 次起不再執行，改回這段",
   "{name} 用同樣的參數已經失敗 {n} 次，這次沒有執行。換個做法，或跟使用者說卡在哪。",
   "{name} 用同樣的參數在這一回合已經失敗 {n} 次，這次沒有再執行。重試不會有不同結果：換個做法（改參數、先讀檔確認內容、換工具），或直接跟使用者說明卡在哪。", vars="name, n"),
_e("steps_low", "接續", "步數快用完", "單次回合剩 5 步時附上，請模型收尾",
   "這一回合只剩 {left} 步（上限 {total}），先收尾。",
   "這一回合只剩 {left} 步（上限 {total} 步）。把手上這件做到可以停的地方，然後回報做了什麼、還剩什麼；剩下的等使用者按「繼續」再做。", vars="left, total"),
_e("settings_note", "接續", "對話設定變更", "使用者改了這個對話的權限或輸出風格，附在下一則訊息最後（不重組系統提示詞，前綴快取不受影響）。有編號和時間，再改時模型看得出哪則比較新",
   "【設定變更 #{n}・{when}・WOOF 自動附上】從這則起適用，取代之前所有的設定變更：\n{lines}",
   "【設定變更 #{n}・{when}・WOOF 自動附上】使用者剛改了這個對話的設定，從這則訊息起適用；之前的設定變更、之前被擋下的紀錄都以這則為準：\n{lines}", vars="n, when, lines"),
_e("model_identity", "系統", "目前的模型", "系統提示詞「系統」段的第一行：WOOF 實際連的模型。模型被問到自己是誰時常答錯訓練資料裡的舊名稱，以這行為準",
   "你是 {label}（模型商：{vendor}，API 模型名稱：{wire}），在 WOOF 裡執行；問到自己是什麼模型以這行為準。",
   "你是 {label}（模型商：{vendor}，API 模型名稱：{wire}），在 WOOF 裡執行。被問到是什麼模型時照這行回答，不要用訓練資料裡記得的名稱。", vars="label, vendor, wire"),
_e("model_switch", "接續", "換模型", "對話中途換模型時，設定變更裡的一行",
   "這個對話從這則起改由 {label}（{wire}）回答，你就是它；之前的回覆是 {prev} 寫的。",
   "這個對話從這則起改由 {label}（API 模型名稱 {wire}）回答，你就是 {label}；之前的助理回覆是 {prev} 寫的，系統提示詞裡的模型名稱以這則為準。", vars="label, wire, prev"),
_e("perm_freed", "接續", "權限放寬", "設定變更裡，原本被擋或要確認、現在可以直接做的類別",
   "之前被擋的「{cats}」現在可以直接用，需要的話直接重做。",
   "之前被擋下或要確認的「{cats}」現在可以直接用。前面因權限沒做成的步驟，需要的話直接重做，不用再問。", vars="cats"),
_e("focus_note", "接續", "重點便條", "多步工作時，每次送模型前附在最後一則的最後面；只在這次請求裡，不存進對話、不進快取前綴；上限見 focus 插件設定",
   "【重點便條・WOOF 自動更新・{when}】\n{lines}",
   "【重點便條・WOOF 每步自動更新，只反映此刻狀態，不是使用者說的話・{when}】\n{lines}", vars="when, lines"),
_e("lang_switch", "接續", "回覆語言變更", "使用者在對話中途切換介面語言時，設定變更裡的一行",
   "之後改用{lang}回覆。",
   "使用者把介面語言改了，之後的回覆改用{lang}。", vars="lang"),
_e("style_note", "接續", "輸出風格變更", "設定變更裡的風格那一行",
   "輸出風格改成「{name}」：{text}",
   "輸出風格改成「{name}」，之後的回覆照這個要求：{text}", vars="name, text"),
_e("style_reset", "接續", "輸出風格改回預設", "設定變更裡的風格那一行（改回預設）",
   "輸出風格改回預設，之前的風格要求不用再照做。",
   "輸出風格改回預設，之前指定的風格要求不用再照做。"),
_e("internal_head", "接續", "內部訊息的標頭", "每則內部訊息送給模型時加在最前面，讓模型分得出不是使用者說的",
   "【內部指令・{label}】WOOF 自動發出，不是使用者說的。\n",
   "【內部指令・{label}】這是 WOOF 流程自動發出的，不是使用者本人說的話。使用者真正的要求只看沒有這個標記的訊息。\n", vars="label"),
_e("truncated", "接續", "輸出被截斷", "回覆因為輸出上限被截斷（continuation 插件）",
   "回覆被截斷了。從斷掉的地方接著寫，不要重複、不要重新開頭。",
   "你上一則回覆因為輸出長度上限被截斷了。請從斷掉的地方直接接著寫，不要重複已經寫過的內容，也不要重新開頭或加前言。"),
_e("think_out", "接續", "思考用完輸出額度", "思考把整個輸出額度用完、正文還沒開始",
   "上一步的思考用完了輸出額度。少想一點，直接寫正文或呼叫工具。",
   "你上一步的思考用完了整個輸出額度，正文還沒開始就被截斷。這次請少想一點，直接寫正文或呼叫工具。"),
_e("stall", "接續", "說要做卻停下", "短回覆最後一句是「接下來我會…」卻沒呼叫工具（continuation 插件）",
   "你說要接著做卻停下了。直接呼叫需要的工具做完；已經做完就給最終回覆。",
   "你剛才說要接著做，但沒有呼叫任何工具就停下了。現在直接執行需要的工具呼叫，把使用者的要求做完再給最終回覆。如果其實已經全部做完，就直接給出完整的最終回覆。"),
_e("todo_left", "接續", "待辦沒做完", "待辦清單還有項目就結束（continuation 插件）",
   "待辦還有 {n} 項沒完成：\n{items}\n做完並更新清單；要使用者決定就直接問；不用做了就更新清單後給最終回覆。",
   "待辦清單還有 {n} 項沒完成：\n{items}\n請繼續把它們做完並更新清單狀態。如果需要使用者決定才能繼續，就明確提出問題；如果其實已經完成或不需要做了，更新清單並給最終回覆。", vars="n, items"),
_e("nudge_tool", "接續", "工具結果後空回覆", "工具跑完模型卻回空白",
   "用文字回覆使用者：工具的結果與結論。",
   "請用文字回覆使用者：說明剛才工具的結果與結論。"),
_e("nudge_empty", "接續", "空回覆", "模型回空白（不是在工具結果後）",
   "上一步沒有輸出。直接用文字回覆使用者。",
   "你上一步沒有輸出任何內容。請直接用文字回覆使用者。"),
_e("resume", "接續", "中斷後接續", "按「繼續」或出錯自動接續時",
   "上一回合中途停下（{reason}）。看最後的工具結果確認進度，從中斷處接著做，不要重做。",
   "上一回合在中途停下了（{reason}）。請先看上面最後的工具結果確認做到哪裡，從中斷的地方接續完成使用者的要求；已經完成的步驟不要重做，也不要從頭開始。", vars="reason"),
_e("bg_done", "接續", "背景指令結束", "背景指令跑完時送回對話（terminal 插件）",
   "任務 {id} 已結束（{state}，{elapsed}）\n指令：{command}\n最後輸出：\n{output}\n\n依結果接著做；跟目前工作無關就簡短說明結果。",
   "任務 {id} 已結束（{state}，跑了 {elapsed}）\n指令：{command}\n最後輸出：\n{output}\n\n請依這個結果接著處理使用者的要求；如果和目前的工作無關，簡短說明結果即可。", vars="id, state, elapsed, command, output"),
_e("raw_args", "接續", "工具參數被截斷", "工具參數不是完整 JSON（多半是輸出被截斷）時的工具結果",
   "工具參數不是完整 JSON（多半被截斷）。拆成幾次較小的呼叫，例如先寫一部分再用 patch 補。",
   "工具參數不是完整的 JSON（多半是這次輸出超過長度上限被截斷）。請拆成幾次較小的呼叫，例如先寫一部分，再用 patch 補上其餘內容。"),
_e("perm_denied", "接續", "權限擋下", "權限設定擋下工具時的工具結果",
   "權限擋下（{reason}）。不要換寫法重試；改用別的方法，或請使用者手動處理。",
   "權限設定擋下了這個工具（{reason}）。不要換個寫法重試同樣的操作；請改用其他方法，或告訴使用者需要他手動處理。", vars="reason"),
_e("compact_head", "壓縮", "摘要開頭", "壓縮後，摘要放在對話最前面的那則訊息",
   "【上下文摘要】以下是較早部分的摘要，原始訊息保留在資料庫：\n\n{summary}",
   "【上下文摘要】以下是這個對話較早部分的摘要，原始訊息已保留在資料庫：\n\n{summary}", vars="summary"),
_e("auto_review", "權限", "自動審核", "「先問我」時，先請審核模型判斷這個動作安不安全（只給工具、參數、工作目錄）",
   "判斷這個工具動作能不能不問使用者直接執行。讀取、工作目錄內一般改檔、跑測試建置、查狀態、一般網路查詢＝SAFE；"
   "大量刪除覆寫、改工作目錄外、改系統設定、安裝移除軟體、推送發布、付費或對外發訊息、看不懂、參數被截斷＝ASK。"
   "只回一行：SAFE 或 ASK 加一句理由。",
   "你是工具執行的安全審核。判斷下面這個動作能不能不問使用者直接執行。\n"
   "可以放行：只讀取、只在工作目錄內建立或修改一般檔案、跑測試或建置、查詢版本與狀態、一般的網路查詢。\n"
   "要問使用者：刪除或覆寫大量檔案、改工作目錄以外的檔案、改系統設定或登錄檔、安裝或移除軟體、"
   "推送或發布到遠端、付費或對外發送訊息、看不懂在做什麼、參數被截斷。\n"
   "只回一行：SAFE 或 ASK，後面接一句理由。"),
_e("tool_pruned", "壓縮", "舊工具輸出的佔位", "壓縮時先把較早、已經用過的工具輸出換成這一行（夠的話就不用寫摘要）",
   "[舊工具輸出已清掉：{name}，{status}，原本約 {chars} 字。要看內容就重新執行或讀取]",
   "[這是較早的工具輸出，為了節省上下文已經清掉：{name}，{status}，原本約 {chars} 字。還需要內容的話，重新執行或重新讀取]", vars="name, status, chars"),
_e("compact_inflight", "壓縮", "壓縮時仍在進行中", "回合做到一半壓縮、使用者這次的要求被摘要進去時，接在摘要後面",
   "\n\n【仍在進行中】使用者這次的要求（原文）。還沒做完：接著做，不要重做。\n\n{request}",
   "\n\n【仍在進行中】下面是使用者這次的要求，原文照錄。壓縮時還沒做完：請接著做完，不要從頭開始，已經完成的步驟不要重做。\n\n{request}", vars="request"),
_e("vision", "其他", "看圖助手", "看圖工具，以及不能看圖的模型收到圖片時交給看圖模型",
   "你是看圖助手，用繁體中文精確回答。",
   "你是看圖助手，用繁體中文精確、有依據地回答，看不清楚的地方直接說看不清楚。"),
_e("compact", "壓縮", "寫摘要（系統提示）", "壓縮時交給摘要模型；精簡＝設定 → 壓縮的「精簡」風格",
   "把以下對話壓縮成可續接的摘要，用繁體中文，只保留必要內容：使用者目標、已定的決定、未完成事項、檔案路徑、關鍵數值與名稱。條列式，不寫過程、不寫評論、不寫寒暄。",
   "把以下對話壓縮成可續接的摘要，用繁體中文，重點寫清楚即可，不重述對話過程、不加評論。依下面的段落整理，沒有內容的段落就省略：\n## 目標與決定\n## 已完成\n## 進行中與未完成\n## 檔案、指令與結果（保留路徑、數字、關鍵輸出）\n## 限制與錯誤"),
_e("compact_merge", "壓縮", "合併分段摘要", "對話太長分段摘要後，合併成一份",
   "以下是同一段對話的多份局部摘要（依時間順序）。請合併成一份完整摘要：去除重複，保留所有未完成事項、檔案路徑與關鍵數值，不要遺漏。用繁體中文。",
   "以下是同一段對話的多份局部摘要（依時間順序）。請合併成一份完整摘要：去除重複，保留所有未完成事項、檔案路徑與關鍵數值，不要遺漏。用繁體中文。"),
_e("compact_part", "壓縮", "分段摘要的提示", "分段摘要時，每段附上的說明",
   "（這是整段對話的第 {i}/{k} 段，只摘要這一段，不要猜測其他段的內容。）",
   "（這是整段對話的第 {i}/{k} 段，只摘要這一段，不要猜測其他段的內容。）", vars="i, k"),


_OV_FILE = DATA / "prompt_overrides.json"
_ov_cache: dict | None = None


def overrides() -> dict[str, str]:
    global _ov_cache
    if _ov_cache is None:
        try:
            _ov_cache = {k: str(v) for k, v in json.loads(_OV_FILE.read_text(encoding="utf-8")).items() if k in CATALOG}
        except (OSError, ValueError):
            _ov_cache = {}
    return _ov_cache


def set_override(key: str, text: str | None) -> None:
    global _ov_cache
    if key not in CATALOG:
        raise KeyError(key)
    ov = dict(overrides())
    if text is None or not str(text).strip() or text in (CATALOG[key]["lean"], CATALOG[key]["normal"]):
        ov.pop(key, None)
    else:
        bad = {f for _, f, _, _ in string.Formatter().parse(text) if f} - set(CATALOG[key]["vars"])
        if bad:
            raise ValueError(f"不認得的代入值：{', '.join(sorted(bad))}（可用：{', '.join(CATALOG[key]['vars']) or '無'}）")
        ov[key] = text
    _OV_FILE.parent.mkdir(parents=True, exist_ok=True)
    _OV_FILE.write_text(json.dumps(ov, ensure_ascii=False, indent=1), encoding="utf-8")
    _ov_cache = ov


def mode_of(cfg: dict) -> str:
    m = str(((cfg or {}).get("prompts") or {}).get("mode") or "normal")
    return m if m in MODES else "normal"


def _mode(cfg: dict | None) -> str:
    if cfg is None:
        from .config import load_config

        cfg = load_config()
    return mode_of(cfg)


def _lang(text: str) -> str:
    """介面語言是英文時，指定「用繁體中文」的段落（看圖、摘要）改成用英文。"""
    if "繁體中文" in text:
        from .lang import ui_lang
        if ui_lang() == "en":
            return text.replace("繁體中文", "英文（English）")
    return text


def get(key: str, cfg: dict | None = None, **fmt) -> str:
    """取得某段內部提示詞（有覆寫用覆寫）。fmt 代入 {名稱}。"""
    text = _lang(overrides().get(key) or CATALOG[key][_mode(cfg)])
    return text.format(**fmt) if fmt else text


def get_in(key: str, mode: str, **fmt) -> str:
    """指定寫法取（壓縮插件有自己的精簡／常規設定）。"""
    text = _lang(overrides().get(key) or CATALOG[key]["lean" if mode in ("lean", "精簡") else "normal"])
    return text.format(**fmt) if fmt else text


def catalog(cfg: dict | None = None) -> dict:
    mode, ov = _mode(cfg), overrides()
    items = []
    for e in CATALOG.values():
        items.append({**e, "override": ov.get(e["key"]), "active": ov.get(e["key"]) or e[mode]})
    return {"mode": mode, "groups": list(GROUPS), "items": items}
