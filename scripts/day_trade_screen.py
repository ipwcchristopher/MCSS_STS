"""MCSS Day Trade Screen — ORB watchlist (post-market) / Gap & Go scan (pre-market).

Modes:
  --mode orb   post-market: rank today's high-RVOL momentum closers as
               next-day Opening Range Breakout candidates (daily data only)
  --mode gap   pre-market: scan pre-market gappers vs previous close
               (Alpaca snapshot primary, yfinance prepost fallback)

Input:  data/universe_raw.csv  (tickers + metadata from fetch_universe.py)
Output: data/day_trade_candidates.csv
        data/day_trade_summary.json

All thresholds come from config/criteria.yaml `day_trade:` section.
Candidates only appear in the Telegram report when day_trade.enabled is true
(flipped after backtest gates pass — see backtest_daytrade.py).
"""

import argparse
import json
import os
import sys
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Dict, List, Optional
from zoneinfo import ZoneInfo

import pandas as pd
import yaml

ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(ROOT / "scripts"))

import catalyst_sources
from technical_filter import _atr, _batch_download


# ── ORB metrics (pure, unit-tested) ────────────────────────────────────────────

def compute_orb_metrics(ohlcv: pd.DataFrame, rvol_window: int = 20) -> Optional[Dict]:
    """Per-ticker metrics off the LAST row of a daily OHLCV frame.

    ORB mode runs post-market, so the last row is a complete session.
    Returns None when there is not enough history or data is degenerate.
    """
    if ohlcv is None or len(ohlcv) < rvol_window + 2:
        return None
    needed = {"High", "Low", "Close", "Volume"}
    if not needed.issubset(ohlcv.columns):
        return None

    close = float(ohlcv["Close"].iloc[-1])
    prev_close = float(ohlcv["Close"].iloc[-2])
    high = float(ohlcv["High"].iloc[-1])
    low = float(ohlcv["Low"].iloc[-1])
    vol = float(ohlcv["Volume"].iloc[-1])
    avg_vol = float(ohlcv["Volume"].iloc[-rvol_window - 1:-1].mean())
    if close <= 0 or prev_close <= 0 or avg_vol <= 0:
        return None

    day_range = high - low
    atr = _atr(ohlcv["High"], ohlcv["Low"], ohlcv["Close"])
    return {
        "close": round(close, 2),
        "change_pct": round((close / prev_close - 1.0) * 100, 2),
        "rvol": round(vol / avg_vol, 2),
        "avg_volume_20d": round(avg_vol),
        "atr": round(atr, 2),
        "atr_pct": round(atr / close * 100, 2),
        # 100 = closed at the high of the day, 0 = at the low
        "close_range_pos": round((close - low) / day_range * 100, 1) if day_range > 0 else 0.0,
        "day_high": round(high, 2),
        "day_low": round(low, 2),
    }


def screen_orb(
    ohlcv_map: Dict[str, pd.DataFrame],
    meta: pd.DataFrame,
    orb_cfg: Dict,
) -> pd.DataFrame:
    """Apply ORB watchlist filters. Pure function — unit-tested.

    meta: universe rows indexed by ticker (long_name, sector columns used).
    Returns candidates sorted by score (rvol × |change|), NOT yet truncated.
    """
    min_price = float(orb_cfg.get("min_price", 5))
    min_avg_vol = float(orb_cfg.get("min_avg_volume_20d", 1_000_000))
    min_rvol = float(orb_cfg.get("min_rvol", 1.5))
    min_atr_pct = float(orb_cfg.get("min_atr_pct", 2.5))
    min_change_pct = float(orb_cfg.get("min_change_pct", 3))
    strong_rvol = float(orb_cfg.get("strong_rvol_override", 2.5))
    top_range_pct = float(orb_cfg.get("close_range_top_pct", 30))

    rows: List[Dict] = []
    for ticker, ohlcv in ohlcv_map.items():
        m = compute_orb_metrics(ohlcv)
        if m is None:
            continue
        if m["close"] < min_price or m["avg_volume_20d"] < min_avg_vol:
            continue
        if m["rvol"] < min_rvol or m["atr_pct"] < min_atr_pct:
            continue
        # momentum trigger: a real % move, or an outright volume explosion
        if abs(m["change_pct"]) < min_change_pct and m["rvol"] < strong_rvol:
            continue
        # strong close: finished in the top X% of the day's range
        if m["close_range_pos"] < 100 - top_range_pct:
            continue

        meta_row = meta.loc[ticker] if ticker in meta.index else {}
        long_name = meta_row.get("long_name", ticker)
        sector = meta_row.get("sector", "")
        rows.append({
            "ticker": ticker,
            "long_name": str(long_name) if pd.notna(long_name) else ticker,
            "sector": str(sector) if pd.notna(sector) else "",
            **m,
            "score": round(m["rvol"] * abs(m["change_pct"]), 2),
        })

    if not rows:
        return pd.DataFrame()
    return pd.DataFrame(rows).sort_values("score", ascending=False).reset_index(drop=True)


# ── Mode runners ───────────────────────────────────────────────────────────────

def _run_orb(universe: pd.DataFrame, dt_cfg: Dict, skip_news: bool) -> pd.DataFrame:
    orb_cfg = dt_cfg.get("orb", {})
    top_n = int(orb_cfg.get("top_n", 5))

    # cheap metadata pre-filter before the bulk download
    # (common equity only — ETF/fund day trading is out of scope, matching L1)
    pre = universe[
        (universe["price"] >= float(orb_cfg.get("min_price", 5)))
        & (universe["avg_volume_20d"] >= float(orb_cfg.get("min_avg_volume_20d", 1_000_000)))
    ]
    if "quote_type" in pre.columns:
        pre = pre[pre["quote_type"] == "EQUITY"]
    tickers = pre["ticker"].astype(str).tolist()
    print(f"ORB pre-filter: {len(universe)} → {len(tickers)} tickers; downloading 3mo OHLCV...")

    ohlcv_map = _batch_download(tickers, period="3mo")
    meta = pre.set_index("ticker")
    candidates = screen_orb(ohlcv_map, meta, orb_cfg)
    print(f"ORB screen: {len(candidates)} passed filters")

    top = candidates.head(top_n).copy()
    if not top.empty and not skip_news:
        top["catalyst_title"] = [
            _top_headline(t, n) for t, n in zip(top["ticker"], top["long_name"])
        ]
    elif not top.empty:
        top["catalyst_title"] = ""
    return top


# ── Gap & Go (pre-market) ──────────────────────────────────────────────────────

def resolve_prev_session(
    daily_close: float, daily_high: float, daily_low: float, daily_date: Optional[date],
    prev_close: float, prev_high: float, prev_low: float,
    today_et: date,
) -> Optional[Dict]:
    """Pick the completed previous session from a snapshot. Pure — unit-tested.

    During pre-market, Alpaca's daily_bar may already be today's partial bar
    (then the reference is previous_daily_bar) or still yesterday's completed
    bar (then it IS the reference). Returns None if neither bar is usable.
    """
    if daily_date is not None and daily_date < today_et and daily_close > 0:
        return {"prev_close": daily_close, "prev_high": daily_high, "prev_low": daily_low}
    if prev_close > 0:
        return {"prev_close": prev_close, "prev_high": prev_high, "prev_low": prev_low}
    return None


def screen_gap(rows: List[Dict], gap_cfg: Dict) -> pd.DataFrame:
    """Apply Gap & Go filters and rank by gap%. Pure function — unit-tested.

    rows: dicts with ticker, long_name, sector, pm_price, prev_close,
          prev_high, prev_low. Stale tickers must already be excluded.
    """
    min_gap = float(gap_cfg.get("min_gap_pct", 3))
    max_gap = float(gap_cfg.get("max_gap_pct", 12))   # FOMO guardrail
    min_price = float(gap_cfg.get("min_price", 5))

    out: List[Dict] = []
    for r in rows:
        pm_price = float(r.get("pm_price", 0))
        prev_close = float(r.get("prev_close", 0))
        if pm_price < min_price or prev_close <= 0:
            continue
        gap_pct = (pm_price / prev_close - 1.0) * 100
        if not (min_gap <= gap_pct <= max_gap):
            continue
        out.append({
            **r,
            "pm_price": round(pm_price, 2),
            "gap_pct": round(gap_pct, 2),
            # report renders 昨日範圍 from day_high/day_low, same as ORB
            "day_high": round(float(r.get("prev_high", 0)), 2),
            "day_low": round(float(r.get("prev_low", 0)), 2),
        })

    if not out:
        return pd.DataFrame()
    return pd.DataFrame(out).sort_values("gap_pct", ascending=False).reset_index(drop=True)


def _alpaca_gap_rows(tickers: List[str], meta: pd.DataFrame, today_et: date) -> Optional[List[Dict]]:
    """Bulk pre-market prices via Alpaca snapshot. None = Alpaca unavailable."""
    key = os.environ.get("ALPACA_API_KEY", "")
    secret = os.environ.get("ALPACA_API_SECRET", "")
    if not key or not secret:
        return None
    try:
        from alpaca.data.historical import StockHistoricalDataClient
        from alpaca.data.requests import StockSnapshotRequest
        client = StockHistoricalDataClient(key, secret)
    except Exception as exc:
        print(f"  Alpaca client init failed: {exc}")
        return None

    rows: List[Dict] = []
    for i in range(0, len(tickers), 1000):
        chunk = tickers[i:i + 1000]
        try:
            snaps = client.get_stock_snapshot(StockSnapshotRequest(symbol_or_symbols=chunk))
        except Exception as exc:
            print(f"  Alpaca snapshot chunk error (skipping {len(chunk)}): {exc}")
            continue
        for sym, snap in snaps.items():
            trade = snap.latest_trade
            if not trade or not trade.timestamp:
                continue
            # stale guard: IEX is thin pre-market — require a trade from today (ET)
            if trade.timestamp.astimezone(ZoneInfo("America/New_York")).date() != today_et:
                continue
            db, pdb = snap.daily_bar, snap.previous_daily_bar
            prev = resolve_prev_session(
                float(db.close) if db else 0.0,
                float(db.high) if db else 0.0,
                float(db.low) if db else 0.0,
                db.timestamp.astimezone(ZoneInfo("America/New_York")).date() if db and db.timestamp else None,
                float(pdb.close) if pdb else 0.0,
                float(pdb.high) if pdb else 0.0,
                float(pdb.low) if pdb else 0.0,
                today_et,
            )
            if prev is None:
                continue
            meta_row = meta.loc[sym] if sym in meta.index else {}
            long_name = meta_row.get("long_name", sym)
            sector = meta_row.get("sector", "")
            rows.append({
                "ticker": sym,
                "long_name": str(long_name) if pd.notna(long_name) else sym,
                "sector": str(sector) if pd.notna(sector) else "",
                "pm_price": float(trade.price),
                **prev,
            })
    return rows


def _yfinance_gap_rows(tickers: List[str], meta: pd.DataFrame) -> List[Dict]:
    """Fallback without Alpaca: yfinance prepost 1m batch for latest PM price.

    Limited to the most liquid names — one heavy batched call, best effort.
    """
    import yfinance as yf
    tickers = tickers[:200]
    print(f"  yfinance fallback over top {len(tickers)} liquid tickers...")
    try:
        intraday = yf.download(tickers, period="1d", interval="1m",
                               prepost=True, progress=False, threads=True)
        daily = yf.download(tickers, period="5d", interval="1d",
                            progress=False, threads=True)
    except Exception as exc:
        print(f"  yfinance fallback failed: {exc}")
        return []
    if intraday.empty or daily.empty:
        return []

    rows: List[Dict] = []
    for t in tickers:
        try:
            pm_series = intraday["Close"][t].dropna() if isinstance(intraday.columns, pd.MultiIndex) else intraday["Close"].dropna()
            d_close = daily["Close"][t].dropna() if isinstance(daily.columns, pd.MultiIndex) else daily["Close"].dropna()
            d_high = daily["High"][t].dropna() if isinstance(daily.columns, pd.MultiIndex) else daily["High"].dropna()
            d_low = daily["Low"][t].dropna() if isinstance(daily.columns, pd.MultiIndex) else daily["Low"].dropna()
            if pm_series.empty or d_close.empty:
                continue
            meta_row = meta.loc[t] if t in meta.index else {}
            long_name = meta_row.get("long_name", t)
            sector = meta_row.get("sector", "")
            rows.append({
                "ticker": t,
                "long_name": str(long_name) if pd.notna(long_name) else t,
                "sector": str(sector) if pd.notna(sector) else "",
                "pm_price": float(pm_series.iloc[-1]),
                "prev_close": float(d_close.iloc[-1]),
                "prev_high": float(d_high.iloc[-1]),
                "prev_low": float(d_low.iloc[-1]),
            })
        except Exception:
            continue
    return rows


def _run_gap(universe: pd.DataFrame, dt_cfg: Dict, skip_news: bool) -> pd.DataFrame:
    gap_cfg = dt_cfg.get("gap", {})
    top_n = int(gap_cfg.get("top_n", 5))
    today_et = datetime.now(ZoneInfo("America/New_York")).date()

    pre = universe[
        (universe["price"] >= float(gap_cfg.get("min_price", 5)))
        & (universe["avg_volume_20d"] >= float(gap_cfg.get("min_avg_volume_20d", 1_000_000)))
    ]
    if "quote_type" in pre.columns:
        pre = pre[pre["quote_type"] == "EQUITY"]
    # most liquid first so the yfinance fallback's cap keeps the right names
    pre = pre.assign(_dollar_vol=pre["price"] * pre["avg_volume_20d"]) \
             .sort_values("_dollar_vol", ascending=False)
    tickers = pre["ticker"].astype(str).tolist()
    meta = pre.set_index("ticker")
    print(f"Gap pre-filter: {len(universe)} → {len(tickers)} tickers")

    rows = _alpaca_gap_rows(tickers, meta, today_et)
    if rows is None:
        rows = _yfinance_gap_rows(tickers, meta)
    print(f"Gap scan: {len(rows)} tickers with fresh pre-market prices")

    candidates = screen_gap(rows, gap_cfg)
    print(f"Gap screen: {len(candidates)} in {gap_cfg.get('min_gap_pct', 3)}–"
          f"{gap_cfg.get('max_gap_pct', 12)}% gap band")

    top = candidates.head(top_n).copy()
    if not top.empty and not skip_news:
        top["catalyst_title"] = [
            _top_headline(t, n) for t, n in zip(top["ticker"], top["long_name"])
        ]
    elif not top.empty:
        top["catalyst_title"] = ""
    return top


def _top_headline(ticker: str, company_name: str) -> str:
    news = catalyst_sources.get_ticker_news(ticker, company_name, window_days=3, max_headlines=1)
    return news[0]["title"][:110] if news else ""


# ── Main ───────────────────────────────────────────────────────────────────────

def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--mode", required=True, choices=["orb", "gap"])
    parser.add_argument("--input", default="data/universe_raw.csv")
    parser.add_argument("--output-dir", default="data")
    parser.add_argument("--dry-run", action="store_true", help="Skip news enrichment")
    args = parser.parse_args()

    out_dir = ROOT / args.output_dir
    out_dir.mkdir(exist_ok=True)
    with open(ROOT / "config" / "criteria.yaml") as f:
        cfg = yaml.safe_load(f)
    dt_cfg = cfg.get("day_trade", {})

    try:
        universe = pd.read_csv(ROOT / args.input)
    except (pd.errors.EmptyDataError, FileNotFoundError):
        universe = pd.DataFrame()

    if universe.empty or "ticker" not in universe.columns:
        print("No universe data — writing empty candidate list.")
        candidates = pd.DataFrame()
    elif args.mode == "orb":
        candidates = _run_orb(universe, dt_cfg, skip_news=args.dry_run)
    else:
        candidates = _run_gap(universe, dt_cfg, skip_news=args.dry_run)

    out_csv = out_dir / "day_trade_candidates.csv"
    candidates.to_csv(out_csv, index=False)
    summary = {
        "mode": args.mode,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "universe_count": int(len(universe)),
        "candidate_count": int(len(candidates)),
        "tickers": candidates["ticker"].tolist() if not candidates.empty else [],
    }
    (out_dir / "day_trade_summary.json").write_text(json.dumps(summary, indent=2))

    if candidates.empty:
        print("Day trade: 0 candidates today.")
    elif args.mode == "orb":
        for _, r in candidates.iterrows():
            print(f"  {r['ticker']:8s} {r['change_pct']:+5.1f}%  RVOL {r['rvol']:.1f}x  "
                  f"ATR {r['atr_pct']:.1f}%  close@{r['close_range_pos']:.0f}%")
    else:
        for _, r in candidates.iterrows():
            print(f"  {r['ticker']:8s} gap {r['gap_pct']:+5.1f}%  "
                  f"PM ${r['pm_price']:.2f} vs prev ${r['prev_close']:.2f}")
    print(f"→ {out_csv}")


if __name__ == "__main__":
    main()
