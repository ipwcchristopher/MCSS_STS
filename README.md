# 📈 MCSS — Momentum + Catalyst Swing System

> 一個每日自動運行嘅美股 swing trade screening 系統。由 AI agent team 協作，每日定時掃描全市場，篩選 Top 5 最值得留意嘅股票，自動 push 去 Telegram —— **唔需要開機，唔需要俾指令**。

---

## 🎯 呢個系統解決咩問題

| 痛點 | 解決方案 |
|------|----------|
| 唔知每日買咩 | 每日自動 screen 全市場 → Top 5 |
| 靠直覺交易 → 持續虧損 | 量化規則取代情緒決策 |
| FOMO 追高、唔 cut loss | 心理護欄硬編碼入系統（RSI > 72 封鎖等） |
| 冇時間盯盤 | 收市後自動分析，Telegram 推送 |
| 想要 entry/stop/target | 系統自動計算建議價位 |

**設計哲學：** Criteria 過關只係「入場資格」，唔等於「必賺」。系統最大價值係幫你**冷靜、有紀律、精準出手**。

---

## 🏛️ 系統架構

```
                    ┌─────────────────────────┐
                    │   GitHub Actions (Cron)  │  ← 免費，每日定時觸發
                    │   唔需要你開機             │
                    └───────────┬─────────────┘
                                │
              ┌─────────────────┴─────────────────┐
              │         Agent Orchestrator         │
              └─────────────────┬─────────────────┘
                                │
   ┌──────────┬──────────┬──────┴─────┬──────────┬──────────┐
   ▼          ▼          ▼            ▼          ▼          ▼
┌──────┐  ┌──────┐  ┌────────┐  ┌────────┐  ┌──────┐  ┌────────┐
│Market│  │ Data │  │Technical│  │ Quant  │  │  AI  │  │ Report │
│ Gate │→ │Agent │→ │ Agent   │→ │Scoring │→ │Agent │→ │ Agent  │
│Agent │  │      │  │         │  │ Agent  │  │      │  │  (TG)  │
└──────┘  └──────┘  └────────┘  └────────┘  └──────┘  └────────┘
  市場檢查   抓數據    技術篩選     量化評分    新聞情緒   推送TG
```

每個 Agent 有專屬職責，順序協作。詳見 [`CLAUDE.md`](./CLAUDE.md)。

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

## 💰 成本分析

| 組件 | 成本 |
|------|------|
| GitHub Actions（排程）| **$0** |
| yfinance（股價/基本面數據）| **$0** |
| Telegram Bot（推送）| **$0** |
| pandas-ta / numpy（技術指標）| **$0** |
| **Lite 版小計** | **$0/月** ✅ |
| Claude API（新聞情緒分析，可選）| ~$5–15/月 |
| **AI-enhanced 版小計** | **~$5–15/月** |

> 你預算係 $0 — 可以先跑 **Lite 版**（跳過 L5 AI 情緒層，用 rule-based catalyst），完全免費。之後想要 AI 新聞分析先加 Claude API。

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

### 2. （可選）申請 Claude API Key
- 去 https://console.anthropic.com 申請（AI-enhanced 版先需要）

### 3. 設定 GitHub Secrets
喺你個 GitHub repo → Settings → Secrets and variables → Actions，加入：

```
TELEGRAM_BOT_TOKEN   = 你嘅 bot token
TELEGRAM_CHAT_ID     = 你嘅 chat id
ANTHROPIC_API_KEY    = 你嘅 Claude key（可選）
```

> 🔒 密鑰只放 GitHub Secrets，**永遠唔好** commit 入代碼。

---

## 📦 項目結構（建設完成後）

```
mcss_project/
├── README.md                 ← 本文件
├── CLAUDE.md                 ← AI agent team 指引
├── requirements.txt          ← Python 依賴
├── config/
│   ├── criteria.yaml         ← Screening 參數（可調整）
│   └── watchlist.txt         ← 你嘅自選股
├── src/
│   ├── orchestrator.py       ← 主協調器
│   ├── agents/
│   │   ├── market_gate.py     ← Gate 0: 市場方向
│   │   ├── data_agent.py      ← L1: 數據抓取
│   │   ├── fundamental.py     ← L2: 基本面篩選
│   │   ├── technical.py       ← L3: 技術 + Trend Template
│   │   ├── quant_scoring.py   ← L4: 量化評分
│   │   ├── ai_catalyst.py     ← L5: AI 新聞情緒
│   │   └── report_agent.py    ← Telegram 報告
│   ├── indicators/
│   │   ├── rs_rating.py       ← Relative Strength 計算
│   │   ├── vcp.py             ← VCP pattern 檢測
│   │   └── trend_template.py  ← Minervini Trend Template
│   └── utils/
│       ├── telegram.py        ← TG 推送
│       └── guardrails.py      ← 心理護欄規則
├── .github/workflows/
│   └── daily_screen.yml      ← GitHub Actions 排程
└── tests/                    ← 測試
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
🎯 MCSS DAILY TOP 5 — 2026-05-24 (收市後)
━━━━━━━━━━━━━━━━━━━━━━━
📊 市場狀態: ✅ CONFIRMED UPTREND
   SPY > 200EMA ✓ | QQQ > 200EMA ✓ | VIX 16.2

━━━ #1 NVDA | Score: 92/105 ━━━
🟢 RS Rating: 94 (跑贏94%市場)
📈 入場區: $128.50 – $130.20
🛑 止損位: $124.80 (-3.5%, ATR 1.5x)
🎯 目標1: $136.00 (+5%) → 出50%
🎯 目標2: Trailing stop
⚡ Catalyst: Analyst upgrade (GS, TP$175)
📰 新聞情緒: POSITIVE ↑
🔥 VCP: 3 contractions, 成交量乾涸
⚠️ Earnings in 8 days
━━━━━━━━━━━━━━━━━━━━━━━
[#2-#5 略...]
```

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
- [ ] **Phase 6**: IBKR Auto Trading (未來，可選)

詳細規格見 [`CLAUDE.md`](./CLAUDE.md)。
