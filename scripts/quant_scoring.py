"""MCSS Phase 3 — L4 Quant Factor Scoring.

Input:  data/l3_technical_passed.csv
Output: data/l4_scored.csv
        data/quant_scoring_summary.json
"""

import argparse
import json
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, List, Optional, Tuple

import pandas as pd
import yfinance as yf
import yaml

ROOT = Path(__file__).parent.parent


# ── Momentum helpers ───────────────────────────────────────────────────────────

def _download_recent_closes(tickers: List[str], period: str = "35d") -> Dict[str, pd.Series]:
    """Batch-download recent closing prices. Returns {ticker: close_series}."""
    if not tickers:
        return {}

    try:
        raw = yf.download(
            tickers,
            period=period,
            auto_adjust=True,
            progress=False,
            threads=True,
        )
    except Exception as exc:
        print(f"[quant_scoring] momentum download failed: {exc}")
        return {t: pd.Series(dtype=float) for t in tickers}

    if raw.empty:
        return {t: pd.Series(dtype=float) for t in tickers}

    result: Dict[str, pd.Series] = {}

    if isinstance(raw.columns, pd.MultiIndex):
        lvl0 = set(raw.columns.get_level_values(0).unique())
        ohlcv = {"Open", "High", "Low", "Close", "Volume"}

        if lvl0 & ohlcv:
            # (price_type, ticker)
            for t in tickers:
                try:
                    result[t] = raw["Close"][t].dropna()
                except Exception:
                    result[t] = pd.Series(dtype=float)
        else:
            # (ticker, price_type)
            for t in tickers:
                try:
                    result[t] = raw[t]["Close"].dropna()
                except Exception:
                    result[t] = pd.Series(dtype=float)
    else:
        col = "Close" if "Close" in raw.columns else raw.columns[0]
        result[tickers[0]] = raw[col].dropna()
        for t in tickers[1:]:
            result[t] = pd.Series(dtype=float)

    return result


def _momentum_composite(close: pd.Series) -> Tuple[Optional[float], Optional[float], Optional[float], float]:
    """Return (perf_5d, perf_10d, perf_20d, composite). None if insufficient data."""
    n = len(close)
    cur = float(close.iloc[-1]) if n > 0 else None

    def perf(lookback: int) -> Optional[float]:
        if cur is None or n < lookback + 1:
            return None
        past = float(close.iloc[-(lookback + 1)])
        return (cur / past - 1.0) if past > 0 else None

    p5, p10, p20 = perf(5), perf(10), perf(20)

    vals = [v for v in [p5, p10, p20] if v is not None]
    composite = 0.5 * (p5 or 0) + 0.3 * (p10 or 0) + 0.2 * (p20 or 0) if vals else 0.0
    return p5, p10, p20, composite


# ── Sub-score calculators ──────────────────────────────────────────────────────

def _rs_score(rs_rating: int, cfg: Dict) -> int:
    high_thresh = int(cfg.get("rs_rating_high_threshold", 80))
    high_pts = int(cfg.get("rs_rating_high_points", 20))
    low_pts = int(cfg.get("rs_rating_low_points", 14))
    return high_pts if rs_rating >= high_thresh else low_pts


def _quality_score(row: pd.Series) -> Tuple[int, int, int]:
    """Returns (gross_margin_pts, roe_pts, fcf_pts)."""
    # Gross margin (0-10)
    gm = row.get("gross_margins", None)
    if pd.isna(gm) or gm is None:
        gm_pts = 0
    elif gm >= 0.60:
        gm_pts = 10
    elif gm >= 0.45:
        gm_pts = 7
    elif gm >= 0.35:
        gm_pts = 5
    else:
        gm_pts = 2

    # ROE (0-8)
    roe = row.get("return_on_equity", None)
    if pd.isna(roe) or roe is None:
        roe_pts = 0
    elif roe >= 0.20:
        roe_pts = 8
    elif roe >= 0.10:
        roe_pts = 5
    elif roe >= 0:
        roe_pts = 2
    else:
        roe_pts = 0

    # FCF positive (0-7)
    fcf = row.get("free_cashflow", None)
    if pd.isna(fcf) or fcf is None:
        fcf_pts = 3  # unknown, neutral
    elif float(fcf) > 0:
        fcf_pts = 7
    else:
        fcf_pts = 0

    return gm_pts, roe_pts, fcf_pts


def _momentum_score(composite: float, max_pts: int = 25) -> int:
    """Map momentum composite return → 0-25 pts."""
    if composite >= 0.10:
        return max_pts
    elif composite >= 0.05:
        return int(max_pts * 0.80)
    elif composite >= 0.02:
        return int(max_pts * 0.60)
    elif composite >= 0.00:
        return int(max_pts * 0.32)
    else:
        return 0


def _vcp_score_pts(vcp_score: int, max_pts: int = 15) -> int:
    return min(round(vcp_score / 100 * max_pts), max_pts)


def _volatility_score(atr: float, price: float, max_pts: int = 10) -> int:
    """ATR/Price sweet spot for swing trading: 2-5%."""
    if price <= 0 or atr <= 0:
        return 0
    atr_pct = atr / price
    if 0.02 <= atr_pct <= 0.05:
        return max_pts
    elif 0.01 <= atr_pct < 0.02 or 0.05 < atr_pct <= 0.08:
        return int(max_pts * 0.60)
    elif atr_pct > 0.08:
        return int(max_pts * 0.20)
    else:
        return int(max_pts * 0.30)


def _value_score(row: pd.Series) -> int:
    """PEG-first, fallback to P/S."""
    peg = row.get("peg_ratio", None)
    if not (pd.isna(peg) or peg is None) and float(peg) > 0:
        peg = float(peg)
        if peg < 1.0:
            return 5
        elif peg <= 2.0:
            return 3
        else:
            return 1

    ps = row.get("price_to_sales", None)
    if not (pd.isna(ps) or ps is None) and float(ps) > 0:
        ps = float(ps)
        if ps < 5:
            return 5
        elif ps <= 15:
            return 3
        else:
            return 1

    return 1  # unknown, give minimum non-zero


def _short_squeeze_bonus(row: pd.Series, composite: float, cfg: Dict) -> int:
    min_float_pct = float(cfg.get("short_squeeze_min_float_pct", 15)) / 100
    bonus_max = int(cfg.get("short_squeeze_bonus_max", 5))
    short_pct = row.get("short_percent_of_float", None)
    if pd.isna(short_pct) or short_pct is None:
        return 0
    if float(short_pct) > min_float_pct and composite > 0.10:
        return bonus_max
    return 0


# ── Main ───────────────────────────────────────────────────────────────────────

def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--input", default="data/l3_technical_passed.csv")
    parser.add_argument("--output-dir", default="data")
    args = parser.parse_args()

    input_path = ROOT / args.input
    out_dir = ROOT / args.output_dir
    out_dir.mkdir(exist_ok=True)

    config_path = ROOT / "config" / "criteria.yaml"
    with open(config_path) as f:
        cfg = yaml.safe_load(f)
    l4_cfg = cfg.get("l4_scoring", {})

    min_score = int(l4_cfg.get("min_total_score", 60))
    top_n = int(l4_cfg.get("top_n", 12))

    print(f"Reading {input_path}...")
    try:
        l3 = pd.read_csv(input_path)
    except (pd.errors.EmptyDataError, Exception):
        l3 = pd.DataFrame()
    tickers = l3["ticker"].tolist() if not l3.empty else []
    print(f"L3 input tickers: {len(tickers)}")

    if not tickers:
        print("No tickers to score. Exiting.")
        pd.DataFrame().to_csv(out_dir / "l4_scored.csv", index=False)
        return

    # ── Download recent closes for momentum ──────────────────────────────
    print("Downloading recent closes for momentum calculation...")
    closes = _download_recent_closes(tickers, period="35d")

    # ── Score each ticker ─────────────────────────────────────────────────
    scored_rows: List[Dict] = []
    scored_at = datetime.now(timezone.utc).isoformat()

    for _, row in l3.iterrows():
        ticker = row["ticker"]

        # Momentum
        close_series = closes.get(ticker, pd.Series(dtype=float))
        p5, p10, p20, composite = _momentum_composite(close_series)

        # Sub-scores
        rs_pts = _rs_score(int(row.get("rs_rating", 0)), l4_cfg)
        gm_pts, roe_pts, fcf_pts = _quality_score(row)
        quality_pts = gm_pts + roe_pts + fcf_pts
        momentum_pts = _momentum_score(composite, int(l4_cfg.get("momentum_score_max", 25)))
        vcp_pts = _vcp_score_pts(int(row.get("vcp_score", 0)), int(l4_cfg.get("vcp_tightness_score_max", 15)))
        vol_pts = _volatility_score(float(row.get("atr", 0)), float(row.get("price", 1)))
        value_pts = _value_score(row)
        bonus_pts = _short_squeeze_bonus(row, composite, l4_cfg)

        total = rs_pts + quality_pts + momentum_pts + vcp_pts + vol_pts + value_pts + bonus_pts

        out_row = row.to_dict()
        out_row.update({
            "score_rs": rs_pts,
            "score_quality": quality_pts,
            "score_quality_gross_margin": gm_pts,
            "score_quality_roe": roe_pts,
            "score_quality_fcf": fcf_pts,
            "score_momentum": momentum_pts,
            "perf_5d": round(p5, 4) if p5 is not None else None,
            "perf_10d": round(p10, 4) if p10 is not None else None,
            "perf_20d": round(p20, 4) if p20 is not None else None,
            "momentum_composite": round(composite, 4),
            "score_vcp": vcp_pts,
            "score_volatility": vol_pts,
            "score_value": value_pts,
            "score_short_squeeze_bonus": bonus_pts,
            "total_score": total,
            "l4_pass": total >= min_score,
            "scored_at": scored_at,
        })
        scored_rows.append(out_row)

    # ── Filter + rank ─────────────────────────────────────────────────────
    all_df = pd.DataFrame(scored_rows).sort_values("total_score", ascending=False)
    passed_df = all_df[all_df["l4_pass"]].head(top_n).reset_index(drop=True)

    all_df.to_csv(out_dir / "l4_all_scored.csv", index=False)
    passed_df.to_csv(out_dir / "l4_scored.csv", index=False)

    # ── Summary ───────────────────────────────────────────────────────────
    top_list = passed_df[["ticker", "total_score"]].to_dict("records") if not passed_df.empty else []
    summary = {
        "run_at": scored_at,
        "l3_input": len(l3),
        "l4_candidates": len(all_df),
        "l4_passed": len(passed_df),
        "min_score_threshold": min_score,
        "top_n_requested": top_n,
        "top_tickers": top_list,
        "score_breakdown_example": (
            {
                "ticker": scored_rows[0]["ticker"],
                "rs": scored_rows[0]["score_rs"],
                "quality": scored_rows[0]["score_quality"],
                "momentum": scored_rows[0]["score_momentum"],
                "vcp": scored_rows[0]["score_vcp"],
                "volatility": scored_rows[0]["score_volatility"],
                "value": scored_rows[0]["score_value"],
                "bonus": scored_rows[0]["score_short_squeeze_bonus"],
                "total": scored_rows[0]["total_score"],
            }
            if scored_rows else {}
        ),
    }
    with open(out_dir / "quant_scoring_summary.json", "w") as f:
        json.dump(summary, f, indent=2)

    print(f"L4 scoring: {len(passed_df)}/{len(l3)} passed (score >= {min_score})")
    if not passed_df.empty:
        print(f"  Top {min(top_n, len(passed_df))} tickers:")
        for _, r in passed_df[["ticker", "total_score"]].iterrows():
            print(f"    {r['ticker']:8s}  {r['total_score']:.0f} pts")
    print(f"  → data/l4_scored.csv ({len(passed_df)} rows)")


if __name__ == "__main__":
    main()
