# CLAUDE.md — MCSS Agent Team 指引

> 本文件係俾 Claude / AI agent team 讀嘅。當你（Claude）喺呢個 codebase 工作時，**先讀完本文件**，理解架構、criteria 規格、coding 慣例同護欄規則，再開始任何 task。

---

## 1. 項目本質

**MCSS (Momentum + Catalyst Swing System)** 係一個自動化美股 swing trade screening 系統。

- **用戶背景**: 香港 swing trader，5年+經驗但因「靠直覺交易」持續虧損。識進階 Python。$0–15/月預算。
- **核心目標**: 用系統化規則取代情緒決策，提高 win rate，每日 push Top 5 去 Telegram。
- **交易風格**: Swing trade，持倉 5–15 交易日，同時 4–6 隻，每次風險 2%。
- **理論基礎**: Minervini SEPA + CAN SLIM + 學術 momentum 研究。

⚠️ **用戶 3 大壞習慣（系統必須防範）**: FOMO 追高、唔 cut loss（死持）、過早獲利。

---

## 2. Agent Team 架構

系統採用 **sequential pipeline**，下表 7 個 stage 順序協作，每個單一職責、輸出傳俾下一個。

> **實作注意**：L1–L4 同 Report 全部係 deterministic Python script（有 unit test）。**只有 L5（AI Catalyst）需要 LLM judgment**。本地 (`claude -p`) 路徑由 `mcss-orchestrator` agent 用 Bash 順序調度，並 spawn `mcss-catalyst-analyst` 做 L5；CI 路徑由 `run_pipeline.py` 跑全 Python（L5 用 `ai_catalyst.py`）。`.claude/agents/` 只保留呢 2 個 agent，唔好為 deterministic 步驟另寫 agent。

| Agent | 檔案 | 職責 | 輸入 → 輸出 |
|-------|------|------|------------|
| **Market Gate** | `market_gate.py` | 檢查大市方向，熊市中止 | 市場數據 → PASS/HALT |
| **Data** | `fetch_universe.py` | 抓全市場 OHLCV + 基本面 | tickers → DataFrame |
| **Fundamental** | `fundamental_filter.py` | L1+L2 硬篩（universe + 基本面）| ~5000 → ~120隻 |
| **Technical** | `technical_filter.py` | L3 技術 + Trend Template + RS | ~120 → ~30隻 |
| **Quant Scoring** | `quant_scoring.py` | L4 100分制評分 | ~30 → Top 12 |
| **AI Catalyst** | `ai_catalyst.py` | L5 新聞情緒 + catalyst | Top 12 → Top 5 |
| **Report** | `report_agent.py` | 格式化 + push Telegram | Top 5 → TG message |

**Orchestrator**：CI = `scripts/run_pipeline.py`（GitHub Actions 入口）；本地 = `mcss-orchestrator` agent。任一 stage fail 要 graceful degradation（記 log，唔好 crash 成個 pipeline）。

---

## 3. 完整 Screening 規格（權威來源）

> 以下係系統嘅核心邏輯。所有數值定義喺 `config/criteria.yaml`，方便調整，唔好 hardcode。

### GATE 0 — Market Direction（跑任何嘢之前）
```
PASS 條件（全部成立）:
  - SPY close > SPY 200 EMA
  - QQQ close > QQQ 200 EMA
  - SPY 50 EMA > SPY 200 EMA
  - VIX < 30
FAIL 動作:
  - 中止主 pipeline
  - Telegram 發「⚠️ 市場警戒，建議持現金」
```

### L1 — Universe Filter（Hard Filter）
```
保留條件:
  - price > 10
  - avg_volume_20d > 2_000_000
  - market_cap > 500_000_000
  - distance_from_52w_high < 35%   # 即 price > 52w_high * 0.65
排除:
  - ETF / SPAC / Warrant / 中概 ADR
  - 本週有 earnings → 移去 earnings_play_pool（獨立處理）
預期: ~5000 → ~600
```

### L2 — Fundamental Filter（Hard Filter, Growth-biased）
```
保留條件:
  - revenue_growth_yoy > 15%
  - revenue_growth 加速: 今季 yoy增速 >= 上季 yoy增速
  - eps_qoq 連續2季增速加快 (acceleration)
  - profit_margin Y/Y expanding
  - gross_margin > 35%
  - institutional_ownership > 40%
  - pe < 150  (growth股放寬; 極端泡沫先排除)
預期: ~600 → ~120
```

### L3 — Technical + Trend Template（Hard Filter, 核心）
```
保留條件（Minervini Trend Template + RS）:
  - price > EMA20 AND price > EMA50 AND price > EMA150 AND price > EMA200
  - EMA50 > EMA150 > EMA200  (完美對齊)
  - EMA200 上升趨勢 > 1個月 (對比21交易日前)
  - distance_from_52w_high < 25%  (L3 收緊版)
  - RS_Rating > 70   # 🔴 最重要 — 見 indicators/rs_rating.py
  - 45 <= RSI(14) <= 72
  - volume > 1.2 * avg_volume_20d
  - VCP_score > 60  (可選, 見 indicators/vcp.py)
預期: ~120 → ~30
```

### L4 — Quant Factor Scoring（100分制 + 5 bonus）
```
RS Rating Score:      20分   # RS80+ =20 / RS70-80 =14
Quality Score:        25分   # gross_margin + ROE + FCF
Momentum Score:       25分   # 5/10/20日動量加權
VCP/Tightness Score:  15分   # 成交量收縮 + 價格收緊
Volatility Score:     10分   # ATR/Price 適中為佳
Value Score:           5分   # PEG + PS vs sector
Short Squeeze Bonus:  +5分   # 只在 short_float>15% AND momentum強
─────────────────────────
總分上限: 105    入選門檻: >= 60    取 Top 12
```

### L5 — AI Catalyst + Sentiment Scoring
```
加分:
  + Analyst upgrade (7日內):        +2
  + Earnings beat (最近季 >10%):    +2
  + Insider buying (30日內):         +1
  + 新聞情緒 positive (Claude分析):  +1
  + Sector RS 強勢:                 +1
  + VCP volume dry-up 後突破:        +1
扣分:
  - 重大負面新聞(SEC/訴訟/Recall):  -3
  - Analyst downgrade (7日內):       -2
最終: 排序取 Top 5
```

### Earnings Play Pool（獨立類別）
```
- earnings 在未來 1–5 交易日
- 歷史 EPS surprise: 過去3季平均 beat > 3%
- position size 減半 (最多 10% 組合)
- 止損收緊: ATR x 1.0 (唔係 1.5)
- 報告加 ⚠️ EARNINGS RISK 標籤
```

---

## 4. 心理護欄（唔可妥協）

> **護欄係 code，唔係靠 LLM 心算。** 實作位置：FOMO RSI 硬 block 在 `technical_filter.py`（L3 篩走 RSI>72）；drawdown / overtrading 等組合層護欄在 `backtest.py` + 部位監控；所有數值在 `config/criteria.yaml` `guardrails` section。

針對用戶壞習慣硬編碼，**任何 agent 都唔可以 bypass**：

```python
# FOMO 防範
if rsi > 72:
    exclude_from_buy_list()   # 唔出現喺 Top 5

# 死持防範 (在 position monitoring 階段)
if current_price < entry * 0.97:   # -3%
    send_telegram_alert("⚠️ {ticker} 跌穿 -3%, 檢視止損")
if current_price < entry * 0.95:   # -5%
    flag_must_action()

# 過早獲利防範
if unrealized_gain < 0.03 and not stop_hit:
    suppress_sell_signal()

# 組合層防範
if portfolio_drawdown > 0.10:
    halt_new_entries()
if monthly_trade_count > 15:
    send_warning("交易過頻，質素優先")
```

---

## 5. Position Sizing 規則

```
risk_per_trade = portfolio_value * 0.02      # 固定 2%
stop_distance  = entry - (1.5 * ATR)
shares         = risk_per_trade / (entry - stop_distance)
position_value = shares * entry
# 上限: position_value <= portfolio_value * 0.25  (單一持倉最多25%)
# Earnings play: position_value <= portfolio_value * 0.10
```

---

## 6. 技術 Stack（全部免費為主）

| 用途 | Library | 備注 |
|------|---------|------|
| 數據 | `yfinance` | 主要數據源，$0 |
| 技術指標 | `pandas-ta` | RSI/EMA/ATR/BB |
| 數據處理 | `pandas`, `numpy` | — |
| 新聞 | `NewsAPI` (free tier) | L5 用 |
| AI 分析 | CI: `google-generativeai` (Gemini `gemini-1.5-flash`) / 本地: Claude.ai subscription | L5 用，可選。`ai_catalyst.py` 支援雙引擎：有 `ANTHROPIC_API_KEY` 用 Claude `claude-sonnet-4-6`，否則用 `GEMINI_API_KEY`。GitHub Actions 只設 `GEMINI_API_KEY`；本地 `mcss-catalyst-analyst` agent 行 subscription（免 API 費） |
| Insider | SEC EDGAR (免費 API) | Form 4 |
| 推送 | `python-telegram-bot` | $0 |
| 排程 | GitHub Actions | $0 |
| 測試 | `pytest` | — |

---

## 7. Coding 慣例

- **語言**: Python 3.11+
- **風格**: PEP 8，type hints 必須，docstring（Google style）
- **配置**: 所有 criteria 數值放 `config/criteria.yaml`，**唔好 hardcode**
- **密鑰**: 只從環境變數讀（`os.environ`），**永遠唔好 commit secrets**
- **錯誤處理**: 每個 agent try/except，fail 記 log 唔好 crash pipeline
- **數據驗證**: yfinance 數據可能缺失/錯誤，每個數值用前要驗證（唔好假設存在）
- **冪等性**: 同一日重複跑要得出同樣結果（除咗即時數據）
- **唔好過度工程**: 先做到 work，再優化。Phase 1 求 functional

---

## 8. 重要原則（用戶特別強調）

1. **唔好亂作數據** — 所有數字必須嚟自真實 API，缺失就標明，唔好估
2. **準確 > 速度** — 寧願慢，唔好錯
3. **可直接使用嘅輸出** — 報告要清晰、可 action
4. **Criteria ≠ 必賺** — 研究顯示機械式跟 SEPA/CANSLIM 15年平均每股蝕6%。系統係「過濾器」唔係「印鈔機」。報告要保持客觀，唔好過度樂觀，唔好俾明確買賣建議，只做客觀分析
5. **心理護欄優先** — 對呢個用戶，紀律比 criteria 更重要

---

## 9. 部署（GitHub Actions）

`.github/workflows/daily_screen.yml` 排程兩個時段（UTC）：
```yaml
on:
  schedule:
    - cron: '0 13 * * 1-5'   # 21:00 HKT (美股開市前) 週一至五
    - cron: '0 21 * * 1-5'   # 05:00 HKT 次日 (美股收市後)
  workflow_dispatch:          # 容許手動觸發
```

密鑰從 GitHub Secrets 注入：`TELEGRAM_BOT_TOKEN`, `TELEGRAM_CHAT_ID`, `ANTHROPIC_API_KEY`。

---

## 10. 開發階段（按順序）

| Phase | 內容 | 交付 |
|-------|------|------|
| 1 | `data_agent` + `fundamental` (L1+L2) | 每日輸出通過基本篩選嘅 list |
| 2 | `technical` + `rs_rating` + `trend_template` (L3) | + RS + Trend Template 篩選 |
| 3 | `quant_scoring` + `vcp` (L4) | 100分制排名 |
| 4 | `ai_catalyst` (L5) + `report_agent` + Telegram | 每日 TG 推送 Top 5 |
| 5 | GitHub Actions 部署 + `market_gate` | 全自動運行 |
| 6 | Backtesting (vectorbt) | 驗證策略 win rate |
| 7 | （可選）IBKR auto trading | Paper trade 先 |

**建設時逐 Phase 嚟，每個 Phase 完成要可獨立測試先 move on。**

---

## 11. 測試要求

- 每個 indicator（RS, VCP, Trend Template）要有 unit test
- 用已知數據驗證計算正確（例如手動算 RSI 對比）
- Pipeline 要有 integration test（mock 數據跑全流程）
- Telegram 推送要有 dry-run 模式（唔真發，print 出嚟）

---

*最後更新: 2026-05-24 | 規格版本: Criteria v2*
