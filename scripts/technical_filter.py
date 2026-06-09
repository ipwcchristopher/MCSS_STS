"""MCSS Phase 2 — L3 Technical Filter (Minervini Trend Template + RS + RSI + Volume).

Input:  data/l1_passed.csv
Output: data/l3_technical_passed.csv
        data/l3_excluded.csv
        data/technical_filter_summary.json
"""

import argparse
import json
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import pandas as pd
import yfinance as yf
import yaml

ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(ROOT / "scripts"))

from indicators.rs_rating import compute_rs_ratings, _download_close_prices
from indicators.vcp import compute_vcp_batch


# ── Pure-pandas indicator helpers ──────────────────────────────────────────────

def _ema(series: pd.Series, span: int) -> pd.Series:
    return series.ewm(span=span, adjust=False).mean()


def _rsi(close: pd.Series, period: int = 14) -> float:
    """Wilder's RSI — return the most recent value."""
    delta = close.diff()
    gain = delta.clip(lower=0)
    loss = (-delta).clip(lower=0)
    avg_gain = gain.ewm(com=period - 1, adjust=False).mean()
    avg_loss = loss.ewm(com=period - 1, adjust=False).mean()
    last_loss = float(avg_loss.iloc[-1])
    if last_loss == 0:
        return 100.0
    rs = float(avg_gain.iloc[-1]) / last_loss
    return 100.0 - (100.0 / (1.0 + rs))


def _atr(high: pd.Series, low: pd.Series, close: pd.Series, period: int = 14) -> float:
    """Wilder's ATR — return the most recent value."""
    prev_close = close.shift(1)
    tr = pd.concat([
        high - low,
        (high - prev_close).abs(),
        (low - prev_close).abs(),
    ], axis=1).max(axis=1)
    atr_series = tr.ewm(com=period - 1, adjust=False).mean()
    return float(atr_series.iloc[-1])


def _volume_ratio(volume: pd.Series, window: int = 20) -> float:
    """Last COMPLETE day's volume ÷ prior 20-day average.

    Uses iloc[-2] as the reference day so partial intraday volume
    (when running during market hours) does not distort the ratio.
    """
    if len(volume) < window + 2:
        return 0.0
    recent = float(volume.iloc[-2])
    avg = float(volume.iloc[-window - 2:-2].mean())
    return recent / avg if avg > 0 else 0.0


# ── yfinance batch download ────────────────────────────────────────────────────

def _batch_download(tickers: List[str], period: str = "1y") -> Dict[str, pd.DataFrame]:
    """
    Download OHLCV for all tickers in one call.
    Returns {ticker: ohlcv_df} — empty df for any that failed.
    """
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
        print(f"[technical_filter] batch download failed: {exc}")
        return {t: pd.DataFrame() for t in tickers}

    if raw.empty:
        return {t: pd.DataFrame() for t in tickers}

    result: Dict[str, pd.DataFrame] = {}

    if isinstance(raw.columns, pd.MultiIndex):
        # Multi-ticker: columns = (price_type, ticker) or (ticker, price_type)
        lvl0 = list(raw.columns.get_level_values(0).unique())
        ohlcv_keys = {"Open", "High", "Low", "Close", "Volume"}

        if set(lvl0) & ohlcv_keys:
            # Level 0 = price type, level 1 = ticker
            for ticker in tickers:
                try:
                    df = raw.xs(ticker, axis=1, level=1)[
                        ["Open", "High", "Low", "Close", "Volume"]
                    ].dropna(how="all")
                    result[ticker] = df
                except Exception:
                    result[ticker] = pd.DataFrame()
        else:
            # Level 0 = ticker
            for ticker in tickers:
                try:
                    df = raw[ticker][
                        ["Open", "High", "Low", "Close", "Volume"]
                    ].dropna(how="all")
                    result[ticker] = df
                except Exception:
                    result[ticker] = pd.DataFrame()
    else:
        # Single ticker
        result[tickers[0]] = raw[
            ["Open", "High", "Low", "Close", "Volume"]
        ].dropna(how="all")
        for t in tickers[1:]:
            result[t] = pd.DataFrame()

    return result


# ── L3 filter logic ────────────────────────────────────────────────────────────

def _apply_l3_filter(
    row: pd.Series,
    ohlcv: pd.DataFrame,
    rs_rating: int,
    vcp_detected: bool,
    vcp_score: int,
    cfg: Dict[str, Any],
) -> Tuple[bool, str, Dict[str, Any]]:
    """
    Apply all L3 conditions to a single ticker.

    Returns:
        (passed, filter_reason, computed_metrics)
    """
    l3 = cfg.get("l3_technical", {})
    metrics: Dict[str, Any] = {
        "rs_rating": rs_rating,
        "vcp_detected": vcp_detected,
        "vcp_score": vcp_score,
    }

    if ohlcv.empty or len(ohlcv) < 210:
        return False, "insufficient_history", metrics

    close = ohlcv["Close"]
    high = ohlcv["High"]
    low = ohlcv["Low"]
    volume = ohlcv["Volume"]

    # Current values
    price = float(close.iloc[-1])
    if price <= 0:
        return False, "invalid_price", metrics

    # EMAs
    e20 = float(_ema(close, 20).iloc[-1])
    e50 = float(_ema(close, 50).iloc[-1])
    e150 = float(_ema(close, 150).iloc[-1])
    e200 = float(_ema(close, 200).iloc[-1])
    e200_21d_ago = float(_ema(close, 200).iloc[-22]) if len(close) >= 222 else e200

    metrics.update({
        "ema20": round(e20, 4),
        "ema50": round(e50, 4),
        "ema150": round(e150, 4),
        "ema200": round(e200, 4),
    })

    # RSI
    rsi = _rsi(close)
    metrics["rsi14"] = round(rsi, 2)

    # ATR
    atr = _atr(high, low, close)
    metrics["atr"] = round(atr, 4)

    # Volume ratio
    vol_ratio = _volume_ratio(volume)
    metrics["volume_ratio"] = round(vol_ratio, 3)

    # 52-week high distance (from OHLCV, more accurate than L2 static data)
    high_52w = float(high.tail(252).max())
    dist_52wh = (high_52w - price) / high_52w * 100 if high_52w > 0 else 0.0
    metrics["dist_from_52wh_pct"] = round(dist_52wh, 2)
    metrics["high_52w"] = round(high_52w, 4)

    # Trend Template (Minervini): check each condition in order
    trend_pass = True
    fail_reason = ""

    checks = [
        (price > e20, "price_below_ema20"),
        (price > e50, "price_below_ema50"),
        (price > e150, "price_below_ema150"),
        (price > e200, "price_below_ema200"),
        (e50 > e150, "ema50_below_ema150"),
        (e150 > e200, "ema150_below_ema200"),
        (e200 > e200_21d_ago, "ema200_not_uptrend"),
    ]
    for condition, reason in checks:
        if not condition:
            trend_pass = False
            fail_reason = reason
            break

    metrics["trend_template_pass"] = trend_pass

    if not trend_pass:
        return False, fail_reason, metrics

    # 52w high distance
    max_dist = float(l3.get("max_distance_from_52w_high_pct", 25))
    if dist_52wh > max_dist:
        return False, f"dist_from_52wh:{dist_52wh:.1f}%>{max_dist}%", metrics

    # RS Rating
    min_rs = int(l3.get("min_rs_rating", 70))
    if rs_rating < min_rs:
        return False, f"rs_rating:{rs_rating}<{min_rs}", metrics

    # RSI — FOMO hard block at 72
    min_rsi = float(l3.get("min_rsi14", 45))
    max_rsi = float(l3.get("max_rsi14", 72))
    if rsi > max_rsi:
        return False, f"rsi_fomo_block:{rsi:.1f}>{max_rsi}", metrics
    if rsi < min_rsi:
        return False, f"rsi_too_low:{rsi:.1f}<{min_rsi}", metrics

    # Volume ratio
    min_vol = float(l3.get("min_volume_ratio_vs_avg20d", 1.2))
    if vol_ratio < min_vol:
        return False, f"volume_ratio:{vol_ratio:.2f}<{min_vol}", metrics

    # VCP hard filter — enforced when vcp_optional is False
    vcp_required = not bool(l3.get("vcp_optional", True))
    if vcp_required and not vcp_detected:
        min_vcp = int(l3.get("min_vcp_score", 60))
        if vcp_score < min_vcp:
            return False, f"vcp_required:score={vcp_score}<{min_vcp}", metrics

    return True, "", metrics


# ── Main ───────────────────────────────────────────────────────────────────────

def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--input", default="data/l2_fundamental_passed.csv")
    parser.add_argument("--output-dir", default="data")
    args = parser.parse_args()

    input_path = ROOT / args.input
    out_dir = ROOT / args.output_dir
    out_dir.mkdir(exist_ok=True)

    config_path = ROOT / "config" / "criteria.yaml"
    with open(config_path) as f:
        cfg = yaml.safe_load(f)

    print(f"Reading {input_path}...")
    l2 = pd.read_csv(input_path)
    tickers = l2["ticker"].tolist()
    print(f"Total tickers: {len(tickers)}")

    # ── Download price history ───────────────────────────────────────────
    print("Downloading OHLCV data (1 year)...")
    ohlcv_map = _batch_download(tickers, period="1y")

    # ── RS Ratings ──────────────────────────────────────────────────────
    print("Computing RS Ratings...")
    close_df = pd.DataFrame({
        t: ohlcv_map[t]["Close"] for t in tickers
        if not ohlcv_map[t].empty and "Close" in ohlcv_map[t].columns
    })
    rs_ratings = compute_rs_ratings(tickers, close_prices=close_df)

    # ── VCP (optional) ──────────────────────────────────────────────────
    print("Computing VCP scores...")
    vcp_results = compute_vcp_batch(
        {t: ohlcv_map[t] for t in tickers if not ohlcv_map[t].empty}
    )

    # ── Apply L3 filter ─────────────────────────────────────────────────
    passed_rows: List[Dict] = []
    excluded_rows: List[Dict] = []
    screened_at = datetime.now(timezone.utc).isoformat()

    for _, row in l2.iterrows():
        ticker = row["ticker"]
        ohlcv = ohlcv_map.get(ticker, pd.DataFrame())
        rs = rs_ratings.get(ticker, 0)
        vcp_det, vcp_sc = vcp_results.get(ticker, (False, 0))

        try:
            passed, reason, metrics = _apply_l3_filter(row, ohlcv, rs, vcp_det, vcp_sc, cfg)
        except Exception as exc:
            passed, reason, metrics = False, f"error:{exc}", {}

        out_row = row.to_dict()
        out_row.update(metrics)
        out_row["l3_pass"] = passed
        out_row["filter_reason"] = reason if not passed else ""
        out_row["l3_notes"] = ""
        out_row["screened_at"] = screened_at

        if passed:
            passed_rows.append(out_row)
        else:
            excluded_rows.append(out_row)

    # ── Write outputs ────────────────────────────────────────────────────
    passed_df = pd.DataFrame(passed_rows)
    excluded_df = pd.DataFrame(excluded_rows)

    passed_df.to_csv(out_dir / "l3_technical_passed.csv", index=False)
    excluded_df.to_csv(out_dir / "l3_excluded.csv", index=False)

    # Summarise top exclusion reasons
    top_reasons: Dict[str, int] = {}
    for r in excluded_rows:
        reason = str(r.get("filter_reason", "unknown"))
        key = reason.split(":")[0]
        top_reasons[key] = top_reasons.get(key, 0) + 1

    summary = {
        "run_at": screened_at,
        "l2_input": len(l2),
        "l3_passed": len(passed_rows),
        "l3_excluded": len(excluded_rows),
        "l3_top_exclusion_reasons": top_reasons,
        "l3_passed_tickers": [r["ticker"] for r in passed_rows],
    }
    with open(out_dir / "technical_filter_summary.json", "w") as f:
        json.dump(summary, f, indent=2)

    print(f"L3 filter: {len(passed_rows)} passed, {len(excluded_rows)} excluded")
    print(f"  → data/l3_technical_passed.csv ({len(passed_rows)} rows)")

    top = sorted(top_reasons.items(), key=lambda x: x[1], reverse=True)[:5]
    print(f"  Top exclusion reasons: {top}")


if __name__ == "__main__":
    main()
