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

def _fmt_pct(val: Any, mult: bool = True) -> str:
    """Format a ratio (0.27 → +27.6%) or pct (27.6 → +27.6%)."""
    try:
        v = float(val)
        v = v * 100 if mult else v
        sign = "+" if v >= 0 else ""
        return f"{sign}{v:.1f}%"
    except (TypeError, ValueError):
        return "N/A"


def _fmt_billions(val: Any) -> str:
    try:
        v = float(val)
        if v >= 1e9:
            return f"${v/1e9:.1f}B"
        if v >= 1e6:
            return f"${v/1e6:.0f}M"
        return f"${v:,.0f}"
    except (TypeError, ValueError):
        return "N/A"


def _format_stock_block(rank: int, row: Any, pos_cfg: Dict) -> str:
    ticker      = str(row.get("ticker", "?"))
    long_name   = str(row.get("long_name", ticker))[:38]
    sector      = str(row.get("sector", ""))
    industry    = str(row.get("industry", ""))
    final_score = float(row.get("final_score", row.get("total_score", 0)))
    rs          = int(row.get("rs_rating", 0))
    rsi         = float(row.get("rsi14", 0))
    price       = float(row.get("price", 0))
    atr         = float(row.get("atr", 0))
    dist        = float(row.get("dist_from_52wh_pct", 0))
    vol_ratio   = float(row.get("volume_ratio", 0))
    vcp         = str(row.get("vcp_detected", "False")).lower() == "true"
    earnings_play = str(row.get("earnings_play", "False")).lower() == "true"
    catalyst_notes = str(row.get("catalyst_notes", ""))

    # Sub-scores
    s_rs   = int(row.get("score_rs", 0))
    s_qual = int(row.get("score_quality", 0))
    s_mom  = int(row.get("score_momentum", 0))
    s_vcp  = int(row.get("score_vcp", 0))
    s_vol  = int(row.get("score_volatility", 0))
    s_val  = int(row.get("score_value", 0))
    s_sqz  = int(row.get("score_short_squeeze_bonus", 0))

    # Fundamentals
    rev_growth = _fmt_pct(row.get("revenue_growth"))
    gross_margin = _fmt_pct(row.get("gross_margins"))
    roe = _fmt_pct(row.get("return_on_equity"))
    fcf = _fmt_billions(row.get("free_cashflow"))
    inst_own = _fmt_pct(row.get("institutional_ownership"))
    fwd_pe = row.get("forward_pe")
    fwd_pe_str = f"{float(fwd_pe):.1f}x" if fwd_pe and str(fwd_pe) not in ("nan", "None") else "N/A"
    mkt_cap = _fmt_billions(row.get("market_cap"))

    sizing = _position_sizing(price, atr, pos_cfg)

    # Technical signal bullets
    rsi_note = "強勢但未過熱" if rsi <= 70 else "接近過熱邊緣，留意"
    dist_note = "接近高位整固" if dist < 10 else ("中位回調" if dist < 20 else "距高位較遠")
    vol_note = f"{vol_ratio:.1f}x 平均" + (" (資金流入)" if vol_ratio >= 1.5 else "")

    lines = [
        f"{'━'*22}",
        f"<b>#{rank} {ticker}</b> — {long_name}",
        f"<b>綜合評分: {final_score:.0f}/105</b>  |  市值: {mkt_cap}",
        f"板塊: {sector} · {industry}",
        "",
        "📈 <b>技術信號</b>",
        f"  • RS評級: <b>{rs}</b>/99 — 跑贏全市 {rs}% 股票",
        f"  • RSI(14): {rsi:.1f} — {rsi_note}",
        f"  • 成交量: {vol_note}",
        f"  • 距52週高: -{dist:.1f}% — {dist_note}",
        f"  • 趨勢模板: ✅ EMA20>50>150>200 多頭排列" + (" | VCP收縮形態" if vcp else ""),
        "",
        "💰 <b>基本面快照</b>",
        f"  • 收入增長: <b>{rev_growth}</b> YoY",
        f"  • 毛利率: {gross_margin}  |  ROE: {roe}",
        f"  • 自由現金流: {fcf}  |  預測PE: {fwd_pe_str}",
        f"  • 機構持股: {inst_own}",
        "",
        "🏆 <b>評分細分</b>",
        f"  RS動能:{s_rs} + 質素:{s_qual} + 動量:{s_mom} + VCP:{s_vcp} + 波幅:{s_vol} + 估值:{s_val}" + (f" + 軋空+{s_sqz}" if s_sqz else ""),
        "",
        "📍 <b>交易設置</b>",
        f"  入場: <b>${sizing['entry']:.2f}</b>",
        f"  止損: ${sizing['stop']:.2f} (-{sizing['risk_pct']}%)  [ATR×1.5 = ${atr:.2f}]",
        f"  目標: ${sizing['target']:.2f} (+{sizing['gain_pct']}%)  [2:1 風險回報]",
    ]

    if catalyst_notes and catalyst_notes not in ("dry-run", "no AI key", ""):
        lines.append(f"  Catalyst: {catalyst_notes[:120]}")
    if earnings_play:
        lines.append("  ⚠️ <b>EARNINGS RISK</b> — 倉位減半，止損收緊")

    return "\n".join(lines)


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
    session_label = "開市前" if "pre" in session else "收市後"

    header = "\n".join([
        f"<b>📊 MCSS 每日篩選 — {today_fmt} ({session_label})</b>",
        f"篩選漏斗: {universe_size}隻 → L3通過: {l3_passed}隻 → Top {len(top5)}",
        "",
        "<b>今日入選股票</b>",
    ])

    blocks = [
        _format_stock_block(rank, row, pos_cfg)
        for rank, (_, row) in enumerate(top5.iterrows(), start=1)
    ]

    footer = "\n".join([
        f"{'━'*22}",
        "<i>純系統輸出，非投資建議。所有決定請自行判斷。</i>",
    ])

    return "\n".join([header] + blocks + [footer])


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

    universe_size, l3_passed = 0, 0
    tech_summary = ROOT / "data" / "technical_filter_summary.json"
    if tech_summary.exists():
        try:
            with open(tech_summary) as f:
                ts = json.load(f)
            universe_size = ts.get("l2_input", 0)
            l3_passed = ts.get("l3_passed", 0)
        except Exception:
            pass

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
        print("No tickers in Top 5. Sending empty-result notification.")
        today_fmt = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        session_label = "開市前" if "pre" in args.session else "收市後"

        _yf_degraded = False
        _quality_file = ROOT / "data" / "universe_quality.json"
        if _quality_file.exists():
            try:
                with open(_quality_file) as _qf:
                    _yf_degraded = json.load(_qf).get("yfinance_degraded", False)
            except Exception:
                pass

        _reasons = ["  • 大市整體偏弱，多數股票跌穿均線"]
        if _yf_degraded:
            _reasons.append("  • 資料源（yfinance）部分數據暫時不可用")
        _reasons.append("  • 當前市況不適合入場，建議持現金觀望")

        empty_msg = "\n".join([
            f"<b>📊 MCSS 每日篩選 — {today_fmt} ({session_label})</b>",
            "",
            "今日篩選結果：<b>0 隻股票</b>符合入選條件。",
            "",
            "可能原因：",
            *_reasons,
            "",
            "<i>純系統輸出，非投資建議。</i>",
        ])
        dry = args.dry_run or not os.environ.get("TELEGRAM_BOT_TOKEN")
        send_message(empty_msg, dry_run=dry)
        if not dry:
            print("Empty-result Telegram notification sent.")
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
