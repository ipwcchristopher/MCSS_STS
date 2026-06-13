"""MCSS Market Brief — sector RS ranking + market headlines.

Runs every session (cheap: one 12-ETF batch download + a few RSS calls).
Output: data/market_brief.json — consumed by report_agent.py everywhere:
normal runs render a compact sector strip; 0-match days and Gate 0 HALT
render the full brief so the user always gets sector/news context.
"""

import argparse
import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, List

import pandas as pd
import yaml

ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(ROOT / "scripts"))

import catalyst_sources
from indicators.rs_rating import _download_close_prices

SECTOR_NAMES: Dict[str, str] = {
    "XLK": "Technology",
    "XLY": "Consumer Discretionary",
    "XLV": "Health Care",
    "XLF": "Financials",
    "XLI": "Industrials",
    "XLP": "Consumer Staples",
    "XLE": "Energy",
    "XLU": "Utilities",
    "XLB": "Materials",
    "XLRE": "Real Estate",
    "XLC": "Communication Services",
}


def rank_sectors(
    closes: pd.DataFrame,
    etfs: List[str],
    benchmark: str,
    lookbacks: List[int],
) -> List[Dict]:
    """Rank sector ETFs by composite return relative to the benchmark.

    Pure function — unit-tested. Returns dicts sorted strongest-first:
    {"etf", "sector", "rel_<d>d" (pct points)..., "composite" (pct points)}
    """
    if benchmark not in closes.columns:
        return []
    bench = closes[benchmark].dropna()
    out: List[Dict] = []
    for etf in etfs:
        if etf not in closes.columns:
            continue
        s = closes[etf].dropna()
        rels: Dict[str, float] = {}
        composite_parts: List[float] = []
        for d in lookbacks:
            if len(s) <= d or len(bench) <= d:
                continue
            past, bench_past = float(s.iloc[-1 - d]), float(bench.iloc[-1 - d])
            if past <= 0 or bench_past <= 0:
                continue
            rel = (float(s.iloc[-1]) / past - 1.0) - (float(bench.iloc[-1]) / bench_past - 1.0)
            rels[f"rel_{d}d"] = round(rel * 100, 2)
            composite_parts.append(rel)
        if composite_parts:
            out.append({
                "etf": etf,
                "sector": SECTOR_NAMES.get(etf, etf),
                **rels,
                "composite": round(sum(composite_parts) / len(composite_parts) * 100, 2),
            })
    out.sort(key=lambda x: x["composite"], reverse=True)
    return out


def _ai_summary(headlines: List[Dict], sectors: List[Dict]) -> str:
    """One short Gemini-written market summary. Empty string when unavailable."""
    key = os.environ.get("GEMINI_API_KEY", "")
    if not key or not headlines:
        return ""
    try:
        import google.generativeai as genai
        genai.configure(api_key=key)
        model = genai.GenerativeModel("gemini-1.5-flash")
        heads = "\n".join(f"- {h['title']}" for h in headlines[:5])
        strong = ", ".join(s["sector"] for s in sectors[:3]) or "N/A"
        weak = ", ".join(s["sector"] for s in sectors[-3:]) or "N/A"
        prompt = (
            "You are an objective market analyst. Based on these US market headlines "
            f"and sector relative strength (strong: {strong}; weak: {weak}), write a "
            "2-3 sentence neutral market summary in Cantonese (keep tickers/terms in "
            "English). No buy/sell advice, no hype.\n\nHeadlines:\n" + heads
        )
        return model.generate_content(prompt).text.strip()
    except Exception:
        return ""


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--output-dir", default="data")
    parser.add_argument("--dry-run", action="store_true", help="Skip AI summary")
    args = parser.parse_args()

    out_dir = ROOT / args.output_dir
    out_dir.mkdir(exist_ok=True)

    with open(ROOT / "config" / "criteria.yaml") as f:
        cfg = yaml.safe_load(f)
    mb_cfg = cfg.get("market_brief", {})
    etfs = list(mb_cfg.get("sector_etfs", list(SECTOR_NAMES)))
    benchmark = str(mb_cfg.get("benchmark", "SPY"))
    lookbacks = [int(d) for d in mb_cfg.get("lookback_days", [1, 5, 20])]
    headline_count = int(mb_cfg.get("headline_count", 5))

    print(f"Downloading {len(etfs)} sector ETFs + {benchmark}...")
    closes = _download_close_prices(etfs + [benchmark])
    sectors = rank_sectors(closes, etfs, benchmark, lookbacks)
    if sectors:
        print(f"Sector RS (vs {benchmark}): "
              f"strong={[s['etf'] for s in sectors[:3]]} weak={[s['etf'] for s in sectors[-3:]]}")
    else:
        print("Sector ranking unavailable (download failed?) — writing empty list.")

    print("Fetching market headlines...")
    headlines = catalyst_sources.get_market_news(limit=headline_count)
    print(f"  {len(headlines)} headlines")

    summary = "" if args.dry_run else _ai_summary(headlines, sectors)

    brief = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "benchmark": benchmark,
        "sectors": sectors,
        "headlines": headlines,
        "ai_summary": summary,
    }
    out_path = out_dir / "market_brief.json"
    out_path.write_text(json.dumps(brief, indent=2, ensure_ascii=False))
    print(f"→ {out_path}")


if __name__ == "__main__":
    main()
