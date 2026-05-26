"""MCSS Phase 4b — Format + Push Telegram Report.

Input:  data/l5_top5.csv
        data/pipeline_run.json (metadata)
Output: Telegram message (HTML)
        data/last_report.txt  (archive copy)
"""

import argparse
import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Optional

import pandas as pd
import yaml

ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(ROOT / "scripts"))

from send_telegram import send_message


# ── Position sizing ────────────────────────────────────────────────────────────

def _position_sizing(price: float, atr: float, cfg: Dict) -> Dict[str, float]:
    """
    Calculate stop, target, and risk % using ATR-based sizing.
    stop = price - (stop_atr_multiplier * ATR)
    target = price + (2 * stop_distance)   → minimum 2:1 R:R
    """
    stop_mult = float(cfg.get("stop_atr_multiplier", 1.5))
    stop = price - stop_mult * atr
    stop_dist = price - stop
    target = price + 2.0 * stop_dist
    risk_pct = stop_dist / price * 100
    gain_pct = (target - price) / price * 100
    return {
        "entry": round(price, 2),
        "stop": round(stop, 2),
        "target": round(target, 2),
        "risk_pct": round(risk_pct, 1),
        "gain_pct": round(gain_pct, 1),
    }


# ── Report formatting ──────────────────────────────────────────────────────────

def _format_report(
    top5: pd.DataFrame,
    session: str,
    run_date: str,
    universe_size: int,
    l3_passed: int,
    cfg: Dict,
) -> str:
    pos_cfg = cfg.get("position_sizing", {})
    today_fmt = datetime.now(timezone.utc).strftime("%Y-%m-%d")

    lines = [
        f"<b>MCSS Daily Screen — {today_fmt} ({session.replace('-', ' ').title()})</b>",
        f"Universe: {universe_size} → L3: {l3_passed} → Top {len(top5)}",
        "",
        "<b>Top Swing Candidates</b>",
        "",
    ]

    for rank, (_, row) in enumerate(top5.iterrows(), start=1):
        ticker = str(row.get("ticker", "?"))
        long_name = str(row.get("long_name", ticker))
        sector = str(row.get("sector", ""))
        final_score = float(row.get("final_score", row.get("total_score", 0)))
        rs = int(row.get("rs_rating", 0))
        rsi = float(row.get("rsi14", 0))
        price = float(row.get("price", 0))
        atr = float(row.get("atr", 0))
        catalyst_notes = str(row.get("catalyst_notes", ""))
        earnings_play = str(row.get("earnings_play", "False")).lower() == "true"
        dist = float(row.get("dist_from_52wh_pct", 0))

        sizing = _position_sizing(price, atr, pos_cfg)

        lines.append(f"{rank}. <b>{ticker}</b> — {long_name[:40]}")
        lines.append(f"   Score: <b>{final_score:.0f}</b> | RS: {rs} | RSI: {rsi:.1f} | Sector: {sector}")
        lines.append(f"   <b>Entry:</b> ${sizing['entry']:.2f} | <b>Stop:</b> ${sizing['stop']:.2f} (-{sizing['risk_pct']}%) | <b>Target:</b> ${sizing['target']:.2f} (+{sizing['gain_pct']}%)")
        lines.append(f"   From 52w High: -{dist:.1f}%")
        if catalyst_notes and catalyst_notes not in ("dry-run", "GEMINI_API_KEY not set — skipped", ""):
            lines.append(f"   Catalyst: {catalyst_notes[:120]}")
        if earnings_play:
            lines.append(f"   ⚠️ EARNINGS RISK — reduce size, tighten stop")
        lines.append("")

    lines.append("<i>Not financial advice. System output only. Apply own judgment.</i>")
    return "\n".join(lines)


# ── Main ───────────────────────────────────────────────────────────────────────

def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--input", default="data/l5_top5.csv")
    parser.add_argument("--output-dir", default="data")
    parser.add_argument("--session", default="post-market")
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()

    input_path = ROOT / args.input
    out_dir = ROOT / args.output_dir
    out_dir.mkdir(exist_ok=True)

    config_path = ROOT / "config" / "criteria.yaml"
    with open(config_path) as f:
        cfg = yaml.safe_load(f)

    # Load pipeline metadata
    pipeline_meta_path = ROOT / "data" / "pipeline_run.json"
    universe_size, l3_passed = 0, 0
    if pipeline_meta_path.exists():
        with open(pipeline_meta_path) as f:
            meta = json.load(f)
        # Try to get counts from summary files
    tech_summary = ROOT / "data" / "technical_filter_summary.json"
    if tech_summary.exists():
        with open(tech_summary) as f:
            ts = json.load(f)
        universe_size = ts.get("l2_input", 0)
        l3_passed = ts.get("l3_passed", 0)

    print(f"Reading {input_path}...")
    if not input_path.exists():
        print("l5_top5.csv not found. Nothing to report.")
        return

    try:
        top5 = pd.read_csv(input_path)
    except (pd.errors.EmptyDataError, Exception) as exc:
        print(f"Could not read l5_top5.csv ({exc}). Nothing to report.")
        return

    if top5.empty or "ticker" not in top5.columns:
        print("No tickers in Top 5. Skipping report.")
        return

    print(f"Formatting report for {len(top5)} tickers...")
    message = _format_report(
        top5,
        session=args.session,
        run_date=datetime.now(timezone.utc).strftime("%Y-%m-%d"),
        universe_size=universe_size,
        l3_passed=l3_passed,
        cfg=cfg,
    )

    # Archive copy
    report_path = out_dir / "last_report.txt"
    with open(report_path, "w") as f:
        f.write(message)
    print(f"Report saved to {report_path}")

    # Push (or dry-run print)
    dry = args.dry_run or not os.environ.get("TELEGRAM_BOT_TOKEN")
    send_message(message, dry_run=dry)

    if not dry:
        print("Telegram message sent.")


if __name__ == "__main__":
    main()
