/**
 * MCSS Telegram Webhook -> GitHub Actions relay (Cloudflare Worker).
 *
 * Flow:
 *   Telegram  --(webhook POST)-->  this Worker  --(repository_dispatch)-->  GitHub Actions
 *
 * Because everything runs in Cloudflare's edge + GitHub's cloud, /run works
 * even when your Mac is off. This REPLACES the local polling bot
 * (scripts/telegram_bot.py) — Telegram allows either a webhook OR getUpdates
 * polling, never both, so the local LaunchAgent must be stopped.
 *
 * Required secrets (set via `wrangler secret put <NAME>`):
 *   TELEGRAM_BOT_TOKEN        - to reply to Telegram
 *   TELEGRAM_WEBHOOK_SECRET   - shared secret; Telegram echoes it in a header
 *   GITHUB_TOKEN              - fine-grained PAT: this repo, Contents=write, Actions=read
 *   AUTHORIZED_CHAT_ID        - only this Telegram chat id may trigger runs
 *
 * Vars (wrangler.toml, non-sensitive):
 *   GITHUB_OWNER, GITHUB_REPO
 */

export default {
  async fetch(request, env) {
    if (request.method !== "POST") {
      return new Response("MCSS Telegram webhook is alive. POST only.", { status: 200 });
    }

    // 1) Authenticate the webhook itself: Telegram echoes the secret we set.
    const secret = request.headers.get("X-Telegram-Bot-Api-Secret-Token");
    if (!env.TELEGRAM_WEBHOOK_SECRET || secret !== env.TELEGRAM_WEBHOOK_SECRET) {
      return new Response("forbidden", { status: 403 });
    }

    let update;
    try {
      update = await request.json();
    } catch {
      return new Response("bad request", { status: 400 });
    }

    const msg = update.message || update.edited_message;
    if (!msg || !msg.text) {
      return new Response("ok", { status: 200 }); // ignore non-text updates
    }

    const chatId = msg.chat && msg.chat.id;

    // 2) Only the configured chat may control the bot.
    if (String(chatId) !== String(env.AUTHORIZED_CHAT_ID)) {
      await sendMessage(env, chatId, "⛔ Unauthorized.");
      return new Response("ok", { status: 200 });
    }

    // 3) Parse command (strip optional @botname suffix used in groups).
    const parts = msg.text.trim().split(/\s+/);
    let cmd = parts[0].toLowerCase();
    const at = cmd.indexOf("@");
    if (at !== -1) cmd = cmd.slice(0, at);

    try {
      if (cmd === "/run") {
        let session = (parts[1] || "post-market").toLowerCase();
        if (session !== "pre-market" && session !== "post-market") session = "post-market";
        const ok = await dispatch(env, session);
        await sendMessage(
          env,
          chatId,
          ok
            ? `🚀 已觸發雲端 pipeline (session: <b>${session}</b>)\nGitHub Actions 開始跑緊,完成會自動 push 報告。\n用 /status 睇進度。`
            : "❌ 觸發失敗 — 檢查 Worker 嘅 GITHUB_TOKEN 權限 (需要 Contents: write)。"
        );
      } else if (cmd === "/status") {
        await sendMessage(env, chatId, await latestRun(env));
      } else if (cmd === "/help" || cmd === "/start") {
        await sendMessage(
          env,
          chatId,
          "📊 <b>MCSS Cloud Bot</b>\n\n" +
            "/run — 觸發雲端 post-market pipeline\n" +
            "/run pre-market — 觸發 pre-market\n" +
            "/status — 睇最近一次 GitHub Actions run\n" +
            "/help — 呢個訊息\n\n" +
            "✅ 完全喺雲端跑,你部機熄機都 work。"
        );
      }
      // Unknown commands are ignored silently.
    } catch (e) {
      await sendMessage(env, chatId, "❌ Worker error: " + (e && e.message ? e.message : String(e)));
    }

    return new Response("ok", { status: 200 });
  },
};

/** Fire a repository_dispatch event to trigger the daily_screen.yml workflow. */
async function dispatch(env, session) {
  const url = `https://api.github.com/repos/${env.GITHUB_OWNER}/${env.GITHUB_REPO}/dispatches`;
  const resp = await fetch(url, {
    method: "POST",
    headers: ghHeaders(env, true),
    body: JSON.stringify({ event_type: "mcss_run", client_payload: { session } }),
  });
  return resp.status === 204; // GitHub returns 204 No Content on success
}

/** Report the most recent run of the daily screen workflow. */
async function latestRun(env) {
  const url =
    `https://api.github.com/repos/${env.GITHUB_OWNER}/${env.GITHUB_REPO}` +
    `/actions/workflows/daily_screen.yml/runs?per_page=1`;
  const resp = await fetch(url, { headers: ghHeaders(env, false) });
  if (!resp.ok) return `⚠️ 無法讀取 run 狀態 (HTTP ${resp.status})`;
  const data = await resp.json();
  const run = data.workflow_runs && data.workflow_runs[0];
  if (!run) return "未搵到任何 workflow run。";
  const icon = run.conclusion === "success" ? "✅" : run.conclusion === null ? "⏳" : "❌";
  return (
    `${icon} <b>最近一次 run</b>\n` +
    `狀態: ${run.status}\n` +
    `結論: ${run.conclusion || "進行中"}\n` +
    `觸發: ${run.event}\n` +
    `時間: ${run.created_at}\n` +
    `${run.html_url}`
  );
}

function ghHeaders(env, withContentType) {
  const h = {
    Authorization: `Bearer ${env.GITHUB_TOKEN}`,
    Accept: "application/vnd.github+json",
    "X-GitHub-Api-Version": "2022-11-28",
    "User-Agent": "mcss-cloudflare-worker", // GitHub API rejects requests without UA
  };
  if (withContentType) h["Content-Type"] = "application/json";
  return h;
}

async function sendMessage(env, chatId, text) {
  const url = `https://api.telegram.org/bot${env.TELEGRAM_BOT_TOKEN}/sendMessage`;
  await fetch(url, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({
      chat_id: chatId,
      text,
      parse_mode: "HTML",
      disable_web_page_preview: true,
    }),
  });
}
