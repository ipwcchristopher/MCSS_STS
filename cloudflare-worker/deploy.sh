#!/usr/bin/env bash
# One-shot deploy for the MCSS Telegram webhook Worker.
#
# DO THESE TWO THINGS FIRST (only you can):
#   1) Create a GitHub fine-grained PAT for repo MCSS_STS:
#        Settings > Developer settings > Fine-grained tokens
#        Repository access: Only select repositories -> MCSS_STS
#        Permissions: Contents = Read and write,  Actions = Read
#      Copy the github_pat_... value (shown once).
#   2) Make sure you can open a browser (for Cloudflare login).
#
# Then run:   cd cloudflare-worker && ./deploy.sh
#
# It loads your Telegram token + chat id from ../.env, generates a webhook
# secret, deploys the Worker, sets all secrets, and registers the Telegram
# webhook. The only thing it asks you to paste is the GitHub PAT (hidden).

set -euo pipefail
cd "$(dirname "$0")"
ENV_FILE="../.env"
WRANGLER="npx --yes wrangler@latest"

echo "==> 1/6 Cloudflare login check"
# NOTE: `wrangler whoami` exits 0 even when NOT logged in, so we must grep its
# output (not rely on exit code) to decide whether login is needed.
if $WRANGLER whoami 2>&1 | grep -qi "not authenticated"; then
  echo "    未登入 Cloudflare — 開瀏覽器登入中..."
  echo "    >>> 喺彈出嘅瀏覽器撳 [Allow],完成後返嚟呢個視窗 <<<"
  $WRANGLER login
fi
# Verify login actually succeeded before continuing (deploy needs it).
if $WRANGLER whoami 2>&1 | grep -qi "not authenticated"; then
  echo "!! 仲未登入到 Cloudflare。請手動行:  npx wrangler login"
  echo "   喺瀏覽器撳 Allow,見到 'Successfully logged in' 之後,再行 ./deploy.sh"
  exit 1
fi
echo "    ✓ 已登入 Cloudflare"

echo "==> 2/6 Loading Telegram token + chat id from $ENV_FILE"
set -a; # shellcheck disable=SC1090
source "$ENV_FILE"; set +a
: "${TELEGRAM_BOT_TOKEN:?TELEGRAM_BOT_TOKEN missing in .env}"
: "${TELEGRAM_CHAT_ID:?TELEGRAM_CHAT_ID missing in .env}"

echo "==> 3/6 Generating webhook secret (stored in .webhook_secret, gitignored)"
[ -f .webhook_secret ] || openssl rand -hex 32 > .webhook_secret
WEBHOOK_SECRET="$(cat .webhook_secret)"

echo "==> 4/6 Deploying Worker (creates it on first run)"
DEPLOY_OUT="$($WRANGLER deploy 2>&1)"
echo "$DEPLOY_OUT"
WORKER_URL="$(printf '%s' "$DEPLOY_OUT" | grep -Eo 'https://[a-zA-Z0-9._-]+\.workers\.dev' | head -1)"
[ -n "$WORKER_URL" ] || { echo "!! Could not detect Worker URL — copy it from above and run README Step 5 manually."; exit 1; }
echo "    Worker URL: $WORKER_URL"

echo "==> 5/6 Setting secrets"
printf '%s' "$TELEGRAM_BOT_TOKEN" | $WRANGLER secret put TELEGRAM_BOT_TOKEN
printf '%s' "$WEBHOOK_SECRET"     | $WRANGLER secret put TELEGRAM_WEBHOOK_SECRET
printf '%s' "$TELEGRAM_CHAT_ID"   | $WRANGLER secret put AUTHORIZED_CHAT_ID
echo "    Paste your GitHub fine-grained PAT (input hidden), then press Enter:"
read -r -s PAT
printf '%s' "$PAT" | $WRANGLER secret put GITHUB_TOKEN
unset PAT

echo "==> 6/6 Registering Telegram webhook"
mask() { sed -E 's/[0-9]{6,}:[A-Za-z0-9_-]+/<TOKEN>/g'; }
curl -s "https://api.telegram.org/bot${TELEGRAM_BOT_TOKEN}/setWebhook" \
  -d "url=${WORKER_URL}" \
  -d "secret_token=${WEBHOOK_SECRET}" | mask
echo ""
echo "    getWebhookInfo:"
curl -s "https://api.telegram.org/bot${TELEGRAM_BOT_TOKEN}/getWebhookInfo" | mask
echo ""
echo "✅ Deployed. In Telegram: send /help, then /run — works even with your Mac off."
echo "   Logs:  npx wrangler tail"
