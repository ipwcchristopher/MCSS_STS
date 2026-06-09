# MCSS Cloud On-Demand Trigger (Cloudflare Worker)

呢個 Worker 令你**熄機都可以喺 Telegram 打 `/run` 觸發 pipeline**。

```
Telegram  --webhook-->  Cloudflare Worker  --repository_dispatch-->  GitHub Actions  --> 報告 push 返 Telegram
```

全部喺雲端,$0(Cloudflare 免費 tier 每日 10 萬次請求,GitHub Actions public repo 免費)。

> ⚠️ **重要:Telegram 只准 webhook 或 polling 二選一。**
> 啟用呢個 webhook 之前,**必須先停咗本地 polling bot**(見最後一步),否則會 409 衝突。

---

## 一次性設定(約 15 分鐘)

### Step 0 — 確認 GitHub Secrets(解決「定時冇 message」)
去 repo → **Settings → Secrets and variables → Actions**,確認呢兩個存在**而且值正確**:

| Secret | 值 |
|--------|-----|
| `TELEGRAM_BOT_TOKEN` | 你 BotFather 個 token(同本地 `.env` 一模一樣,前後唔好有空格/引號)|
| `TELEGRAM_CHAT_ID` | `658982886` |

> 之後 workflow 已改成:live run 如果呢兩個 secret 唔啱會**直接 fail(紅色 ❌)**,唔會再靜靜地 dry-run 乜都唔send。下次 schedule 跑你一睇 Actions 就知啱唔啱。

### Step 1 — 建立 GitHub PAT(俾 Worker 觸發 Actions)
GitHub → **Settings → Developer settings → Fine-grained tokens → Generate new token**
- **Repository access**: Only select repositories → `MCSS_STS`
- **Permissions**:
  - **Contents**: Read and write ← `repository_dispatch` 需要
  - **Actions**: Read ← `/status` 讀 run 狀態需要
- 生成後**抄低個 token**(`github_pat_...`),只會顯示一次。

### Step 2 — 安裝同登入 Cloudflare wrangler
```bash
npm install -g wrangler      # 或 npx wrangler
wrangler login               # 開瀏覽器登入你 Cloudflare 帳戶(免費)
```

### Step 3 — 設定 Worker secrets
```bash
cd cloudflare-worker

# 生成一個 webhook secret(抄低,Step 5 要用)
openssl rand -hex 32

wrangler secret put TELEGRAM_BOT_TOKEN       # 貼你個 bot token
wrangler secret put TELEGRAM_WEBHOOK_SECRET  # 貼上面 openssl 生成嗰串
wrangler secret put GITHUB_TOKEN             # 貼 Step 1 個 PAT
wrangler secret put AUTHORIZED_CHAT_ID       # 輸入 658982886
```

### Step 4 — Deploy
```bash
wrangler deploy
```
完成後會俾你一條 URL,類似:
`https://mcss-telegram-webhook.<your-subdomain>.workers.dev`
**抄低呢條 URL。**

### Step 5 — 同 Telegram 講個 webhook 喺邊(用啱個 secret)
```bash
BOT_TOKEN="<你個 bot token>"
WORKER_URL="https://mcss-telegram-webhook.<your-subdomain>.workers.dev"
WEBHOOK_SECRET="<Step 3 個 openssl secret>"

curl -s "https://api.telegram.org/bot${BOT_TOKEN}/setWebhook" \
  -d "url=${WORKER_URL}" \
  -d "secret_token=${WEBHOOK_SECRET}"
```
應該返 `{"ok":true,...}`。核對:
```bash
curl -s "https://api.telegram.org/bot${BOT_TOKEN}/getWebhookInfo"
```

### Step 6 — 測試
喺 Telegram 打 `/help`、再打 `/run`。
應該即刻收到「🚀 已觸發雲端 pipeline」,GitHub Actions 嗰邊會見到一個 `repository_dispatch` 觸發嘅 run。**呢個唔需要你部機開機。**

---

## 停咗本地 polling bot(必做)
本地 `telegram_bot.py` 同呢個 webhook 唔可以並存。停佢:
```bash
launchctl unload ~/Library/LaunchAgents/com.mcss.telegram-bot.plist
# 確認冇咗:
launchctl list | grep mcss   # 應該冇 output
```
(如果想完全唔再開機自動行:`rm ~/Library/LaunchAgents/com.mcss.telegram-bot.plist`)

如果之後想轉返用本地 polling,要先刪 webhook:
`curl "https://api.telegram.org/bot<TOKEN>/deleteWebhook"`

---

## 日後改 Worker code
改完 `worker.js` 再 `wrangler deploy` 就得,secrets 唔使重設。
睇 log:`wrangler tail`。
