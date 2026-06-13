"""MCSS Day Trade Backtest — replay ORB + Gap strategies over history.

Judges the day trade strategies against the launch gates in
config/criteria.yaml day_trade.backtest.gates (win rate / expectancy /
profit factor, measured OUT-OF-SAMPLE only). day_trade.enabled stays
false until every gate passes.

Same-logic guarantee: candidate selection reuses day_trade_screen's pure
functions (compute_orb_metrics / screen_orb / screen_gap) on historical
slices — the backtest trades exactly what the live screen would have picked.

Data: yfinance daily bars + Alpaca IEX historical minute bars (free tier).
Cached under data/backtest_cache/ — delete the folder to refresh.

Honest caveats (also printed with results):
  - Today's universe replayed historically → survivorship bias, results
    are likely OPTIMISTIC. Treat gates as necessary, not sufficient.
  - Gap% uses the official open as the pre-market proxy.
  - IEX-only prices; thin names may differ from the consolidated tape.
  - Entries assumed filled at the breakout level, no slippage modelled.

Usage:
    python scripts/backtest_daytrade.py [--strategy orb|gap|both]
"""

import argparse
import json
import os
import sys
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Dict, List, Optional
from zoneinfo import ZoneInfo

import pandas as pd
import yaml

ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(ROOT / "scripts"))

from day_trade_screen import compute_orb_metrics, screen_gap, screen_orb
from technical_filter import _atr, _batch_download

CACHE_DIR = ROOT / "data" / "backtest_cache"
ET = ZoneInfo("America/New_York")


# ── Daily data (yfinance, cached) ──────────────────────────────────────────────

def load_daily_data(tickers: List[str]) -> Dict[str, pd.DataFrame]:
    """1y daily OHLCV for all tickers, cached to one parquet."""
    cache = CACHE_DIR / "daily.parquet"
    if cache.exists():
        combined = pd.read_parquet(cache)
        out = {t: df.droplevel(0) for t, df in combined.groupby(level=0)}
        print(f"  daily cache hit: {len(out)} tickers")
        return out
    ohlcv_map = _batch_download(tickers, period="1y")
    ohlcv_map = {t: df for t, df in ohlcv_map.items() if not df.empty}
    combined = pd.concat(ohlcv_map, names=["ticker", "date"])
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    combined.to_parquet(cache)
    print(f"  daily downloaded + cached: {len(ohlcv_map)} tickers")
    return ohlcv_map


# ── Minute data (Alpaca, cached per day) ───────────────────────────────────────

def fetch_minute_bars(symbols: List[str], day: date) -> Dict[str, pd.DataFrame]:
    """Minute bars for one trading day, indexed in ET. Cached per day."""
    if not symbols:
        return {}
    cache = CACHE_DIR / f"minute_{day.isoformat()}.parquet"
    if cache.exists():
        combined = pd.read_parquet(cache)
        cached = {s: df.droplevel(0) for s, df in combined.groupby(level=0)}
        missing = [s for s in symbols if s not in cached]
        if not missing:
            return {s: cached[s] for s in symbols if s in cached}
    else:
        cached, missing = {}, list(symbols)

    from alpaca.data.historical import StockHistoricalDataClient
    from alpaca.data.requests import StockBarsRequest
    from alpaca.data.timeframe import TimeFrame

    client = StockHistoricalDataClient(
        os.environ["ALPACA_API_KEY"], os.environ["ALPACA_API_SECRET"])
    start = datetime.combine(day, datetime.min.time(), tzinfo=ET).replace(hour=9, minute=25)
    end = datetime.combine(day, datetime.min.time(), tzinfo=ET).replace(hour=16, minute=5)
    try:
        req = StockBarsRequest(symbol_or_symbols=missing, timeframe=TimeFrame.Minute,
                               start=start, end=end)
        df = client.get_stock_bars(req).df
    except Exception as exc:
        print(f"  minute fetch {day} failed: {exc}")
        df = pd.DataFrame()

    if not df.empty:
        for sym, sdf in df.groupby(level=0):
            sdf = sdf.droplevel(0)
            sdf.index = sdf.index.tz_convert(ET)
            cached[str(sym)] = sdf

    if cached:
        CACHE_DIR.mkdir(parents=True, exist_ok=True)
        pd.concat(cached, names=["symbol", "timestamp"]).to_parquet(cache)
    return {s: cached[s] for s in symbols if s in cached}


# ── Trade simulation (pure, unit-tested) ───────────────────────────────────────

def simulate_breakout_trade(
    minute_df: pd.DataFrame,
    day: date,
    or_minutes: int,
    target_r: float,
    daily_atr: float,
) -> Optional[Dict]:
    """Simulate one opening-range-breakout day trade on minute bars (ET index).

    Entry:  first bar after the opening range whose high breaks OR high
            (filled at the OR high, no slippage).
    Stop:   the NARROWER of OR low and entry − 1×ATR(daily).
    Target: entry + target_r × risk.  Exit at ~15:55 ET if neither hits.
    Same-bar stop+target → counted as stop (conservative).
    Returns {"r": float, "exit": str} or None when no trade triggered.
    """
    if minute_df is None or minute_df.empty:
        return None
    open_t = datetime.combine(day, datetime.min.time(), tzinfo=ET).replace(hour=9, minute=30)
    or_end = open_t + timedelta(minutes=or_minutes)
    eod_t = open_t.replace(hour=15, minute=55)

    or_bars = minute_df[(minute_df.index >= open_t) & (minute_df.index < or_end)]
    if or_bars.empty:
        return None
    or_high = float(or_bars["high"].max())
    or_low = float(or_bars["low"].min())

    session = minute_df[(minute_df.index >= or_end) & (minute_df.index <= eod_t)]
    if session.empty:
        return None

    entry = or_high
    stop = or_low
    if daily_atr > 0:
        stop = max(stop, entry - daily_atr)   # take the narrower stop
    risk = entry - stop
    if risk <= 0:
        return None
    target = entry + target_r * risk

    in_trade = False
    for ts, bar in session.iterrows():
        high, low = float(bar["high"]), float(bar["low"])
        if not in_trade:
            if high > or_high:
                in_trade = True
                # entered this bar — it can still stop us out before close
                if low <= stop:
                    return {"r": -1.0, "exit": "stop"}
                if high >= target:
                    # triggered and ran to target within the same bar; order of
                    # touches unknowable on 1-min bars → count as stop missed,
                    # target hit only if the bar didn't also touch the stop
                    return {"r": target_r, "exit": "target"}
            continue
        if low <= stop:
            return {"r": -1.0, "exit": "stop"}
        if high >= target:
            return {"r": target_r, "exit": "target"}
    if not in_trade:
        return None
    exit_price = float(session.iloc[-1]["close"])
    return {"r": (exit_price - entry) / risk, "exit": "eod"}


def summarize(trades: List[Dict], trading_days: int) -> Dict:
    """Win rate / expectancy / profit factor / streaks. Pure — unit-tested."""
    if not trades:
        return {"n_trades": 0, "win_rate_pct": 0.0, "expectancy_r": 0.0,
                "profit_factor": 0.0, "max_consec_losses": 0, "trades_per_day": 0.0,
                "cum_r": 0.0}
    rs = [t["r"] for t in trades]
    wins = [r for r in rs if r > 0]
    losses = [r for r in rs if r <= 0]
    gross_win = sum(wins)
    gross_loss = abs(sum(losses))
    streak = max_streak = 0
    for r in rs:
        streak = streak + 1 if r <= 0 else 0
        max_streak = max(max_streak, streak)
    return {
        "n_trades": len(rs),
        "win_rate_pct": round(len(wins) / len(rs) * 100, 1),
        "expectancy_r": round(sum(rs) / len(rs), 3),
        "profit_factor": round(gross_win / gross_loss, 2) if gross_loss > 0 else float("inf"),
        "max_consec_losses": max_streak,
        "trades_per_day": round(len(rs) / trading_days, 2) if trading_days else 0.0,
        "cum_r": round(sum(rs), 1),
    }


# ── Historical candidate replay (reuses live screen logic) ─────────────────────

def replay_orb_watchlists(
    ohlcv_map: Dict[str, pd.DataFrame],
    meta: pd.DataFrame,
    orb_cfg: Dict,
    trade_days: List[date],
) -> Dict[date, List[str]]:
    """For each day D, the top-N ORB watchlist the live screen would have
    produced post-market on D (traded on the NEXT session)."""
    top_n = int(orb_cfg.get("top_n", 5))
    min_rvol = float(orb_cfg.get("min_rvol", 1.5))

    # cheap vectorized precheck (same RVOL formula as compute_orb_metrics)
    # so the expensive exact screen only runs on plausible ticker-days
    rvol_ok: Dict[str, set] = {}
    for t, df in ohlcv_map.items():
        if len(df) < 25 or "Volume" not in df.columns:
            continue
        rvol = df["Volume"] / df["Volume"].rolling(20).mean().shift(1)
        days_ok = set(df.index[rvol >= min_rvol].date)
        if days_ok:
            rvol_ok[t] = days_ok

    out: Dict[date, List[str]] = {}
    for d in trade_days:
        sliced = {}
        for t, days_ok in rvol_ok.items():
            if d not in days_ok:
                continue
            df = ohlcv_map[t]
            sliced[t] = df[df.index.date <= d]
        if not sliced:
            out[d] = []
            continue
        picks = screen_orb(sliced, meta, orb_cfg)
        out[d] = picks.head(top_n)["ticker"].tolist() if not picks.empty else []
    return out


def replay_gap_candidates(
    ohlcv_map: Dict[str, pd.DataFrame],
    meta: pd.DataFrame,
    gap_cfg: Dict,
    trade_days: List[date],
) -> Dict[date, List[str]]:
    """For each day D, the top-N gappers at D's open (official open as the
    pre-market proxy) that the live gap screen would have flagged."""
    top_n = int(gap_cfg.get("top_n", 5))
    min_avg_vol = float(gap_cfg.get("min_avg_volume_20d", 1_000_000))

    out: Dict[date, List[str]] = {}
    for d in trade_days:
        rows = []
        for t, df in ohlcv_map.items():
            idx = df.index.date
            pos = idx.searchsorted(d)
            if pos >= len(idx) or idx[pos] != d or pos == 0:
                continue
            if float(df["Volume"].iloc[max(0, pos - 20):pos].mean() or 0) < min_avg_vol:
                continue
            meta_row = meta.loc[t] if t in meta.index else {}
            rows.append({
                "ticker": t,
                "long_name": str(meta_row.get("long_name", t)),
                "sector": str(meta_row.get("sector", "")),
                "pm_price": float(df["Open"].iloc[pos]),
                "prev_close": float(df["Close"].iloc[pos - 1]),
                "prev_high": float(df["High"].iloc[pos - 1]),
                "prev_low": float(df["Low"].iloc[pos - 1]),
            })
        picks = screen_gap(rows, gap_cfg)
        out[d] = picks.head(top_n)["ticker"].tolist() if not picks.empty else []
    return out


def _daily_atr_asof(df: pd.DataFrame, d: date) -> float:
    """ATR(14) from daily bars strictly BEFORE day d (no lookahead)."""
    hist = df[df.index.date < d]
    if len(hist) < 15:
        return 0.0
    return _atr(hist["High"], hist["Low"], hist["Close"])


# ── Main ───────────────────────────────────────────────────────────────────────

def run_backtest(strategy: str) -> Dict:
    with open(ROOT / "config" / "criteria.yaml") as f:
        cfg = yaml.safe_load(f)
    dt_cfg = cfg.get("day_trade", {})
    bt_cfg = dt_cfg.get("backtest", {})
    lookback_m = int(bt_cfg.get("lookback_months", 6))
    oos_m = int(bt_cfg.get("out_of_sample_months", 2))
    orb_minutes = int(bt_cfg.get("opening_range_minutes", 15))
    gap_minutes = int(bt_cfg.get("gap_or_minutes", 5))
    target_r = float(bt_cfg.get("target_r", 2.0))
    gates = bt_cfg.get("gates", {})

    universe = pd.read_csv(ROOT / "data" / "universe_raw.csv")
    if "quote_type" in universe.columns:
        universe = universe[universe["quote_type"] == "EQUITY"]
    universe = universe[
        (universe["price"] >= float(dt_cfg.get("orb", {}).get("min_price", 5)))
        & (universe["avg_volume_20d"] >= float(dt_cfg.get("orb", {}).get("min_avg_volume_20d", 1_000_000)))
    ]
    tickers = universe["ticker"].astype(str).tolist()
    meta = universe.set_index("ticker")
    print(f"Universe: {len(tickers)} liquid equities (survivorship caveat applies)")

    print("Loading daily data...")
    ohlcv_map = load_daily_data(tickers)

    # trading calendar from the most complete ticker history
    calendar = sorted({d for df in ohlcv_map.values() for d in df.index.date})
    end_day = calendar[-1]
    start_day = end_day - timedelta(days=lookback_m * 30)
    oos_start = end_day - timedelta(days=oos_m * 30)
    trade_days = [d for d in calendar if start_day <= d <= end_day]
    print(f"Window: {trade_days[0]} → {trade_days[-1]} "
          f"({len(trade_days)} sessions; out-of-sample from {oos_start})")

    results: Dict = {"window": {"start": str(trade_days[0]), "end": str(trade_days[-1]),
                                "oos_start": str(oos_start)},
                     "config": {"orb_minutes": orb_minutes, "gap_minutes": gap_minutes,
                                "target_r": target_r}, "strategies": {}}

    plans: Dict[str, Dict[date, List[str]]] = {}
    if strategy in ("orb", "both"):
        print("Replaying ORB watchlists (screen logic = live)...")
        wl = replay_orb_watchlists(ohlcv_map, meta, dt_cfg.get("orb", {}), trade_days)
        # watchlist built on day D → traded next session
        plans["orb"] = {}
        for i, d in enumerate(trade_days[:-1]):
            plans["orb"][trade_days[i + 1]] = wl.get(d, [])
    if strategy in ("gap", "both"):
        print("Replaying Gap candidates...")
        plans["gap"] = replay_gap_candidates(ohlcv_map, meta, dt_cfg.get("gap", {}), trade_days)

    # fetch minute bars day by day (one request/day, cached)
    all_days = sorted({d for p in plans.values() for d in p})
    trades: Dict[str, List[Dict]] = {s: [] for s in plans}
    skipped = 0
    for n, d in enumerate(all_days):
        symbols = sorted({t for s in plans for t in plans[s].get(d, [])})
        if not symbols:
            continue
        bars = fetch_minute_bars(symbols, d)
        for strat, plan in plans.items():
            or_min = orb_minutes if strat == "orb" else gap_minutes
            for t in plan.get(d, []):
                mdf = bars.get(t)
                if mdf is None or mdf.empty:
                    skipped += 1
                    continue
                atr = _daily_atr_asof(ohlcv_map.get(t, pd.DataFrame()), d)
                res = simulate_breakout_trade(mdf, d, or_min, target_r, atr)
                if res is not None:
                    trades[strat].append({**res, "ticker": t, "day": str(d)})
        if (n + 1) % 20 == 0:
            print(f"  simulated {n + 1}/{len(all_days)} days...")

    print(f"Simulation done. {skipped} ticker-days skipped (no minute data).")

    # ── In/out-of-sample split + gates ──
    overall_pass = True
    for strat, tlist in trades.items():
        ins = [t for t in tlist if date.fromisoformat(t["day"]) < oos_start]
        oos = [t for t in tlist if date.fromisoformat(t["day"]) >= oos_start]
        n_ins_days = len([d for d in all_days if d < oos_start])
        n_oos_days = len([d for d in all_days if d >= oos_start])
        s_ins, s_oos = summarize(ins, n_ins_days), summarize(oos, n_oos_days)

        g_pass = (
            s_oos["n_trades"] > 0
            and s_oos["win_rate_pct"] >= float(gates.get("min_win_rate_pct", 45))
            and s_oos["expectancy_r"] >= float(gates.get("min_expectancy_r", 0.15))
            and s_oos["profit_factor"] >= float(gates.get("min_profit_factor", 1.3))
        )
        overall_pass = overall_pass and g_pass
        results["strategies"][strat] = {
            "in_sample": s_ins, "out_of_sample": s_oos, "gates_pass": g_pass,
            "trades": tlist,
        }

        print(f"\n── {strat.upper()} ──")
        for label, s in (("in-sample ", s_ins), ("out-sample", s_oos)):
            print(f"  {label}: {s['n_trades']:4d} trades | win {s['win_rate_pct']:5.1f}% | "
                  f"exp {s['expectancy_r']:+.3f}R | PF {s['profit_factor']:.2f} | "
                  f"cum {s['cum_r']:+.1f}R | maxLoseStreak {s['max_consec_losses']}")
        print(f"  GATES (out-of-sample): {'✅ PASS' if g_pass else '❌ FAIL'} "
              f"(need win≥{gates.get('min_win_rate_pct')}%, exp≥{gates.get('min_expectancy_r')}R, "
              f"PF≥{gates.get('min_profit_factor')})")

    results["all_gates_pass"] = overall_pass
    results["generated_at"] = datetime.now(timezone.utc).isoformat()
    results["caveats"] = [
        "survivorship bias: today's universe replayed historically (optimistic)",
        "gap% proxied by official open",
        "IEX-only prices, no slippage modelled",
    ]
    out = ROOT / "data" / "backtest_daytrade_result.json"
    out.write_text(json.dumps(results, indent=2))
    print(f"\n→ {out}")
    print("⚠️  Caveats: " + "; ".join(results["caveats"]))
    return results


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--strategy", default="both", choices=["orb", "gap", "both"])
    args = parser.parse_args()
    run_backtest(args.strategy)


if __name__ == "__main__":
    main()
