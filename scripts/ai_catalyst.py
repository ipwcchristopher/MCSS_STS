"""MCSS Phase 4a — L5 AI Catalyst + Sentiment Scoring.

Input:  data/l4_scored.csv
Output: data/l5_top5.csv
        data/catalyst_details.json

AI analysis priority (graceful fallback):
  1. ANTHROPIC_API_KEY set → Claude claude-sonnet-4-6, 12 tickers in parallel
  2. GEMINI_API_KEY set    → Gemini gemini-1.5-flash, 12 tickers in parallel (via thread pool)
  3. Neither set           → pass through L4 ranking unchanged

Headlines come from catalyst_sources.py (yfinance + Google News RSS keyless;
FINNHUB_API_KEY / NEWSAPI_KEY optionally enrich).
"""

import asyncio
import json
import os
import sys
import argparse
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import Any, Dict, List, Optional

import pandas as pd
import requests
import yaml

ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(ROOT / "scripts"))

import catalyst_sources

_THREAD_POOL = ThreadPoolExecutor(max_workers=16)


# ── SEC EDGAR Form 4 (free, no key required) ──────────────────────────────────

def _check_insider_buying(ticker: str, window_days: int = 30) -> bool:
    """Return True if any Form 4 insider purchase filed in the last window_days."""
    end = datetime.now(timezone.utc).date()
    start = end - timedelta(days=window_days)
    url = (
        "https://efts.sec.gov/LATEST/search-index"
        f"?q=%22{ticker}%22"
        f"&dateRange=custom"
        f"&startdt={start.isoformat()}"
        f"&enddt={end.isoformat()}"
        f"&forms=4"
    )
    try:
        resp = requests.get(url, timeout=10, headers={"User-Agent": "MCSS-Screener contact@example.com"})
        if resp.status_code != 200:
            return False
        data = resp.json()
        hits = data.get("hits", {}).get("total", {})
        count = hits.get("value", 0) if isinstance(hits, dict) else int(hits)
        return count > 0
    except Exception:
        return False


# ── Shared prompt builder ──────────────────────────────────────────────────────

def _build_analysis_prompt(
    ticker: str,
    company_name: str,
    headlines: List[str],
    l5_cfg: Dict,
) -> str:
    """Build the catalyst scoring prompt (shared between Claude and Gemini)."""
    headlines_text = (
        "\n".join(f"- {h}" for h in headlines)
        if headlines
        else "No recent headlines found."
    )
    return (
        f"Analyze recent news for {ticker} ({company_name}).\n\n"
        f"Headlines:\n{headlines_text}\n\n"
        f"Score these signals (0 if absent, specified points if present):\n"
        f"- analyst_upgrade_pts: +{l5_cfg.get('analyst_upgrade_points', 2)} if analyst upgrade in last 7 days\n"
        f"- analyst_downgrade_pts: {l5_cfg.get('analyst_downgrade_points', -2)} if analyst downgrade in last 7 days\n"
        f"- earnings_beat_pts: +{l5_cfg.get('earnings_beat_points', 2)} if recent EPS beat >10%\n"
        f"- news_sentiment_pts: +{l5_cfg.get('news_positive_points', 1)} if overall sentiment positive\n"
        f"- major_negative_pts: {l5_cfg.get('major_negative_news_points', -3)} if SEC probe, recall, major lawsuit\n\n"
        f'Return ONLY valid JSON, no markdown:\n'
        f'{{"analyst_upgrade_pts":0,"analyst_downgrade_pts":0,'
        f'"earnings_beat_pts":0,"news_sentiment_pts":0,"major_negative_pts":0,'
        f'"total_adjustment":0,"reason":"one sentence"}}'
    )


def _parse_ai_response(text: str) -> Dict[str, Any]:
    """Parse JSON from AI response, stripping markdown fences if present."""
    text = text.strip()
    if text.startswith("```"):
        text = text.split("```")[1]
        if text.startswith("json"):
            text = text[4:]
        text = text.strip()
    result = json.loads(text)
    result["adjustment"] = int(result.get("total_adjustment", 0))
    return result


# ── Claude AI analysis (async) ─────────────────────────────────────────────────

async def _analyze_with_claude(
    ticker: str,
    company_name: str,
    headlines: List[str],
    client: Any,  # anthropic.AsyncAnthropic
    cfg: Dict,
) -> Dict[str, Any]:
    """Ask Claude to score catalyst signals. Returns adjustment dict."""
    import anthropic  # imported here so Gemini-only runs don't require the package

    l5_cfg = cfg.get("l5_catalyst", {})
    system = (
        "You are a quantitative equity analyst. "
        "Analyze news headlines and return ONLY valid JSON — no markdown fences."
    )
    user_prompt = _build_analysis_prompt(ticker, company_name, headlines, l5_cfg)

    try:
        response = await client.messages.create(
            model="claude-sonnet-4-6",
            max_tokens=256,
            system=system,
            messages=[{"role": "user", "content": user_prompt}],
        )
        return _parse_ai_response(response.content[0].text)
    except Exception as exc:
        return {"adjustment": 0, "reason": f"Claude error: {exc}"}


# ── Gemini AI analysis (sync, runs in thread pool) ─────────────────────────────

def _analyze_with_gemini_sync(
    ticker: str,
    company_name: str,
    headlines: List[str],
    gemini_key: str,
    cfg: Dict,
) -> Dict[str, Any]:
    """Ask Gemini to score catalyst signals. Blocking — caller offloads to executor."""
    try:
        import google.generativeai as genai
    except ImportError:
        return {"adjustment": 0, "reason": "google-generativeai not installed"}

    genai.configure(api_key=gemini_key)
    model = genai.GenerativeModel("gemini-1.5-flash")
    l5_cfg = cfg.get("l5_catalyst", {})
    prompt = _build_analysis_prompt(ticker, company_name, headlines, l5_cfg)

    try:
        response = model.generate_content(prompt)
        return _parse_ai_response(response.text)
    except Exception as exc:
        return {"adjustment": 0, "reason": f"Gemini error: {exc}"}


# ── Per-ticker async worker ────────────────────────────────────────────────────

async def _process_ticker_async(
    row: "pd.Series",
    cfg: Dict,
    news_window: int,
    max_headlines: int,
    insider_window: int,
    insider_pts: int,
    sector_rs_pts: int,
    vcp_breakout_pts: int,
    claude_client: Optional[Any] = None,   # anthropic.AsyncAnthropic or None
    gemini_key: str = "",
) -> Dict:
    """Run all I/O for one ticker concurrently. Uses Claude if available, else Gemini."""
    ticker = str(row["ticker"])
    company_name = str(row.get("long_name", ticker))
    catalyst_score = 0
    details: Dict[str, Any] = {"ticker": ticker}

    loop = asyncio.get_event_loop()

    # SEC EDGAR (blocking HTTP → thread pool)
    insider_found: bool = await loop.run_in_executor(
        _THREAD_POOL, _check_insider_buying, ticker, insider_window
    )
    if insider_found:
        catalyst_score += insider_pts
        details["insider_buying"] = True
    else:
        details["insider_buying"] = False

    # Sector RS heuristic (no I/O)
    sector = str(row.get("sector", ""))
    strong_sectors = {"Technology", "Communication Services", "Consumer Discretionary", "Industrials"}
    if sector in strong_sectors:
        catalyst_score += sector_rs_pts
        details["sector_rs_strong"] = True
    else:
        details["sector_rs_strong"] = False

    # VCP breakout (no I/O)
    vcp_det = str(row.get("vcp_detected", "False")).lower() == "true"
    vol_ratio = float(row.get("volume_ratio", 0))
    if vcp_det and vol_ratio >= 1.5:
        catalyst_score += vcp_breakout_pts
        details["vcp_breakout"] = True
    else:
        details["vcp_breakout"] = False

    # Free news sources via catalyst_sources (keyless yfinance + Google RSS;
    # Finnhub and NewsAPI used only when their keys are set).
    # (blocking HTTP → thread pool)
    headline_dicts = await loop.run_in_executor(
        _THREAD_POOL, catalyst_sources.get_ticker_news,
        ticker, company_name, news_window, max_headlines,
    )
    headlines: List[str] = catalyst_sources.headlines_to_strings(headline_dicts)
    details["news_count"] = len(headlines)

    # AI analysis — Claude (async) or Gemini (sync via executor)
    if claude_client is not None:
        ai_result = await _analyze_with_claude(ticker, company_name, headlines, claude_client, cfg)
        details["ai_engine"] = "claude-sonnet-4-6"
    elif gemini_key:
        ai_result = await loop.run_in_executor(
            _THREAD_POOL,
            _analyze_with_gemini_sync, ticker, company_name, headlines, gemini_key, cfg,
        )
        details["ai_engine"] = "gemini-1.5-flash"
    else:
        ai_result = {"adjustment": 0, "reason": "no AI key — skipped"}
        details["ai_engine"] = "none"

    catalyst_score += ai_result.get("adjustment", 0)
    details["ai_adjustment"] = ai_result.get("adjustment", 0)
    details["ai_reason"] = ai_result.get("reason", "")

    out_row = row.to_dict()
    out_row["catalyst_score"] = catalyst_score
    out_row["catalyst_notes"] = details.get("ai_reason", "")
    out_row["final_score"] = float(row.get("total_score", 0)) + catalyst_score
    out_row["screened_at_l5"] = datetime.now(timezone.utc).isoformat()

    engine_tag = details.get("ai_engine", "none")
    print(
        f"  ✓ {ticker} [{engine_tag}]: catalyst={catalyst_score:+d}  "
        f"({details.get('ai_reason', '')[:55]})",
        flush=True,
    )
    return {"row": out_row, "details": details}


# ── Async main ─────────────────────────────────────────────────────────────────

async def main_async(args: argparse.Namespace, l4: "pd.DataFrame", cfg: Dict) -> None:
    """Dispatch all tickers in parallel. Claude → Gemini → pass-through fallback."""
    l5_cfg = cfg.get("l5_catalyst", {})
    top_n_final = int(l5_cfg.get("top_n_final", 5))
    insider_window = int(l5_cfg.get("insider_buying_window_days", 30))
    insider_pts = int(l5_cfg.get("insider_buying_points", 1))
    sector_rs_pts = int(l5_cfg.get("sector_rs_strong_points", 1))
    vcp_breakout_pts = int(l5_cfg.get("vcp_breakout_points", 1))
    cs_cfg = cfg.get("catalyst_sources", {})
    news_window = int(cs_cfg.get("ticker_news_window_days", 7))
    max_headlines = int(cs_cfg.get("max_headlines_per_ticker", 8))
    anthropic_key = os.environ.get("ANTHROPIC_API_KEY", "")
    gemini_key = os.environ.get("GEMINI_API_KEY", "")

    out_dir = ROOT / args.output_dir
    out_dir.mkdir(exist_ok=True)

    # ── Dry-run: skip all API calls ──
    if args.dry_run:
        print("DRY RUN — skipping all API calls. Passing through L4 ranking.")
        l4 = l4.copy()
        l4["catalyst_score"] = 0
        l4["catalyst_notes"] = "dry-run"
        l4["final_score"] = l4.get("total_score", 0)
        l4.head(top_n_final).to_csv(out_dir / "l5_top5.csv", index=False)
        with open(out_dir / "catalyst_details.json", "w") as f:
            json.dump({}, f)
        return

    # ── Determine AI engine ──
    if anthropic_key:
        import anthropic
        print(f"AI engine: Claude (claude-sonnet-4-6) — {len(l4)} tickers in parallel")
        claude_ctx = anthropic.AsyncAnthropic(api_key=anthropic_key)
    elif gemini_key:
        print(f"AI engine: Gemini (gemini-1.5-flash) — {len(l4)} tickers in parallel via thread pool")
        claude_ctx = None
    else:
        print("No AI key set (ANTHROPIC_API_KEY / GEMINI_API_KEY). Passing through L4 ranking.")
        l4 = l4.copy()
        l4["catalyst_score"] = 0
        l4["catalyst_notes"] = "no AI key"
        l4["final_score"] = l4.get("total_score", 0)
        l4.head(top_n_final).to_csv(out_dir / "l5_top5.csv", index=False)
        with open(out_dir / "catalyst_details.json", "w") as f:
            json.dump({}, f)
        return

    # ── Dispatch all tickers simultaneously ──
    async def _run_with_client(client: Optional[Any]) -> List[Any]:
        tasks = [
            _process_ticker_async(
                row, cfg, news_window, max_headlines,
                insider_window, insider_pts, sector_rs_pts, vcp_breakout_pts,
                claude_client=client,
                gemini_key=gemini_key if client is None else "",
            )
            for _, row in l4.iterrows()
        ]
        return await asyncio.gather(*tasks, return_exceptions=True)

    if anthropic_key:
        async with claude_ctx as client:
            results = await _run_with_client(client)
    else:
        results = await _run_with_client(None)

    rows: List[Dict] = []
    catalyst_log: Dict[str, Any] = {}
    for res in results:
        if isinstance(res, Exception):
            print(f"  ⚠ ticker task failed: {res}", flush=True)
            continue
        rows.append(res["row"])
        catalyst_log[res["details"]["ticker"]] = res["details"]

    if not rows:
        print("All ticker tasks failed. Writing empty output.")
        pd.DataFrame(columns=["ticker", "total_score", "catalyst_score", "final_score"]).to_csv(
            out_dir / "l5_top5.csv", index=False
        )
        return

    result_df = pd.DataFrame(rows).sort_values("final_score", ascending=False)
    top5_df = result_df.head(top_n_final).reset_index(drop=True)

    top5_df.to_csv(out_dir / "l5_top5.csv", index=False)
    with open(out_dir / "catalyst_details.json", "w") as f:
        json.dump(catalyst_log, f, indent=2)

    print(f"\nL5 result: Top {len(top5_df)} tickers selected")
    for _, r in top5_df[["ticker", "total_score", "catalyst_score", "final_score"]].iterrows():
        print(f"  {r['ticker']:8s}  L4={r['total_score']:.0f}  catalyst={r['catalyst_score']:+d}  final={r['final_score']:.0f}")
    print("  → data/l5_top5.csv")


# ── Sync entry point ───────────────────────────────────────────────────────────

def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--input", default="data/l4_scored.csv")
    parser.add_argument("--output-dir", default="data")
    parser.add_argument("--dry-run", action="store_true", help="Skip all API calls")
    args = parser.parse_args()

    config_path = ROOT / "config" / "criteria.yaml"
    with open(config_path) as f:
        cfg = yaml.safe_load(f)

    input_path = ROOT / args.input
    try:
        l4 = pd.read_csv(input_path)
    except (pd.errors.EmptyDataError, FileNotFoundError):
        l4 = pd.DataFrame()

    if l4.empty:
        print("No tickers in L4 input. Writing empty output.")
        out_dir = ROOT / args.output_dir
        out_dir.mkdir(exist_ok=True)
        pd.DataFrame(columns=["ticker", "total_score", "catalyst_score", "final_score"]).to_csv(
            out_dir / "l5_top5.csv", index=False
        )
        return

    print(f"L4 input: {len(l4)} tickers")
    asyncio.run(main_async(args, l4, cfg))


if __name__ == "__main__":
    main()
