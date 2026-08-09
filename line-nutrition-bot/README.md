# LINE 食物營養紀錄 Bot

在 LINE 傳一張食物照片，Bot 會辨識食物、確認不確定的項目、估算熱量與蛋白質，並存進 Notion。另外支援體重記錄、每日平均消耗熱量估算、每日熱量盈餘計算。

## 功能總覽
| 功能 | 使用方式 |
|---|---|
| 📷 餐點營養估算 | 傳食物照片 → 依提示確認 → 得到各項與總計熱量/蛋白質，自動存檔 |
| 📊 營養統計 | 輸入或點選 `今日` / `本週` / `本月`（今日總計、本週每日平均、本月每日平均）|
| ⚖️ 上傳體重 | 點「上傳體重」→ 回覆純數字（例如 `68.5`）|
| 🔥 計算每日平均消耗熱量 | 點「計算每日平均消耗熱量」→ 用過去一週體重變化 + 前一日攝取估算，更新此表 |
| ➕ 熱量盈餘 | 點「今日熱量盈餘」→ 今日攝取 − 每日平均消耗熱量；缺資料會明確告知缺哪一項 |

所有功能都做在 LINE 介面內，並附 **Rich Menu 圖文選單**，六個功能常駐輸入框下方。

---

## 技術與成本
- **後端**：Python + Flask（`gunicorn` 部署）
- **AI**：Google Gemini `gemini-2.5-flash`（低成本，有免費額度）
- **儲存**：Notion（兩張資料庫）
- **對話狀態**：SQLite（伺服器休眠喚醒／當機重啟時，半途對話不遺失）
- **部署**：Render 免費方案
- **防休眠**：UptimeRobot 免費定時喚醒
- 全部服務皆有免費方案，個人使用零成本。

---

## 每日平均消耗熱量估算邏輯
採「區間夾擠法」。把每日消耗視為每日平均消耗熱量，並假設體重變化反映「前一天」攝取與消耗的落差：
- 某天**變輕** → 前一天消耗>攝取 → 每日平均消耗熱量 **大於** 前一日攝取（一個下界 x）
- 某天**變重** → 前一天消耗<攝取 → 每日平均消耗熱量 **小於** 前一日攝取（一個上界 y）

按下「計算每日平均消耗熱量」時，統計過去 7 天：
`每日平均消耗熱量 ≈ ( max(所有下界 x) + min(所有上界 y) ) / 2`

例外處理：
- 有效比較 < 3 天 → 回覆資料不足
- 只有下界或只有上界 → 回覆「只能推估大於/小於 ○○」
- 下界 > 上界（區間矛盾，多因水分波動）→ 不給數字，建議累積更多天

> 註：此法反推的其實是「每日總消耗（含活動量）」，非躺著不動的最低代謝，屬日常追蹤用估計值。

---

## 部署步驟

### Step 1. 申請 Gemini API Key
前往 <https://aistudio.google.com/apikey> → Create API key → 複製（`GEMINI_API_KEY`）。

### Step 2. 建立五張 Notion 資料庫

先在 <https://www.notion.so/my-integrations> 建立 integration，複製 **Internal Integration Token**（`NOTION_API_KEY`）。

**資料庫 A — 餐點表** `NOTION_DATABASE_ID`
| 欄位名稱 | 型別 |
|---|---|
| `使用者ID` | Title |
| `暱稱` | Text |
| `日期時間` | Date |
| `食物明細` | Text |
| `總熱量` | Number |
| `總蛋白質` | Number |

**資料庫 B — 體重表** `NOTION_WEIGHT_DATABASE_ID`
| 欄位名稱 | 型別 |
|---|---|
| `使用者ID` | Title |
| `暱稱` | Text |
| `日期` | Date |
| `體重` | Number |

> 體重表採「同一人同一天覆蓋為最新一筆」，所以一天內重複上傳只會留最後一次的值。

**資料庫 C — 每日平均消耗熱量表（每人一列）** `NOTION_BMR_DATABASE_ID`
| 欄位名稱 | 型別 |
|---|---|
| `使用者ID` | Title |
| `暱稱` | Text |
| `每日平均消耗熱量` | Number |
| `更新時間` | Date |

**資料庫 D — 蛋白質目標表（每人一列）** `NOTION_PROTEIN_DATABASE_ID`
| 欄位名稱 | 型別 |
|---|---|
| `使用者ID` | Title |
| `暱稱` | Text |
| `加權數` | Number |
| `更新時間` | Date |

> 每日蛋白質目標＝最近一次體重 × 加權數（例如加權 1.5、體重 80kg → 目標 120g）。

**資料庫 E — 使用者權限表（白名單）** `NOTION_ACCESS_DATABASE_ID`
| 欄位名稱 | 型別 |
|---|---|
| `使用者ID` | Title |
| `暱稱` | Text |
| `是否開通` | Checkbox |
| `首次加入時間` | Date |

> 私人 bot 白名單：使用者第一次傳訊息會自動被記錄（預設未開通）。要開通某人，到這張表把他那列的「是否開通」打勾即可（最多 60 秒生效，不需重新部署）。未開通者只會收到使用說明與付費提示，無法使用任何功能。擁有者（`OWNER_USER_IDS`）永遠有權限。

**五張資料庫都要**打開頁面 → 右上「···」→ Connections → 加入你的 integration（沒做會寫不進去）。

取得各自的 Database ID：資料庫網址 `notion.so/xxxx...xxxx?v=...` 中，`?` 前那段 32 碼即是。

> 資料保留：餐點與體重超過 `DATA_RETENTION_DAYS`（預設 60）天會在使用者互動時自動清除，Notion 不會無限長大。每日平均消耗熱量表每人只有一列，不清除。

### Step 3. 建立 LINE Bot
1. <https://developers.line.biz/console/> → 建立 Provider → 建立 Messaging API channel。
2. **Basic settings** → 複製 `Channel secret`。
3. **Messaging API** → Issue `Channel access token`（long-lived）→ 複製。
4. 關閉「自動回覆訊息」、開啟「Webhooks」。

### Step 4. 部署到 Render（免費）
1. 專案推到 GitHub repo。
2. <https://render.com> → New Web Service → 連結 repo。
3. 設定：
   - Build Command：`pip install -r requirements.txt`
   - Start Command：`gunicorn -c gunicorn.conf.py app:app`
4. Environment Variables 填入：

| Key | 說明 |
|---|---|
| `LINE_CHANNEL_ACCESS_TOKEN` | LINE token |
| `LINE_CHANNEL_SECRET` | LINE secret |
| `GEMINI_API_KEY` | Gemini 金鑰 |
| `GEMINI_MODEL` | `gemini-2.5-flash` |
| `NOTION_API_KEY` | Notion token |
| `NOTION_DATABASE_ID` | 餐點表 ID |
| `NOTION_WEIGHT_DATABASE_ID` | 體重表 ID |
| `NOTION_BMR_DATABASE_ID` | 每日平均消耗熱量表 ID |
| `NOTION_PROTEIN_DATABASE_ID` | 蛋白質目標表 ID |
| `NOTION_ACCESS_DATABASE_ID` | 使用者權限表 ID |
| `OWNER_USER_IDS` | 你的 LINE User ID（永遠有權限；多個用逗號分隔）|
| `TIMEZONE_OFFSET` | `8` |
| `BMR_MIN_DAYS` | `3` |
| `DATA_RETENTION_DAYS` | `60` |

5. 部署後取得網址，例如 `https://xxxx.onrender.com`。

### Step 5. 綁定 Webhook
LINE → Messaging API → Webhook URL 填 `https://你的網址.onrender.com/callback` → Verify → 開啟 Use webhook。

### Step 6. 建立 Rich Menu 圖文選單
在本機（或任何裝好套件、設好 `.env` 的環境）執行一次：
```bash
pip install -r requirements.txt
cp .env.example .env   # 填入金鑰
python rich_menu_setup.py
```
腳本會自動產生選單圖並上架。要移除：`python rich_menu_setup.py --delete`。

### Step 7. 防止免費伺服器休眠（選用但建議）
Render 免費方案閒置 15 分鐘會休眠，喚醒需 30–50 秒。
到 <https://uptimerobot.com>（免費）建立一個 HTTP(s) 監控，
URL 填你的 Render 網址（根路徑 `/` 會回 OK），間隔 5 分鐘，即可保持喚醒。

---

## 使用方式
- 傳**食物照片** → 確認 → 得結果並存檔
- `今日` / `本週` / `本月` → 營養統計
- `上傳體重` → 接著回覆 `68.5` 這樣的數字
- `計算每日平均消耗熱量` → 一週估算並更新該表
- `今日熱量盈餘` → 今日攝取減消耗（需先有今日餐點、今日體重、當前每日平均消耗熱量）

---

## 常見問題
**Q：熱量盈餘說缺資料？**
需同時具備今日餐點紀錄、今日體重、當前每日平均消耗熱量。缺哪項訊息會直接列出，補齊即可。

**Q：算每日平均消耗熱量說資料不足／矛盾？**
需至少 3 天「每天有體重＋前一日餐點紀錄」，且體重要有升有降才能夾出區間。矛盾多因水分波動，持續記錄即可改善。

**Q：Bot 說找不到先前的照片？**
確認流程有 15 分鐘時效，重新部署時暫存會清空。依提示重新上傳照片即可。

**Q：Notion 寫不進去？**
多半是忘了把 integration 加入資料庫 Connections，或欄位名稱／型別不一致。

---

## 專案結構
```
line-nutrition-bot/
├── app.py              # 主程式與對話流程、五大功能路由
├── gemini_service.py   # Gemini：辨識 + 估算（含壓縮、JSON 重試）
├── notion_service.py   # Notion：餐點/體重/統計/每日平均消耗熱量表
├── bmr_service.py      # 每日平均消耗熱量區間夾擠 + 熱量盈餘
├── session.py          # 對話狀態（SQLite，含逾時/遺失處理）
├── config.py           # 環境變數集中管理
├── rich_menu_setup.py  # 一次性：產生並上架圖文選單
├── requirements.txt
├── .env.example
└── README.md
```
