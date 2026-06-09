"""
Apply L1 Universe filter + L2 Fundamental filter to raw universe data.

Reads data from fetch_universe.py output, applies MCSS criteria from
config/criteria.yaml, and produces filtered output files.

Usage:
    python scripts/fundamental_filter.py [--input data/universe_raw.csv]
    python scripts/fundamental_filter.py --input data/test_universe.csv --output-dir data/test
"""
import argparse
import json
from datetime import datetime, timedelta
from pathlib import Path
from typing import Optional

import pandas as pd
import yaml
import yfinance as yf


# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

def load_config(config_path: str = "config/criteria.yaml") -> dict:
    with open(config_path) as f:
        return yaml.safe_load(f)


# ---------------------------------------------------------------------------
# L1 — Universe Filter
# ---------------------------------------------------------------------------

# quote_type values from yfinance that are NOT tradeable equities
_EXCLUDED_QUOTE_TYPES = {
    "ETF", "MUTUALFUND", "INDEX", "FUTURE", "OPTION", "CURRENCY",
    "CRYPTOCURRENCY", "WARRANT",
}

# Known Chinese ADR suffixes / name patterns (best-effort heuristic)
_CHINA_ADR_KEYWORDS = ("Holdings", "Group Ltd", "Co., Ltd", "Technology Ltd")
_CHINA_ADR_INDUSTRIES = {
    "Internet Content & Information",  # usually Chinese internet
}


def _is_excluded_type(row: pd.Series) -> bool:
    """Return True if the security is an ETF, fund, warrant, or similar non-equity."""
    qt = str(row.get("quote_type", "") or "").upper()
    return qt in _EXCLUDED_QUOTE_TYPES


def _check_earnings_play(earnings_date_str: str, window_days: int = 5) -> bool:
    """Return True if earnings are within the next `window_days` trading days."""
    if not earnings_date_str or earnings_date_str in ("None", "nan", ""):
        return False
    try:
        # yfinance may store as "Timestamp('2026-05-28 00:00:00')" or ISO
        raw = str(earnings_date_str).strip()
        if "Timestamp" in raw:
            raw = raw.replace("Timestamp('", "").replace("')", "").split()[0]
        elif "[" in raw:
            # list form: take first date
            raw = raw.strip("[]").split(",")[0].strip().strip("'")
        dt = datetime.fromisoformat(raw[:10])
        today = datetime.utcnow().replace(hour=0, minute=0, second=0, microsecond=0)
        delta = (dt - today).days
        return 0 <= delta <= window_days
    except Exception:
        return False


def _check_revenue_acceleration(ticker: str) -> Optional[bool]:
    """Return True if current quarter YoY revenue growth >= prior quarter's.

    Fetches quarterly_income_stmt for 6 quarters (min required).
    Returns None when insufficient data — caller treats as unknown (soft pass).
    """
    try:
        stmt = yf.Ticker(ticker).quarterly_income_stmt
        if stmt is None or stmt.empty:
            return None
        rev_key = next(
            (k for k in stmt.index if "revenue" in str(k).lower() and "total" in str(k).lower()),
            None,
        )
        if rev_key is None:
            rev_key = next((k for k in stmt.index if "revenue" in str(k).lower()), None)
        if rev_key is None:
            return None
        rev = stmt.loc[rev_key].sort_index(ascending=False).dropna()
        if len(rev) < 6:
            return None
        q = [float(rev.iloc[i]) for i in range(6)]
        if q[4] <= 0 or q[5] <= 0:
            return None
        yoy_now = q[0] / q[4] - 1.0
        yoy_prev = q[1] / q[5] - 1.0
        return yoy_now >= yoy_prev
    except Exception:
        return None


def _check_eps_acceleration(ticker: str) -> Optional[bool]:
    """Return True if EPS growth accelerated over the last 2 consecutive quarters.

    Uses diluted EPS from quarterly_income_stmt; requires 5 quarters.
    Returns None when insufficient data — caller treats as unknown (soft pass).
    """
    try:
        stmt = yf.Ticker(ticker).quarterly_income_stmt
        if stmt is None or stmt.empty:
            return None
        eps_key = next(
            (k for k in stmt.index if "diluted" in str(k).lower() and "eps" in str(k).lower()),
            None,
        )
        if eps_key is None:
            eps_key = next((k for k in stmt.index if "eps" in str(k).lower()), None)
        if eps_key is None:
            return None
        eps = stmt.loc[eps_key].sort_index(ascending=False).dropna()
        if len(eps) < 5:
            return None
        e = [float(eps.iloc[i]) for i in range(5)]
        if e[3] == 0 or e[4] == 0:
            return None
        g_recent = (e[0] - e[4]) / abs(e[4])
        g_prior = (e[1] - e[3]) / abs(e[3])
        return g_recent > g_prior
    except Exception:
        return None


def apply_l1_filter(
    df: pd.DataFrame, cfg: dict
) -> tuple[pd.DataFrame, pd.DataFrame]:
    """
    Apply L1 universe filter.

    Returns (passed_df, excluded_df). excluded_df has a 'filter_reason' column.
    Both DataFrames include an 'earnings_play' boolean flag.
    """
    l1 = cfg["l1_universe"]
    ep = cfg["earnings_play_pool"]

    min_price = l1["min_price"]
    min_volume = l1["min_avg_volume_20d"]
    min_mktcap = l1["min_market_cap"]
    max_dist_pct = l1["max_distance_from_52w_high_pct"] / 100  # → decimal

    rows_passed = []
    rows_excluded = []

    for _, row in df.iterrows():
        reasons = []

        # --- Excluded security type ---
        if _is_excluded_type(row):
            reasons.append(f"excluded_type:{row.get('quote_type', 'unknown')}")

        # --- Price ---
        price = row.get("price")
        if price is None or pd.isna(price):
            reasons.append("price:missing")
        elif float(price) < min_price:
            reasons.append(f"price:{price:.2f}<{min_price}")

        # --- Volume ---
        vol = row.get("avg_volume_20d")
        if vol is None or pd.isna(vol):
            reasons.append("volume:missing")
        elif float(vol) < min_volume:
            reasons.append(f"volume:{int(vol)}<{min_volume}")

        # --- Market cap ---
        mktcap = row.get("market_cap")
        if mktcap is None or pd.isna(mktcap):
            reasons.append("market_cap:missing")
        elif float(mktcap) < min_mktcap:
            reasons.append(f"market_cap:{int(mktcap)}<{min_mktcap}")

        # --- Distance from 52-week high ---
        hi52 = row.get("fifty_two_week_high")
        if hi52 is None or pd.isna(hi52) or float(hi52) == 0:
            reasons.append("52w_high:missing")
        elif price is not None and not pd.isna(price):
            dist = 1.0 - float(price) / float(hi52)
            if dist > max_dist_pct:
                reasons.append(f"dist_from_52wh:{dist*100:.1f}%>{max_dist_pct*100:.0f}%")

        # --- Earnings play flag (keep in pool, don't exclude) ---
        earnings_play = _check_earnings_play(
            str(row.get("earnings_date", "")), ep["earnings_within_days"]
        )

        out = row.to_dict()
        out["earnings_play"] = earnings_play
        out["l1_pass"] = len(reasons) == 0

        if reasons:
            out["filter_reason"] = "; ".join(reasons)
            rows_excluded.append(out)
        else:
            out["filter_reason"] = ""
            rows_passed.append(out)

    passed = pd.DataFrame(rows_passed) if rows_passed else pd.DataFrame()
    excluded = pd.DataFrame(rows_excluded) if rows_excluded else pd.DataFrame()
    return passed, excluded


# ---------------------------------------------------------------------------
# L2 — Fundamental Filter
# ---------------------------------------------------------------------------

def apply_l2_filter(
    df: pd.DataFrame, cfg: dict
) -> tuple[pd.DataFrame, pd.DataFrame]:
    """
    Apply L2 fundamental filter on L1-passed stocks.

    Returns (passed_df, excluded_df). excluded_df has 'filter_reason'.
    Note: revenue/EPS acceleration uses yfinance quarterly_income_stmt.
    Returns None when data is unavailable (soft pass); False means confirmed
    deceleration and triggers a hard exclude.
    """
    l2 = cfg["l2_fundamental"]

    min_rev_growth = l2["min_revenue_growth_yoy_pct"] / 100
    min_gross_margin = l2["min_gross_margin_pct"] / 100
    min_inst_own = l2["min_institutional_ownership_pct"] / 100
    max_pe = l2["max_pe"]

    rows_passed = []
    rows_excluded = []

    for _, row in df.iterrows():
        reasons = []

        # --- Revenue growth (yoy) ---
        rev_growth = row.get("revenue_growth")
        if rev_growth is None or pd.isna(rev_growth):
            # Missing data: soft fail — note unverifiable but don't hard exclude
            # because some growth companies don't report via yfinance info
            pass  # treat as unknown, keep and flag below
        elif float(rev_growth) < min_rev_growth:
            reasons.append(f"rev_growth:{float(rev_growth)*100:.1f}%<{min_rev_growth*100:.0f}%")

        # --- Gross margin ---
        gross_margin = row.get("gross_margins")
        if gross_margin is None or pd.isna(gross_margin):
            reasons.append("gross_margin:missing")
        elif float(gross_margin) < min_gross_margin:
            reasons.append(f"gross_margin:{float(gross_margin)*100:.1f}%<{min_gross_margin*100:.0f}%")

        # --- Institutional ownership ---
        inst_own = row.get("institutional_ownership")
        if inst_own is None or pd.isna(inst_own):
            reasons.append("inst_ownership:missing")
        elif float(inst_own) < min_inst_own:
            reasons.append(f"inst_own:{float(inst_own)*100:.1f}%<{min_inst_own*100:.0f}%")

        # --- PE ratio (soft: allow None for unprofitable growth stocks) ---
        pe = row.get("trailing_pe")
        if pe is not None and not pd.isna(pe) and float(pe) > max_pe:
            reasons.append(f"trailing_pe:{float(pe):.0f}>{max_pe}")

        # --- Acceleration checks (quarterly financials via yfinance) ---
        # None = insufficient data → soft pass (don't hard-exclude)
        # False = confirmed deceleration → hard-exclude
        l2_cfg = cfg.get("l2_fundamental", {})
        if l2_cfg.get("require_revenue_growth_acceleration", True):
            rev_accel = _check_revenue_acceleration(str(row.get("ticker", "")))
            if rev_accel is False:
                reasons.append("rev_growth:decelerating")
        else:
            rev_accel = None

        eps_needed = int(l2_cfg.get("require_eps_acceleration_consecutive_quarters", 2))
        if eps_needed > 0:
            eps_accel = _check_eps_acceleration(str(row.get("ticker", "")))
            if eps_accel is False:
                reasons.append("eps_growth:decelerating")
        else:
            eps_accel = None

        out = row.to_dict()
        out["l2_pass"] = len(reasons) == 0
        out["rev_accel"] = rev_accel
        out["eps_accel"] = eps_accel
        out["l2_notes"] = ""

        if reasons:
            out["filter_reason"] = "; ".join(reasons)
            rows_excluded.append(out)
        else:
            out["filter_reason"] = ""
            rows_passed.append(out)

    passed = pd.DataFrame(rows_passed) if rows_passed else pd.DataFrame()
    excluded = pd.DataFrame(rows_excluded) if rows_excluded else pd.DataFrame()
    return passed, excluded


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main() -> None:
    parser = argparse.ArgumentParser(description="MCSS L1 + L2 fundamental filter")
    parser.add_argument("--input", default="data/universe_raw.csv")
    parser.add_argument("--output-dir", default="data")
    parser.add_argument("--config", default="config/criteria.yaml")
    args = parser.parse_args()

    out_dir = Path(args.output_dir)
    out_dir.mkdir(parents=True, exist_ok=True)

    cfg = load_config(args.config)

    print(f"Reading {args.input}...")
    df = pd.read_csv(args.input, dtype=str)

    # Cast numeric columns (they were saved as strings in CSV)
    numeric_cols = [
        "market_cap", "price", "avg_volume_20d", "fifty_two_week_high",
        "fifty_two_week_low", "revenue_growth", "gross_margins",
        "profit_margins", "operating_margins", "return_on_equity",
        "free_cashflow", "institutional_ownership", "trailing_pe",
        "forward_pe", "peg_ratio", "price_to_sales", "short_percent_of_float",
    ]
    for col in numeric_cols:
        if col in df.columns:
            df[col] = pd.to_numeric(df[col], errors="coerce")

    print(f"Total tickers: {len(df)}")

    # --- L1 ---
    l1_passed, l1_excluded = apply_l1_filter(df, cfg)
    l1_passed.to_csv(out_dir / "l1_passed.csv", index=False)
    l1_excluded.to_csv(out_dir / "l1_excluded.csv", index=False)
    print(f"L1 filter: {len(l1_passed)} passed, {len(l1_excluded)} excluded")

    earnings_play_count = int(l1_passed["earnings_play"].sum()) if not l1_passed.empty else 0
    print(f"  → Earnings play pool: {earnings_play_count} tickers flagged")

    if l1_passed.empty:
        print("WARNING: No tickers passed L1. Check input data quality.")
        _write_summary(out_dir, df, l1_passed, l1_excluded, pd.DataFrame(), pd.DataFrame())
        return

    # --- L2 ---
    l2_passed, l2_excluded = apply_l2_filter(l1_passed, cfg)
    l2_passed.to_csv(out_dir / "l2_fundamental_passed.csv", index=False)
    l2_excluded.to_csv(out_dir / "l2_excluded.csv", index=False)
    print(f"L2 filter: {len(l2_passed)} passed, {len(l2_excluded)} excluded")

    if l2_passed.empty:
        print("WARNING: No tickers passed L2. Market conditions may be very selective.")

    _write_summary(out_dir, df, l1_passed, l1_excluded, l2_passed, l2_excluded)

    print(f"\nOutput files → {out_dir}/")
    print(f"  l1_passed.csv            ({len(l1_passed)} rows)")
    print(f"  l1_excluded.csv          ({len(l1_excluded)} rows)")
    print(f"  l2_fundamental_passed.csv ({len(l2_passed)} rows)")
    print(f"  l2_excluded.csv          ({len(l2_excluded)} rows)")
    print(f"  fundamental_filter_summary.json")


def _write_summary(
    out_dir: Path,
    raw: pd.DataFrame,
    l1_passed: pd.DataFrame,
    l1_excluded: pd.DataFrame,
    l2_passed: pd.DataFrame,
    l2_excluded: pd.DataFrame,
) -> None:
    """Write a JSON summary for the agent to read."""
    earnings_play = int(l1_passed["earnings_play"].sum()) if not l1_passed.empty else 0

    # Top exclusion reasons for L1
    l1_reasons: dict[str, int] = {}
    if not l1_excluded.empty and "filter_reason" in l1_excluded.columns:
        for reason_str in l1_excluded["filter_reason"].dropna():
            for r in str(reason_str).split(";"):
                key = r.strip().split(":")[0]
                l1_reasons[key] = l1_reasons.get(key, 0) + 1

    l2_reasons: dict[str, int] = {}
    if not l2_excluded.empty and "filter_reason" in l2_excluded.columns:
        for reason_str in l2_excluded["filter_reason"].dropna():
            for r in str(reason_str).split(";"):
                key = r.strip().split(":")[0]
                l2_reasons[key] = l2_reasons.get(key, 0) + 1

    summary = {
        "run_at": datetime.utcnow().isoformat(),
        "input_count": len(raw),
        "l1_passed": len(l1_passed),
        "l1_excluded": len(l1_excluded),
        "l1_earnings_play_flagged": earnings_play,
        "l1_top_exclusion_reasons": dict(
            sorted(l1_reasons.items(), key=lambda x: x[1], reverse=True)[:10]
        ),
        "l2_passed": len(l2_passed),
        "l2_excluded": len(l2_excluded),
        "l2_top_exclusion_reasons": dict(
            sorted(l2_reasons.items(), key=lambda x: x[1], reverse=True)[:10]
        ),
        "l2_passed_tickers": list(l2_passed["ticker"].values) if not l2_passed.empty else [],
    }

    with open(out_dir / "fundamental_filter_summary.json", "w") as f:
        json.dump(summary, f, indent=2)


if __name__ == "__main__":
    main()
