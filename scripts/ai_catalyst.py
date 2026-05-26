"""MCSS Phase 4a — L5 AI Catalyst + Sentiment Scoring.

Input:  data/l4_scored.csv
Output: data/l5_top5.csv
        data/catalyst_details.json

API keys (env vars, all optional — script degrades gracefully if missing):
  GEMINI_API_KEY   — Google Gemini for news analysis
  NEWSAPI_KEY      — NewsAPI for recent headlines
"""

import json
import os
import sys
import argparse
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import pandas as pd
import requests
import yaml

ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(ROOT / "scripts"))


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


# ── NewsAPI ────────────────────────────────────────────────────────────────────

def _fetch_news(
    ticker: str,
    company_name: str,
    newsapi_key: str,
    window_days: int = 7,
) -> List[str]:
    """Return list of recent headline strings for the ticker."""
    end = datetime.now(timezone.utc)
    start = end - timedelta(days=window_days)
    query = f"{ticker} OR \"{company_name}\"" if company_name else ticker

    url = "https://newsapi.org/v2/everything"
    params = {
        "q": query,
        "from": start.strftime("%Y-%m-%d"),
        "to": end.strftime("%Y-%m-%d"),
        "language": "en",
        "sortBy": "relevancy",
        "pageSize": 10,
        "apiKey": newsapi_key,
    }
    try:
        resp = requests.get(url, params=params, timeout=15)
        if resp.status_code != 200:
            return []
        articles = resp.json().get("articles", [])
        return [f"{a.get('title', '')} — {a.get('description', '')}" for a in articles[:8]]
    except Exception:
        return []


# ── Gemini analysis ────────────────────────────────────────────────────────────

def _analyze_with_gemini(
    ticker: str,
    company_name: str,
    headlines: List[str],
    gemini_key: str,
    cfg: Dict,
) -> Dict[str, Any]:
    """
    Ask Gemini to score catalyst signals from headlines.
    Returns a dict with adjustment points and reasoning.
    """
    try:
        import google.generativeai as genai
    except ImportError:
        return {"adjustment": 0, "reason": "google-generativeai not installed", "raw": {}}

    genai.configure(api_key=gemini_key)
    model = genai.GenerativeModel("gemini-1.5-flash")

    headlines_text = "\n".join(f"- {h}" for h in headlines) if headlines else "No recent headlines found."

    l5_cfg = cfg.get("l5_catalyst", {})
    prompt = f"""You are a quantitative equity analyst. Analyze these recent news headlines for {ticker} ({company_name}) and return a JSON scoring object.

Headlines:
{headlines_text}

Score the following signals (use 0 if signal absent, use the specified points if present):
- analyst_upgrade_7d: +{l5_cfg.get('analyst_upgrade_points', 2)} if analyst upgrade in last 7 days
- analyst_downgrade_7d: {l5_cfg.get('analyst_downgrade_points', -2)} if analyst downgrade in last 7 days
- earnings_beat: +{l5_cfg.get('earnings_beat_points', 2)} if recent earnings beat >10%
- news_sentiment_positive: +{l5_cfg.get('news_positive_points', 1)} if overall news sentiment positive
- major_negative_news: {l5_cfg.get('major_negative_news_points', -3)} if SEC investigation, product recall, major lawsuit, fraud allegation

Return ONLY valid JSON, no markdown:
{{
  "analyst_upgrade_pts": 0,
  "analyst_downgrade_pts": 0,
  "earnings_beat_pts": 0,
  "news_sentiment_pts": 0,
  "major_negative_pts": 0,
  "total_adjustment": 0,
  "reason": "one sentence summary of key catalysts or risks"
}}"""

    try:
        response = model.generate_content(prompt)
        text = response.text.strip()
        # Strip markdown fences if present
        if text.startswith("```"):
            text = text.split("```")[1]
            if text.startswith("json"):
                text = text[4:]
        result = json.loads(text)
        result["adjustment"] = int(result.get("total_adjustment", 0))
        return result
    except Exception as exc:
        return {"adjustment": 0, "reason": f"Gemini error: {exc}", "raw": {}}


# ── Main ───────────────────────────────────────────────────────────────────────

def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--input", default="data/l4_scored.csv")
    parser.add_argument("--output-dir", default="data")
    parser.add_argument("--dry-run", action="store_true", help="Skip API calls, pass through L4 ranking")
    args = parser.parse_args()

    input_path = ROOT / args.input
    out_dir = ROOT / args.output_dir
    out_dir.mkdir(exist_ok=True)

    config_path = ROOT / "config" / "criteria.yaml"
    with open(config_path) as f:
        cfg = yaml.safe_load(f)
    l5_cfg = cfg.get("l5_catalyst", {})
    top_n_final = int(l5_cfg.get("top_n_final", 5))
    insider_window = int(l5_cfg.get("insider_buying_window_days", 30))
    insider_pts = int(l5_cfg.get("insider_buying_points", 1))
    sector_rs_pts = int(l5_cfg.get("sector_rs_strong_points", 1))
    vcp_breakout_pts = int(l5_cfg.get("vcp_breakout_points", 1))

    gemini_key = os.environ.get("GEMINI_API_KEY", "")
    newsapi_key = os.environ.get("NEWSAPI_KEY", "")

    print(f"Reading {input_path}...")
    try:
        l4 = pd.read_csv(input_path)
    except (pd.errors.EmptyDataError, FileNotFoundError):
        l4 = pd.DataFrame()

    if l4.empty:
        print("No tickers in L4 input. Writing empty output.")
        # Write a header-only CSV so downstream scripts don't crash on empty read
        pd.DataFrame(columns=["ticker", "total_score", "catalyst_score", "final_score"]).to_csv(
            out_dir / "l5_top5.csv", index=False
        )
        return

    tickers = l4["ticker"].tolist()
    print(f"L4 input: {len(tickers)} tickers")

    if args.dry_run:
        print("DRY RUN — skipping all API calls. Using L4 ranking as-is.")

    catalyst_log: Dict[str, Any] = {}
    rows = []

    for _, row in l4.iterrows():
        ticker = str(row["ticker"])
        company_name = str(row.get("long_name", ticker))
        catalyst_score = 0
        details: Dict[str, Any] = {"ticker": ticker}

        if not args.dry_run:
            # ── Insider buying (SEC EDGAR, free) ──────────────────────────
            insider_found = _check_insider_buying(ticker, window_days=insider_window)
            if insider_found:
                catalyst_score += insider_pts
                details["insider_buying"] = True
                print(f"  {ticker}: insider buying signal +{insider_pts}")
            else:
                details["insider_buying"] = False

            # ── Sector RS strength ────────────────────────────────────────
            sector = str(row.get("sector", ""))
            strong_sectors = {"Technology", "Communication Services", "Consumer Discretionary", "Industrials"}
            if sector in strong_sectors:
                catalyst_score += sector_rs_pts
                details["sector_rs_strong"] = True
            else:
                details["sector_rs_strong"] = False

            # ── VCP breakout signal ───────────────────────────────────────
            vcp_det = str(row.get("vcp_detected", "False")).lower() == "true"
            vol_ratio = float(row.get("volume_ratio", 0))
            if vcp_det and vol_ratio >= 1.5:
                catalyst_score += vcp_breakout_pts
                details["vcp_breakout"] = True
            else:
                details["vcp_breakout"] = False

            # ── News + Gemini analysis ────────────────────────────────────
            if gemini_key:
                headlines: List[str] = []
                if newsapi_key:
                    print(f"  {ticker}: fetching news...")
                    headlines = _fetch_news(ticker, company_name, newsapi_key)
                    details["news_count"] = len(headlines)

                print(f"  {ticker}: analyzing with Gemini...")
                gemini_result = _analyze_with_gemini(ticker, company_name, headlines, gemini_key, cfg)
                catalyst_score += gemini_result.get("adjustment", 0)
                details["gemini_adjustment"] = gemini_result.get("adjustment", 0)
                details["gemini_reason"] = gemini_result.get("reason", "")
                details["gemini_raw"] = {k: v for k, v in gemini_result.items()
                                         if k not in ("adjustment", "reason", "raw")}
            else:
                details["gemini_adjustment"] = 0
                details["gemini_reason"] = "GEMINI_API_KEY not set — skipped"
                if not newsapi_key:
                    details["news_count"] = 0
        else:
            details.update({
                "insider_buying": None,
                "sector_rs_strong": None,
                "vcp_breakout": None,
                "gemini_adjustment": 0,
                "gemini_reason": "dry-run",
            })

        out_row = row.to_dict()
        out_row["catalyst_score"] = catalyst_score
        out_row["catalyst_notes"] = details.get("gemini_reason", "")
        out_row["final_score"] = float(row.get("total_score", 0)) + catalyst_score
        out_row["screened_at_l5"] = datetime.now(timezone.utc).isoformat()
        rows.append(out_row)
        catalyst_log[ticker] = details

    # ── Rank and take Top N ───────────────────────────────────────────────
    result_df = pd.DataFrame(rows).sort_values("final_score", ascending=False)
    top5_df = result_df.head(top_n_final).reset_index(drop=True)

    top5_df.to_csv(out_dir / "l5_top5.csv", index=False)

    with open(out_dir / "catalyst_details.json", "w") as f:
        json.dump(catalyst_log, f, indent=2)

    print(f"\nL5 result: Top {len(top5_df)} tickers selected")
    for _, r in top5_df[["ticker", "total_score", "catalyst_score", "final_score"]].iterrows():
        print(f"  {r['ticker']:8s}  L4={r['total_score']:.0f}  catalyst={r['catalyst_score']:+d}  final={r['final_score']:.0f}")
    print(f"  → data/l5_top5.csv")


if __name__ == "__main__":
    main()
