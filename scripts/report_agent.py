"""MCSS Phase 4b — Format + Push Telegram Report.

Input:  data/l5_top5.csv
        data/pipeline_run.json (metadata)
Output: Telegram message (HTML)
        data/last_report.txt  (archive copy)
"""

import argparse
import html
import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

import pandas as pd
import yaml

ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(ROOT / "scripts"))

from send_telegram import send_message


# ── Market brief blocks ────────────────────────────────────────────────────────

def _load_json_file(path: Path) -> Dict:
    if not path.exists():
        return {}
    try:
        return json.loads(path.read_text())
    except Exception:
        return {}


def _format_sector_strip(brief: Dict) -> str:
    """Two-line sector RS strip: strongest and weakest 3 vs benchmark."""
    sectors = brief.get("sectors", [])
    if not sectors:
        return ""
    def _fmt(s: Dict) -> str:
        return f"{s['sector']} {s['composite']:+.1f}%"
    return "\n".join([
        f"📡 <b>板塊雷達</b> (相對 {brief.get('benchmark', 'SPY')} 綜合強度)",
        "  強: " + " · ".join(_fmt(s) for s in sectors[:3]),
        "  弱: " + " · ".join(_fmt(s) for s in sectors[-3:]),
    ])


def _format_day_trade_section(cfg: Dict) -> str:
    """Day trade candidates block.

    Two framings, chosen by config:
      - day_trade.enabled   : show the section at all
      - day_trade.tradeable : frame as tradeable signals (entry/risk levels).
        Stays false until backtest_daytrade.py clears the launch gates — the
        6-month replay showed the ORB edge was regime-dependent (in-sample
        negative), so the default is an INFORMATIONAL 異動掃描, not a signal.
    """
    dt_cfg = cfg.get("day_trade", {})
    if not dt_cfg.get("enabled", False):
        return ""
    csv_path = ROOT / "data" / "day_trade_candidates.csv"
    if not csv_path.exists():
        return ""
    try:
        df = pd.read_csv(csv_path)
    except Exception:
        return ""
    if df.empty or "ticker" not in df.columns:
        return ""

    summary = _load_json_file(ROOT / "data" / "day_trade_summary.json")
    mode = str(summary.get("mode", "orb"))
    tradeable = bool(dt_cfg.get("tradeable", False))
    risk = dt_cfg.get("risk_per_trade_pct", 1)
    or_minutes = dt_cfg.get("backtest", {}).get("opening_range_minutes", 15)
    max_gap = dt_cfg.get("gap", {}).get("max_gap_pct", 12)

    lines: List[str] = []
    if mode == "orb":
        lines.append("📊 <b>今日異動掃描</b>" + ("" if tradeable else " <i>(資訊參考 · 非交易信號)</i>"))
        for rank, (_, r) in enumerate(df.iterrows(), start=1):
            name = html.escape(str(r.get("long_name", ""))[:30])
            sector = html.escape(str(r.get("sector", "")))
            lines += [
                f"#{rank} <b>{r['ticker']}</b> — {name}" + (f" [{sector}]" if sector else ""),
                f"   今日 {r['change_pct']:+.1f}% | RVOL {r['rvol']:.1f}x | "
                f"ATR {r['atr_pct']:.1f}% (${r['atr']:.2f})",
                f"   收市強度 {r['close_range_pos']:.0f}% | "
                f"今日範圍 ${r['day_low']:.2f}–${r['day_high']:.2f}",
            ]
            catalyst = str(r.get("catalyst_title", "") or "")
            if catalyst and catalyst != "nan":
                lines.append(f"   Catalyst: {html.escape(catalyst)}")
        if tradeable:
            lines.append(f"📏 參考: 開市首{or_minutes}分鐘範圍突破 | 風險 {risk}%/注 | 唔留倉過夜")
        else:
            lines.append("ℹ️ 純異動掃描,留意板塊資金流向。Backtest 未證明穩定 edge,非入場建議。")
    else:  # gap
        lines.append("📊 <b>開市前異動掃描</b>" + ("" if tradeable else " <i>(資訊參考 · 非交易信號)</i>"))
        for rank, (_, r) in enumerate(df.iterrows(), start=1):
            name = html.escape(str(r.get("long_name", ""))[:30])
            lines += [
                f"#{rank} <b>{r['ticker']}</b> — {name}",
                f"   Gap {r['gap_pct']:+.1f}% | PM ${r['pm_price']:.2f} vs 昨收 ${r['prev_close']:.2f}",
                f"   昨日範圍 ${r['day_low']:.2f}–${r['day_high']:.2f}",
            ]
            catalyst = str(r.get("catalyst_title", "") or "")
            if catalyst and catalyst != "nan":
                lines.append(f"   Catalyst: {html.escape(catalyst)}")
        if tradeable:
            lines.append(f"📏 Gap &gt;{max_gap}% 已自動排除 (FOMO 護欄) | 風險 {risk}%/注 | 唔留倉過夜")
        else:
            lines.append(f"ℹ️ 純 gap 掃描 (>{max_gap}% 已隔除),留意異動方向。Backtest 未證明 edge,非入場建議。")
    return "\n".join(lines)


def _format_market_brief_full(brief: Dict) -> str:
    """Sector strip + watch hint + headlines + AI summary — 0-match / HALT days."""
    parts = []
    strip = _format_sector_strip(brief)
    if strip:
        parts.append(strip)
    sectors = brief.get("sectors", [])
    if sectors:
        watch = "、".join(s["sector"] for s in sectors[:3])
        parts.append(f"👀 <b>留意板塊</b>: {watch}")
    headlines = brief.get("headlines", [])
    if headlines:
        lines = ["📰 <b>大市頭條</b>"]
        for h in headlines[:5]:
            title = html.escape(str(h.get("title", ""))[:90])
            source = html.escape(str(h.get("source", "")))
            lines.append(f"  • {title}" + (f" <i>({source})</i>" if source else ""))
        parts.append("\n".join(lines))
    if brief.get("ai_summary"):
        parts.append(f"🧠 <b>大市摘要</b>\n{html.escape(str(brief['ai_summary']))}")
    return "\n\n".join(parts)


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
        lines.append(f"  Catalyst: {html.escape(catalyst_notes[:120])}")
    if earnings_play:
        lines.append("  ⚠️ <b>EARNINGS RISK</b> — 倉位減半，止損收緊")

    return "\n".join(lines)


def _format_swing_compact(top5: pd.DataFrame, pos_cfg: Dict) -> str:
    """One line per swing pick — pre-market recap of the post-market report."""
    if top5.empty:
        return ""
    lines = ["📋 <b>Swing Top 5 簡覽</b> (詳細分析見收市後報告)"]
    for rank, (_, row) in enumerate(top5.iterrows(), start=1):
        ticker = str(row.get("ticker", "?"))
        score = float(row.get("final_score", row.get("total_score", 0)))
        price = float(row.get("price", 0))
        atr = float(row.get("atr", 0))
        sizing = _position_sizing(price, atr, pos_cfg)
        lines.append(
            f"  #{rank} <b>{ticker}</b> {score:.0f}分 | "
            f"入場 ${sizing['entry']:.2f} | 止損 ${sizing['stop']:.2f}"
        )
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

    # ── Pre-market: day trade first, swing as compact recap, then brief ──
    if "pre" in session:
        sections = [f"<b>🌅 MCSS 開市前掃描 — {today_fmt}</b>"]
        dt_block = _format_day_trade_section(cfg)
        if dt_block:
            sections += ["", dt_block]
        compact = _format_swing_compact(top5, pos_cfg)
        if compact:
            sections += ["", compact]
        brief_full = _format_market_brief_full(
            _load_json_file(ROOT / "data" / "market_brief.json"))
        if brief_full:
            sections += ["", brief_full]
        sections += ["", "<i>純系統輸出，非投資建議。所有決定請自行判斷。</i>"]
        return "\n".join(sections)

    # ── Post-market: full swing blocks + day trade watchlist + sector strip ──
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

    # Combined message: day trade candidates + sector strip after swing blocks
    extra_sections = []
    dt_block = _format_day_trade_section(cfg)
    if dt_block:
        extra_sections += [f"{'━'*22}", dt_block]
    strip = _format_sector_strip(_load_json_file(ROOT / "data" / "market_brief.json"))
    if strip:
        extra_sections += [f"{'━'*22}", strip]

    footer = "\n".join([
        f"{'━'*22}",
        "<i>純系統輸出，非投資建議。所有決定請自行判斷。</i>",
    ])

    return "\n".join([header] + blocks + extra_sections + [footer])


# ── Main ───────────────────────────────────────────────────────────────────────

def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--input", default="data/l5_top5.csv")
    parser.add_argument("--output-dir", default="data")
    parser.add_argument("--session", default="post-market")
    parser.add_argument("--halt", action="store_true",
                        help="Gate 0 HALT mode: send market brief only, no stocks")
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()

    input_path = ROOT / args.input
    out_dir = ROOT / args.output_dir
    out_dir.mkdir(exist_ok=True)

    config_path = ROOT / "config" / "criteria.yaml"
    with open(config_path) as f:
        cfg = yaml.safe_load(f)

    # ── Gate 0 HALT: market brief only, no stocks ──
    if args.halt:
        gate = _load_json_file(ROOT / "data" / "market_gate_result.json")
        brief = _load_json_file(ROOT / "data" / "market_brief.json")
        today_fmt = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        session_label = "開市前" if "pre" in args.session else "收市後"
        reason = str(gate.get("reason", ""))
        vix = gate.get("conditions", {}).get("vix_close")

        halt_lines = [
            f"<b>⚠️ MCSS 市場警戒 — {today_fmt} ({session_label})</b>",
            "",
            "Gate 0 HALT — 大市方向唔利，今日唔出個股名單。",
        ]
        if reason:
            halt_lines.append(f"原因: {html.escape(reason)}" + (f" (VIX: {vix})" if vix else ""))
        brief_block = _format_market_brief_full(brief)
        if brief_block:
            halt_lines += ["", brief_block]
        halt_lines += ["", "<i>市場警戒，建議持現金，等待市況改善。純系統輸出，非投資建議。</i>"]

        dry = args.dry_run or not os.environ.get("TELEGRAM_BOT_TOKEN")
        send_message("\n".join(halt_lines), dry_run=dry)
        if not dry:
            print("HALT market-brief message sent.")
        return

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

        empty_lines = [
            f"<b>📊 MCSS 每日篩選 — {today_fmt} ({session_label})</b>",
            "",
            "今日篩選結果：<b>0 隻股票</b>符合入選條件。",
            "",
            "可能原因：",
            *_reasons,
        ]
        # No picks ≠ no value: still deliver day trade candidates (if enabled),
        # sector RS and market headlines
        dt_block = _format_day_trade_section(cfg)
        if dt_block:
            empty_lines += ["", dt_block]
        brief_block = _format_market_brief_full(
            _load_json_file(ROOT / "data" / "market_brief.json"))
        if brief_block:
            empty_lines += ["", brief_block]
        empty_lines += ["", "<i>純系統輸出，非投資建議。</i>"]
        empty_msg = "\n".join(empty_lines)
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
