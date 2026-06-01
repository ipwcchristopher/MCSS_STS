"""MCSS Phase 5 — Full Portfolio Backtesting Engine.

Walk-forward simulation of MCSS L3/L4 screening logic over 1 year of history.
Uses current L2 fundamental universe as the base (survivorship bias noted).

Usage:
    python scripts/backtest.py                   # full 1-year run
    python scripts/backtest.py --dry-run         # download + cache only
    python scripts/backtest.py --refresh-cache   # force re-download OHLCV
"""

import argparse
import sys
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, List, Optional, Tuple

import numpy as np
import pandas as pd
import yaml
import yfinance as yf

ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(ROOT / "scripts"))

from indicators.vcp import compute_vcp_score
from quant_scoring import (
    _momentum_score,
    _quality_score,
    _rs_score,
    _short_squeeze_bonus,
    _value_score,
    _volatility_score,
    _vcp_score_pts,
)
from technical_filter import _ema, _rsi, _atr

CACHE_DIR = ROOT / "data" / "backtest_cache"
REPORT_PATH = ROOT / "data" / "backtest_report.html"
TRADES_PATH = ROOT / "data" / "backtest_trades.csv"


# ── Data Cache ─────────────────────────────────────────────────────────────────

class DataCache:

    def __init__(self, cache_dir: Path = CACHE_DIR):
        self.cache_dir = cache_dir
        self.cache_dir.mkdir(parents=True, exist_ok=True)

    def load_all(
        self, tickers: List[str], refresh: bool = False
    ) -> Dict[str, pd.DataFrame]:
        cached, need_dl = {}, []
        for t in tickers:
            f = self.cache_dir / f"{t}.parquet"
            if not refresh and f.exists():
                try:
                    df = pd.read_parquet(f)
                    if len(df) >= 400:
                        cached[t] = df
                        continue
                except Exception:
                    pass
            need_dl.append(t)

        print(f"OHLCV cache: {len(cached)} loaded | {len(need_dl)} to download")
        if need_dl:
            cached.update(self._batch_download(need_dl))
        return cached

    def _batch_download(self, tickers: List[str]) -> Dict[str, pd.DataFrame]:
        result: Dict[str, pd.DataFrame] = {}
        try:
            raw = yf.download(
                tickers,
                period="2y",
                auto_adjust=True,
                progress=True,
                threads=True,
            )
        except Exception as e:
            print(f"Batch download failed: {e}")
            return result

        if raw.empty:
            return result

        def _extract(ticker: str) -> pd.DataFrame:
            if isinstance(raw.columns, pd.MultiIndex):
                lvl0 = set(raw.columns.get_level_values(0))
                ohlcv = {"Open", "High", "Low", "Close", "Volume"}
                if lvl0 & ohlcv:
                    return raw.xs(ticker, axis=1, level=1)[list(ohlcv)].dropna(how="all")
                else:
                    return raw[ticker][list(ohlcv)].dropna(how="all")
            return raw[["Open", "High", "Low", "Close", "Volume"]].dropna(how="all")

        for t in tickers:
            try:
                df = _extract(t)
                if not df.empty:
                    df.to_parquet(self.cache_dir / f"{t}.parquet")
                    result[t] = df
            except Exception:
                pass

        print(f"Downloaded {len(result)}/{len(tickers)} tickers")
        return result


# ── Pre-computed indicators (vectorised, all dates at once) ───────────────────

def _build_indicator_matrix(
    all_ohlcv: Dict[str, pd.DataFrame],
) -> Dict[str, pd.DataFrame]:
    """Pre-compute all technical indicator time-series for every ticker."""
    indicators: Dict[str, pd.DataFrame] = {}
    for ticker, ohlcv in all_ohlcv.items():
        if len(ohlcv) < 210:
            continue
        c = ohlcv["Close"]
        h = ohlcv["High"]
        lo = ohlcv["Low"]
        v = ohlcv["Volume"]

        ind = pd.DataFrame(index=ohlcv.index)
        ind["close"]  = c
        ind["high"]   = h
        ind["low"]    = lo
        ind["volume"] = v
        ind["ema20"]  = c.ewm(span=20,  adjust=False).mean()
        ind["ema50"]  = c.ewm(span=50,  adjust=False).mean()
        ind["ema150"] = c.ewm(span=150, adjust=False).mean()
        ind["ema200"] = c.ewm(span=200, adjust=False).mean()

        delta = c.diff()
        gain = delta.clip(lower=0).ewm(com=13, adjust=False).mean()
        loss = (-delta).clip(lower=0).ewm(com=13, adjust=False).mean()
        ind["rsi14"] = 100 - (100 / (1 + gain / loss.replace(0, np.nan))).fillna(100)

        prev = c.shift(1)
        tr = pd.concat([h - lo, (h - prev).abs(), (lo - prev).abs()], axis=1).max(axis=1)
        ind["atr14"] = tr.ewm(com=13, adjust=False).mean()

        ind["vol_avg20"] = v.rolling(20, min_periods=10).mean()

        ind["high_252"] = h.rolling(252, min_periods=63).max()
        ind["dist_52wh"] = (ind["high_252"] - c) / ind["high_252"] * 100

        ind["perf_5d"]  = c.pct_change(5)
        ind["perf_10d"] = c.pct_change(10)
        ind["perf_20d"] = c.pct_change(20)
        ind["momentum"] = (
            0.5 * ind["perf_5d"].fillna(0)
            + 0.3 * ind["perf_10d"].fillna(0)
            + 0.2 * ind["perf_20d"].fillna(0)
        )

        indicators[ticker] = ind
    return indicators


def _precompute_rs_ratings(
    ind_map: Dict[str, pd.DataFrame],
    trading_days: List[pd.Timestamp],
    step: int = 5,
) -> Dict[pd.Timestamp, Dict[str, int]]:
    """Vectorised RS Rating computation, recomputed every `step` trading days."""
    close_matrix = pd.DataFrame({t: df["close"] for t, df in ind_map.items()})
    weights = [(63, 0.4), (126, 0.2), (189, 0.2), (252, 0.2)]
    cache: Dict[pd.Timestamp, Dict[str, int]] = {}

    print(f"Pre-computing RS Ratings (every {step} days)...", end="", flush=True)
    computed = 0
    for i, date in enumerate(trading_days):
        if i % step != 0:
            continue
        sub = close_matrix[close_matrix.index <= date]
        if len(sub) < 63:
            cache[date] = {}
            continue

        composite = pd.Series(0.0, index=sub.columns)
        current = sub.iloc[-1]
        for days, w in weights:
            if len(sub) >= days:
                past = sub.iloc[-days]
                ret = (current / past - 1.0).fillna(0)
                composite = composite + w * ret

        ranked = (composite.rank(pct=True) * 99).round().astype(int)
        cache[date] = ranked.to_dict()
        computed += 1

    print(f" done ({computed} snapshots)")
    return cache


def _get_rs(
    cache: Dict[pd.Timestamp, Dict[str, int]], date: pd.Timestamp
) -> Dict[str, int]:
    dates = [d for d in cache if d <= date]
    return cache[max(dates)] if dates else {}


# ── L3 + L4 filter for a single ticker at a given row ────────────────────────

def _passes_l3(row: pd.Series, l3_cfg: Dict) -> bool:
    if any(pd.isna(row.get(k)) for k in ["ema20", "ema50", "ema150", "ema200", "rsi14", "atr14"]):
        return False
    c = float(row["close"])
    e20, e50, e150, e200 = (
        float(row["ema20"]), float(row["ema50"]),
        float(row["ema150"]), float(row["ema200"]),
    )
    if not (c > e20 > 0 and c > e50 and c > e150 and c > e200):
        return False
    if not (e50 > e150 > e200):
        return False
    rsi = float(row["rsi14"])
    if rsi > float(l3_cfg.get("max_rsi14", 72)) or rsi < float(l3_cfg.get("min_rsi14", 45)):
        return False
    if float(row.get("dist_52wh", 100)) > float(l3_cfg.get("max_distance_from_52w_high_pct", 25)):
        return False
    return True


def _l4_score(
    ind_row: pd.Series,
    fund_row: pd.Series,
    rs_rating: int,
    ohlcv_slice: pd.DataFrame,
    l4_cfg: Dict,
) -> float:
    composite = float(ind_row.get("momentum", 0))
    _, vcp_sc = compute_vcp_score(ohlcv_slice)
    gm_pts, roe_pts, fcf_pts = _quality_score(fund_row)
    total = (
        _rs_score(rs_rating, l4_cfg)
        + gm_pts + roe_pts + fcf_pts
        + _momentum_score(composite, int(l4_cfg.get("momentum_score_max", 25)))
        + _vcp_score_pts(vcp_sc, int(l4_cfg.get("vcp_tightness_score_max", 15)))
        + _volatility_score(float(ind_row.get("atr14", 0)), float(ind_row.get("close", 1)))
        + _value_score(fund_row)
        + _short_squeeze_bonus(fund_row, composite, l4_cfg)
    )
    return float(total)


# ── Position dataclass ────────────────────────────────────────────────────────

@dataclass
class Position:
    ticker: str
    entry_date: pd.Timestamp
    entry_price: float
    shares: float
    stop: float
    target: float
    score: float
    exit_date: Optional[pd.Timestamp] = None
    exit_price: Optional[float] = None
    outcome: Optional[str] = None


# ── Portfolio Simulator ───────────────────────────────────────────────────────

class PortfolioSimulator:

    def __init__(
        self,
        cfg: Dict,
        ind_map: Dict[str, pd.DataFrame],
        all_ohlcv: Dict[str, pd.DataFrame],
        universe_df: pd.DataFrame,
        rs_cache: Dict[pd.Timestamp, Dict[str, int]],
    ):
        self.cfg = cfg
        self.ind_map = ind_map
        self.all_ohlcv = all_ohlcv
        self.universe_df = universe_df
        self.rs_cache = rs_cache

        bt = cfg.get("backtest", {})
        sz = cfg.get("position_sizing", {})
        gr = cfg.get("guardrails", {})

        self.initial_capital: float = float(bt.get("initial_capital", 100_000))
        self.reward_ratio: float = float(bt.get("reward_ratio", 2.0))
        self.risk_pct: float = float(sz.get("risk_per_trade_pct", 2)) / 100
        self.stop_mult: float = float(sz.get("stop_atr_multiplier", 1.5))
        self.max_pos_pct: float = float(sz.get("max_position_pct", 25)) / 100
        self.max_positions: int = int(gr.get("max_concurrent_positions", 6))
        self.halt_dd: float = float(gr.get("halt_new_entries_portfolio_drawdown_pct", 10)) / 100

    def run(
        self,
        trading_days: List[pd.Timestamp],
    ) -> Tuple[pd.Series, pd.DataFrame]:
        l3_cfg = self.cfg.get("l3_technical", {})
        l4_cfg = self.cfg.get("l4_scoring", {})
        min_score = int(l4_cfg.get("min_total_score", 60))

        cash = self.initial_capital
        peak = self.initial_capital
        positions: List[Position] = []
        trades: List[Dict] = []
        daily_values: Dict[pd.Timestamp, float] = {}

        fund_lookup = self.universe_df.set_index("ticker")

        print(f"\n{'='*62}")
        print(f"Walk-forward: {trading_days[0].date()} → {trading_days[-1].date()}")
        print(f"Days: {len(trading_days)} | Universe: {len(self.universe_df)} stocks")
        print(f"Capital: ${self.initial_capital:,.0f} | Max positions: {self.max_positions}")
        print(f"{'='*62}\n")

        for date in trading_days:

            # ── 1. Close positions that hit stop or target ─────────────────
            still_open: List[Position] = []
            for pos in positions:
                df = self.all_ohlcv.get(pos.ticker)
                if df is None or date not in df.index:
                    still_open.append(pos)
                    continue

                day_high = float(df.loc[date, "High"])
                day_low  = float(df.loc[date, "Low"])

                if day_low <= pos.stop:
                    pos.exit_date, pos.exit_price, pos.outcome = date, pos.stop, "stop"
                elif day_high >= pos.target:
                    pos.exit_date, pos.exit_price, pos.outcome = date, pos.target, "target"
                else:
                    still_open.append(pos)
                    continue

                cash += pos.shares * pos.exit_price
                pnl_pct = (pos.exit_price / pos.entry_price - 1) * 100
                print(
                    f"  [{date.date()}] CLOSE {pos.ticker:6s} "
                    f"@ ${pos.exit_price:.2f} ({pos.outcome.upper():6s}) "
                    f"{pnl_pct:+.1f}%  cash: ${cash:,.0f}"
                )
                trades.append({
                    "ticker": pos.ticker,
                    "entry_date": pos.entry_date.date(),
                    "entry_price": round(pos.entry_price, 2),
                    "exit_date": pos.exit_date.date(),
                    "exit_price": round(pos.exit_price, 2),
                    "shares": round(pos.shares, 4),
                    "pnl_usd": round((pos.exit_price - pos.entry_price) * pos.shares, 2),
                    "pnl_pct": round(pnl_pct, 2),
                    "outcome": pos.outcome,
                    "hold_days": (pos.exit_date - pos.entry_date).days,
                    "entry_score": round(pos.score, 1),
                })

            positions = still_open

            # ── 2. Daily portfolio value ───────────────────────────────────
            open_value = sum(
                float(self.all_ohlcv[p.ticker].loc[date, "Close"]) * p.shares
                if p.ticker in self.all_ohlcv and date in self.all_ohlcv[p.ticker].index
                else p.entry_price * p.shares
                for p in positions
            )
            portfolio_value = cash + open_value
            peak = max(peak, portfolio_value)
            daily_values[date] = portfolio_value

            # ── 3. Drawdown guardrail ─────────────────────────────────────
            drawdown = (peak - portfolio_value) / peak
            if drawdown >= self.halt_dd:
                continue

            # ── 4. Screen for new entries ─────────────────────────────────
            slots = self.max_positions - len(positions)
            if slots <= 0:
                continue

            open_tickers = {p.ticker for p in positions}
            rs_today = _get_rs(self.rs_cache, date)
            candidates: List[Dict] = []

            for ticker, ind_df in self.ind_map.items():
                if ticker in open_tickers:
                    continue
                if ticker not in fund_lookup.index:
                    continue
                if date not in ind_df.index:
                    continue

                # EMA200 uptrend check (21-day look-back)
                date_iloc = ind_df.index.get_loc(date)
                if date_iloc < 221:
                    continue
                e200_now  = float(ind_df["ema200"].iloc[date_iloc])
                e200_21d  = float(ind_df["ema200"].iloc[date_iloc - 21])
                if e200_now <= e200_21d:
                    continue

                ind_row = ind_df.loc[date]

                # RS Rating gate
                rs = rs_today.get(ticker, 0)
                if rs < int(l3_cfg.get("min_rs_rating", 70)):
                    continue

                # L3 filter (vectorised lookup)
                if not _passes_l3(ind_row, l3_cfg):
                    continue

                # Volume ratio
                vol_avg = float(ind_row.get("vol_avg20", 0))
                vol_today = float(ind_row.get("volume", 0))
                if vol_avg > 0 and vol_today / vol_avg < float(l3_cfg.get("min_volume_ratio_vs_avg20d", 1.2)):
                    continue

                # L4 score (pass OHLCV slice for VCP)
                ohlcv_slice = self.all_ohlcv[ticker][self.all_ohlcv[ticker].index <= date]
                fund_row = fund_lookup.loc[ticker]
                score = _l4_score(ind_row, fund_row, rs, ohlcv_slice, l4_cfg)

                if score >= min_score:
                    candidates.append({
                        "ticker": ticker,
                        "price": float(ind_row["close"]),
                        "atr": float(ind_row["atr14"]),
                        "score": score,
                    })

            candidates.sort(key=lambda x: x["score"], reverse=True)

            for cand in candidates[:slots]:
                price = cand["price"]
                atr   = cand["atr"]
                stop_dist = self.stop_mult * atr
                if stop_dist <= 0:
                    continue
                stop   = price - stop_dist
                target = price + self.reward_ratio * stop_dist

                shares = min(
                    (portfolio_value * self.risk_pct) / stop_dist,
                    (portfolio_value * self.max_pos_pct) / price,
                )
                cost = shares * price
                if cost > cash or shares < 0.01:
                    continue

                cash -= cost
                positions.append(Position(
                    ticker=cand["ticker"],
                    entry_date=date,
                    entry_price=price,
                    shares=shares,
                    stop=stop,
                    target=target,
                    score=cand["score"],
                ))
                print(
                    f"  [{date.date()}] OPEN  {cand['ticker']:6s} "
                    f"@ ${price:.2f}  stop ${stop:.2f}  tgt ${target:.2f} "
                    f"score {cand['score']:.0f}  cash left: ${cash:,.0f}"
                )

        # Close remaining positions at last close
        for pos in positions:
            df = self.all_ohlcv.get(pos.ticker)
            last_price = float(df["Close"].iloc[-1]) if df is not None else pos.entry_price
            last_date  = df.index[-1] if df is not None else trading_days[-1]
            cash += pos.shares * last_price
            pnl_pct = (last_price / pos.entry_price - 1) * 100
            trades.append({
                "ticker": pos.ticker,
                "entry_date": pos.entry_date.date(),
                "entry_price": round(pos.entry_price, 2),
                "exit_date": last_date.date(),
                "exit_price": round(last_price, 2),
                "shares": round(pos.shares, 4),
                "pnl_usd": round((last_price - pos.entry_price) * pos.shares, 2),
                "pnl_pct": round(pnl_pct, 2),
                "outcome": "end_of_backtest",
                "hold_days": (last_date - pos.entry_date).days,
                "entry_score": round(pos.score, 1),
            })

        daily_series = pd.Series(daily_values, name="portfolio_value")
        returns = daily_series.pct_change().dropna()
        return returns, pd.DataFrame(trades)


# ── Summary + Report ──────────────────────────────────────────────────────────

def _print_summary(returns: pd.Series, trades_df: pd.DataFrame, initial: float) -> None:
    print(f"\n{'='*62}")
    print("BACKTEST SUMMARY")
    print(f"{'='*62}")

    if trades_df.empty:
        print("No trades executed.")
        return

    closed = trades_df[trades_df["outcome"] != "end_of_backtest"]
    wins   = closed[closed["pnl_pct"] > 0]
    losses = closed[closed["pnl_pct"] <= 0]

    total_return = float((1 + returns).prod() - 1)
    win_rate = len(wins) / max(len(closed), 1) * 100
    avg_win  = float(wins["pnl_pct"].mean()) if not wins.empty else 0.0
    avg_loss = float(losses["pnl_pct"].mean()) if not losses.empty else 0.0
    expectancy = (win_rate / 100 * avg_win) + ((1 - win_rate / 100) * avg_loss)

    print(f"  Total closed trades:  {len(closed)}")
    print(f"  Win rate:             {win_rate:.1f}%")
    print(f"  Avg win:              {avg_win:+.1f}%")
    print(f"  Avg loss:             {avg_loss:+.1f}%")
    print(f"  Expectancy / trade:   {expectancy:+.2f}%")
    print(f"  Target hits:          {len(closed[closed['outcome'] == 'target'])}")
    print(f"  Stop hits:            {len(closed[closed['outcome'] == 'stop'])}")
    print(f"  Total return:         {total_return * 100:+.1f}%")
    ann = ((1 + total_return) ** (252 / max(len(returns), 1)) - 1) * 100
    print(f"  Annualised return:    {ann:+.1f}%")
    print(f"\n  ⚠️  Survivorship bias: L2 uses CURRENT fundamental data.")
    print(f"{'='*62}\n")


def _generate_report(returns: pd.Series, trades_df: pd.DataFrame) -> None:
    try:
        import quantstats as qs
        qs.extend_pandas()
        qs.reports.html(
            returns,
            benchmark="SPY",
            output=str(REPORT_PATH),
            title="MCSS Backtest — 1 Year Walk-Forward Simulation",
            download_filename=str(REPORT_PATH),
        )
        print(f"HTML report → {REPORT_PATH}")
    except Exception as e:
        print(f"quantstats report failed ({e}); trades saved to {TRADES_PATH}")

    if not trades_df.empty:
        trades_df.to_csv(TRADES_PATH, index=False)
        print(f"Trades CSV  → {TRADES_PATH}")


# ── Main ───────────────────────────────────────────────────────────────────────

def main() -> None:
    parser = argparse.ArgumentParser(description="MCSS Phase 5 Backtesting Engine")
    parser.add_argument("--dry-run",       action="store_true",
                        help="Download + cache data only, skip simulation")
    parser.add_argument("--refresh-cache", action="store_true",
                        help="Force re-download all OHLCV data")
    parser.add_argument("--universe",      default="data/l2_fundamental_passed.csv",
                        help="Path to L2 universe CSV")
    args = parser.parse_args()

    config_path = ROOT / "config" / "criteria.yaml"
    with open(config_path) as f:
        cfg = yaml.safe_load(f)

    universe_path = ROOT / args.universe
    if not universe_path.exists():
        print(f"Universe not found: {universe_path}")
        print("Run the pipeline first:  python scripts/run_pipeline.py")
        sys.exit(1)

    universe_df = pd.read_csv(universe_path)
    tickers = universe_df["ticker"].tolist()

    print(f"\nMCSS Backtesting Engine  ({datetime.now(timezone.utc).strftime('%Y-%m-%d')})")
    print(f"Universe: {len(tickers)} tickers  |  ⚠️  L2 uses current fundamentals (survivorship bias)\n")

    # Phase A — data cache
    cache = DataCache()
    all_ohlcv = cache.load_all(tickers, refresh=args.refresh_cache)

    if args.dry_run:
        print(f"\n--dry-run complete. {len(all_ohlcv)} tickers cached → {CACHE_DIR}")
        sys.exit(0)

    if not all_ohlcv:
        print("No OHLCV data. Check internet connection.")
        sys.exit(1)

    # Phase B — pre-compute indicators + RS
    print("\nBuilding indicator matrix...")
    ind_map = _build_indicator_matrix(all_ohlcv)
    print(f"Indicators built for {len(ind_map)} tickers")

    all_dates = sorted({d for df in all_ohlcv.values() for d in df.index})
    bt_cfg = cfg.get("backtest", {})
    window = int(bt_cfg.get("window_days", 252))
    warmup = int(bt_cfg.get("warmup_days", 260))

    if len(all_dates) < warmup + 20:
        print(f"Insufficient history ({len(all_dates)} days, need {warmup + 20})")
        sys.exit(1)

    trading_days = all_dates[-(window + 1):]

    rs_step = int(bt_cfg.get("rs_recompute_interval_days", 5))
    rs_cache = _precompute_rs_ratings(ind_map, all_dates, step=rs_step)

    # Phase C — walk-forward simulation
    sim = PortfolioSimulator(cfg, ind_map, all_ohlcv, universe_df, rs_cache)
    returns, trades_df = sim.run(trading_days)

    # Phase D — report
    _print_summary(returns, trades_df, sim.initial_capital)
    _generate_report(returns, trades_df)


if __name__ == "__main__":
    main()
