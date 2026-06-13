# 📈 MCSS — Momentum + Catalyst Swing System

> 一個每日自動運行嘅美股 swing trade screening 系統。由 AI agent team 協作，每日定時掃描全市場，篩選 Top 5 最值得留意嘅股票，自動 push 去 Telegram —— **唔需要開機，唔需要俾指令**。

---

## 🎯 呢個系統解決咩問題

| 痛點 | 解決方案 |
|------|----------|
| 唔知每日買咩 | 每日自動 screen 全市場 → Top 5 swing |
| 靠直覺交易 → 持續虧損 | 量化規則取代情緒決策 |
| FOMO 追高、唔 cut loss | 心理護欄硬編碼入系統（RSI > 72 封鎖等） |
| 冇時間盯盤 | 收市後自動分析，Telegram 推送 |
| 想要 entry/stop/target | 系統自動計算建議價位 |
| 想知邊隻異動、有咩 catalyst | 每日 day trade 異動掃描（ORB / gap）+ 免費新聞 catalyst |
| 0 隻入選嗰日一片空白 | 照出板塊 RS 雷達 + 大市頭條，「留意板塊」提示 |

**設計哲學：** Criteria 過關只係「入場資格」，唔等於「必賺」。系統最大價值係幫你**冷靜、有紀律、精準出手**。

---

## 🏛️ 系統架構

```
   ┌──────────────────────┐          ┌───────────────────────────┐
   │ GitHub Actions (Cron) │          │  本地 ./run_mcss.sh        │
   │ CI: run_pipeline.py   │          │  agent: mcss-orchestrator  │
   │ (全 Python，唔使開機) │          │  (Bash 順序調度)           │
   └───────────┬──────────┘          └─────────────┬─────────────┘
               └───────────────┬───────────────────┘
                               ▼  順序 pipeline (每級縮小宇宙)
 ┌──────┐ ┌──────┐ ┌──────────┐ ┌─────────┐ ┌───────┐ ┌──────────┐ ┌──────┐
 │Gate 0│→│  L1  │→│    L2    │→│   L3    │→│  L4   │→│    L5    │→│Report│
 │Market│ │Fetch │ │Fundamen- │ │Technical│ │ Quant │ │ Catalyst │ │ +TG  │
 │ Gate │ │Univ. │ │tal Filter│ │ Filter  │ │Scoring│ │ (AI/新聞)│ │      │
 └──────┘ └──────┘ └──────────┘ └─────────┘ └───────┘ └──────────┘ └──────┘
 市場檢查  抓數據    L1+L2基本面   技術篩選    量化評分   新聞情緒    推送TG
 └────────── deterministic Python（有 unit test）──────────┘ └─ LLM ─┘
```

> **L1–L4 + Report 全部係 deterministic Python script**（`scripts/*.py`，有 unit test，唔靠 LLM）。**只有 L5 catalyst 需要 LLM judgment**（live 新聞研究）。
> - **CI 路徑**：`run_pipeline.py` 跑全 Python，L5 用 `ai_catalyst.py`（Gemini API）。
> - **本地路徑**：`mcss-orchestrator` agent 用 Bash 順序調度上面各 script，L5 spawn `mcss-catalyst-analyst`（行 Claude.ai subscription，免 API 費）。
>
> `.claude/agents/` 只保留 `mcss-orchestrator` 同 `mcss-catalyst-analyst` 兩個 agent。詳見 [`CLAUDE.md`](./CLAUDE.md)。

---

## ⚙️ 點解用 GitHub Actions（關鍵決定）

你要求「**唔開機都自動跑**」，所以唔可以用本地電腦（熄機就唔跑）。

| 方案 | 成本 | 適合度 |
|------|------|--------|
| **GitHub Actions** ✅ | $0（public repo 無限，private 2000分鐘/月） | ⭐⭐⭐⭐⭐ 最佳 |
| Railway / Render | 有免費 tier 但限制多 | ⭐⭐⭐ |
| Oracle Cloud Free | 永久免費 VM 但要自己管 | ⭐⭐⭐ |
| 本地電腦 cron | $0 但要長開機 | ❌ 唔符合你需求 |

**GitHub Actions 計算：** 每次 screening run 約 5–10 分鐘，每日 2 次（pre/post-market）= 約 600 分鐘/月。Public repo 完全免費；private repo 都喺 2000 分鐘額度內。

---

## 📡 數據宇宙來源

系統按優先順序自動選擇股票宇宙：

| 優先 | 來源 | 數量 | 需要 |
|------|------|------|------|
| 1 | Alpaca Trading API（活躍美股 + bars 過濾）| ~729 | `ALPACA_API_KEY` + `ALPACA_API_SECRET` |
| 2 | S&P 500 + NASDAQ 100 + S&P 400 MidCap（Wikipedia）| ~805 | 唔需要（默認） |
| 3 | NASDAQ FTP 全宇宙（`--full-universe` flag）| ~8,000+ | 設計用於 GitHub Actions |

**本地開發默認用第 2 種**（唔需要 API key 都可以跑完整 pipeline）。

---

## 💰 成本分析

| 組件 | 成本 |
|------|------|
| GitHub Actions（排程）| **$0** |
| yfinance（股價/基本面數據）| **$0** |
| Telegram Bot（推送）| **$0** |
| pandas-ta / numpy（技術指標）| **$0** |
| Catalyst 新聞（yfinance + Google News RSS）| **$0**（keyless）|
| 板塊 RS（SPDR ETF 數據）| **$0** |
| Day trade 掃描 + backtest（Alpaca IEX）| **$0**（free tier）|
| **Lite 版小計** | **$0/月** ✅ |
| Catalyst 增強（可選）— Finnhub / NewsAPI free tier | **$0**（額度內）|
| L5 AI 情緒分析（可選）— Gemini `gemini-1.5-flash` | **~$0**（free tier 額度內，CI 預設）|
| L5 AI 情緒分析（可選）— Claude `claude-sonnet-4-6`（質素更高）| ~$5–15/月 |
| **AI-enhanced 版小計** | **$0–15/月** |

> 你預算係 $0 — 可以先跑 **Lite 版**（無 AI key，L5 直接 pass-through L4 排名），完全免費。想要 AI 新聞分析：**CI 用 Gemini（基本免費）**；本地用 `mcss-catalyst-analyst` agent 行你嘅 Claude.ai subscription（免 API 費）；想 CI 都用 Claude 質素先加 `ANTHROPIC_API_KEY`。

---

## 📅 每日運行時間表（HKT）

根據你選擇「兩個時段都要」：

| 時間 (HKT) | 觸發 | 動作 |
|-----------|------|------|
| **21:00** (美股開市前) | GitHub Actions | Pre-market gap scan + Market Direction 檢查 |
| **次日 05:00** (美股收市後) | GitHub Actions | 完整 5-Layer screening → Top 5 報告 |

兩個時段都會 push Telegram。

---

## 🚀 Setup 步驟（部署前準備）

> ⚠️ 以下需要你**自己**完成（涉及帳戶建立同密鑰，唔可以由 AI 代做）

### 1. 建立 Telegram Bot
1. Telegram 搜尋 `@BotFather`
2. 輸入 `/newbot`，跟指示建立
3. 記低 **Bot Token**（類似 `123456:ABC-DEF...`）
4. 搜尋你個 bot，send 一句嘢
5. 開 `https://api.telegram.org/bot<TOKEN>/getUpdates` 攞你嘅 **Chat ID**

### 2. （可選）申請 L5 AI 分析 Key
- **Gemini（推薦俾 CI 用，基本免費）**：去 https://aistudio.google.com/apikey 攞 `GEMINI_API_KEY`
- **Claude（可選，質素更高，付費）**：去 https://console.anthropic.com 攞 `ANTHROPIC_API_KEY`
- 兩者皆可選；都唔設就跑 Lite 版（L5 pass-through L4 排名）。本地用 `mcss-catalyst-analyst` agent 行 Claude.ai subscription 則唔使任何 key

### 2b. 本地開發：建立 `.env` 文件
喺項目根目錄建立 `.env`（已加入 `.gitignore`，唔會 commit）：

```
TELEGRAM_BOT_TOKEN=你嘅bot_token
TELEGRAM_CHAT_ID=你嘅chat_id
ALPACA_API_KEY=你嘅alpaca_key（可選）
ALPACA_API_SECRET=你嘅alpaca_secret（可選）
GEMINI_API_KEY=你嘅gemini_key（可選，本地跑 run_pipeline.py 嘅 L5 用）
ANTHROPIC_API_KEY=你嘅claude_key（可選，若設則 L5 優先用 Claude）
```

本地跑 `python scripts/run_pipeline.py` 時系統會自動讀取。用 `./run_mcss.sh`（agent 路徑）跑 L5 則行 Claude.ai subscription，唔使 AI key。

### 3. 設定 GitHub Secrets
喺你個 GitHub repo → Settings → Secrets and variables → Actions，加入：

```
TELEGRAM_BOT_TOKEN   = 你嘅 bot token
TELEGRAM_CHAT_ID     = 你嘅 chat id
ALPACA_API_KEY       = 你嘅 Alpaca key（可選，宇宙更精準 + day trade gap 掃描）
ALPACA_API_SECRET    = 你嘅 Alpaca secret（可選）
GEMINI_API_KEY       = 你嘅 Gemini key（可選，L5 AI 分析；workflow 預設用呢個）
FINNHUB_API_KEY      = 你嘅 Finnhub key（可選，catalyst 新聞增強，free tier）
NEWSAPI_KEY          = 你嘅 NewsAPI key（可選，多一個新聞源，free tier）
```

> ℹ️ CI 嘅 L5 預設行 **Gemini**（`daily_screen.yml` 只注入 `GEMINI_API_KEY`）。若想 CI 改用 Claude，要另設 `ANTHROPIC_API_KEY` secret **並**喺 `daily_screen.yml` 個 `env:` 加返佢（`ai_catalyst.py` 有 key 就優先用 Claude）。
>
> ℹ️ **Catalyst 新聞唔使任何 key 都跑得**（yfinance + Google News RSS keyless）。`FINNHUB_API_KEY` / `NEWSAPI_KEY` 只係增強，冇就自動 keyless mode。

> 🔒 密鑰只放 GitHub Secrets / `.env`，**永遠唔好** commit 入代碼。

---

## 📦 項目結構

```
MCSS_STS/
├── README.md
├── CLAUDE.md                  ← AI agent team 指引 + 完整 spec
├── requirements.txt           ← Python 依賴
├── run_mcss.sh                ← 本地快速運行腳本
├── config/
│   └── criteria.yaml          ← Screening 參數（可調整）
├── scripts/
│   ├── run_pipeline.py        ← 主協調器（順序執行7個階段）
│   ├── market_gate.py         ← Gate 0: SPY/QQQ/VIX 市場方向
│   ├── fetch_universe.py      ← L1: 宇宙抓取（Alpaca / S&P500+NDX+S&P400）
│   ├── fundamental_filter.py  ← L2: 基本面篩選
│   ├── technical_filter.py    ← L3: Minervini Trend Template + VCP
│   ├── quant_scoring.py       ← L4: 量化評分
│   ├── ai_catalyst.py         ← L5: AI 新聞情緒
│   ├── report_agent.py        ← 生成報告
│   ├── send_telegram.py       ← Telegram 推送
│   └── indicators/            ← rs_rating, vcp, trend_template
├── .github/workflows/
│   └── daily_screen.yml       ← GitHub Actions 排程
├── tests/                     ← pytest 測試套件（38 tests）
└── data/                      ← 每次運行輸出（CSV/JSON，唔 commit）
```

---

## 🛡️ 內建心理護欄

針對你問卷顯示嘅 3 大壞習慣，系統硬性執行：

| 壞習慣 | 護欄規則 |
|--------|----------|
| FOMO 追高 | RSI > 72 嘅股票唔會出現喺 buy list |
| 唔 cut loss（死持）| Entry -3% 自動 TG 警報，-5% 標記必須處理 |
| 過早獲利 | < +3% 唔會發 sell signal（除非觸 stop）|

---

## 📊 Telegram 報告範例

```
📊 MCSS 每日篩選 — 2026-05-24 (收市後)
篩選漏斗: 805隻 → L3通過: 28隻 → Top 5

今日入選股票
━━━━━━━━━━━━━━━━━━━━━━
#1 NVDA — NVIDIA Corporation
綜合評分: 92/105  |  市值: $3.1B

板塊: Technology · Semiconductors

📈 技術信號
  • RS評級: 94/99 — 跑贏全市 94% 股票
  • RSI(14): 63.2 — 強勢但未過熱
  • 成交量: 1.8x 平均 (資金流入)
  • 距52週高: -4.1% — 接近高位整固
  • 趨勢模板: ✅ EMA20>50>150>200 多頭排列 | VCP收縮形態

💰 基本面快照
  • 收入增長: +94.0% YoY
  • 毛利率: +75.0%  |  ROE: +91.5%
  • 自由現金流: $28.1B  |  預測PE: 32.4x
  • 機構持股: +66.0%

🏆 評分細分
  RS動能:20 + 質素:24 + 動量:23 + VCP:13 + 波幅:8 + 估值:4

📍 交易設置
  入場: $128.50
  止損: $124.80 (-2.9%)  [ATR×1.5 = $2.47]
  目標: $135.90 (+5.8%)  [2:1 風險回報]
  Catalyst: Morgan Stanley upgrade to Overweight (+2). Q1 EPS beat 14% (+2).
━━━━━━━━━━━━━━━━━━━━━━
純系統輸出，非投資建議。所有決定請自行判斷。

[#2–#5 同格式，略]
```

> 範例對應 `report_agent.py` 實際輸出格式（Telegram 以 HTML 粗體渲染）。有 earnings 嘅股票會多一行 `⚠️ EARNINGS RISK — 倉位減半，止損收緊`；0 隻入選時改發精簡「今日 0 隻」通知。

---

## ⚠️ 免責聲明

本系統僅供教育及研究用途，**唔係投資建議**。所有分析基於公開數據，過去表現唔保證未來結果。投資涉及風險，所有交易決定由使用者自行負責。系統設計者唔對任何投資損失負責。

---

## 🗺️ 開發進度

- [x] 需求分析（100條問卷）
- [x] Screening Criteria v2 確認
- [x] **Phase 1**: 數據基建 + L1/L2 (Week 1–2)
- [x] **Phase 2**: L3 技術 + Trend Template + RS Rating (Week 3–4)
- [x] **Phase 3**: L4 Quant Scoring + L5 AI Catalyst (Week 5–6)
- [x] **Phase 4**: Telegram Bot + GitHub Actions 部署 (Week 7)
- [x] **Phase 5**: Backtesting + 優化 (Week 8–10)
- [x] **Bug fixes**: yfinance 401 rate limit 修正、空 CSV crash 修正、Alpaca env 加載修正
- [x] **Day Trade + Catalyst 進化** (2026-06): keyless catalyst 聚合、板塊 RS 雷達、ORB/Gap 異動掃描、combined message、TG 4096 分拆、`--session` bug fix
  - Day trade backtest（6 個月）：ORB regime-dependent、Gap 失敗 → 定為**資訊版 watchlist**（`tradeable: false`），非交易信號
- [x] **Test suite**: 90 pytest tests（38 原有 + 52 新）
- [ ] **Phase 6**: IBKR Auto Trading (未來，可選)

詳細規格見 [`CLAUDE.md`](./CLAUDE.md)。
